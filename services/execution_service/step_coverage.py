"""Which generated steps do not actually do anything yet.

A run that writes a feature file, a page object and a step file, compiles them
all, and reports success is still not finished if three of its steps are
`return 'pending'`. The suite goes green in the sense that nothing errored and
red in the sense that nothing was verified — which is the failure mode this
whole platform exists to refuse.

There are two distinct gaps and they need different instruments:

**Undefined.** A step in a `.feature` with no definition anywhere. Cucumber
knows this exactly, and `--dry-run` asks it in about ten milliseconds without
starting a browser. That is the external truth, and it catches steps no
generator ever knew about — a hand-written feature, a Background inherited from
elsewhere.

**Pending.** A definition exists but its body is a TODO. The renderer emits
`return 'pending';` precisely so this reports honestly at run time, and the
same marker makes it findable now, before anything runs.

Both are found deterministically. Nothing here asks a model anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The marker the renderer emits for a step it could not bind. Matching on the
#: statement rather than the comment above it means a step a human later filled
#: in stops being reported the moment they delete this line.
_PENDING_RE = re.compile(r"^\s*return\s+['\"]pending['\"]\s*;", re.MULTILINE)

#: `Given('...', async function (...) {` — the step's pattern and where it starts.
_STEP_DEF_RE = re.compile(
    r"^(Given|When|Then)\(\s*(['\"])(?P<pattern>(?:\\.|(?!\2).)*)\2", re.MULTILINE
)


@dataclass
class Gap:
    """One step that will not verify anything as things stand."""

    text: str
    kind: str                    # "undefined" | "pending"
    keyword: str = ""
    file_path: str = ""
    line: int = 0

    @property
    def summary(self) -> str:
        where = f" ({self.file_path}:{self.line})" if self.file_path else ""
        return f"{self.kind}: {self.keyword}{self.text}{where}"


@dataclass
class CoverageReport:
    """What the dry run and the source scan found."""

    gaps: list[Gap] = field(default_factory=list)
    ran: bool = False
    skipped_reason: str = ""
    detail: str = ""

    @property
    def undefined(self) -> list[Gap]:
        return [g for g in self.gaps if g.kind == "undefined"]

    @property
    def pending(self) -> list[Gap]:
        return [g for g in self.gaps if g.kind == "pending"]

    @property
    def clean(self) -> bool:
        """Every step is bound to something that does work.

        `ran` is load-bearing. A dry run that could not start found no gaps,
        and "found no gaps" is not "there are none" — the distinction this
        platform has had to relearn in four separate places.
        """
        return self.ran and not self.gaps

    @property
    def verdict(self) -> str:
        if not self.ran:
            return "unverified"
        return "covered" if not self.gaps else "gaps"

    def summary(self) -> str:
        if not self.ran:
            return f"step coverage NOT checked ({self.skipped_reason or 'unavailable'})"
        if not self.gaps:
            return "step coverage: every step is bound"
        parts = []
        if self.undefined:
            parts.append(f"{len(self.undefined)} undefined")
        if self.pending:
            parts.append(f"{len(self.pending)} pending")
        return "step coverage: " + ", ".join(parts)


class StepCoverage:
    """Finds steps that are not yet real, before anything claims to be done."""

    def __init__(self, project_root: str | Path, runner: Any = None) -> None:
        self.root = Path(project_root)
        self.runner = runner

    # ------------------------------------------------------------------ #
    def scan(self, *, steps_dir: str = "tests/steps", timeout: int = 120) -> CoverageReport:
        report = CoverageReport()

        pending = self._pending_definitions(steps_dir)
        undefined, ran, reason, detail = self._undefined_steps(timeout)

        report.gaps = undefined + pending
        report.ran = ran
        report.skipped_reason = reason
        report.detail = detail

        # A pending scan alone is still evidence, and it costs nothing. If the
        # dry run could not start but a pending body was found, the answer is
        # unambiguously "not covered" — say so rather than "unverified".
        if not ran and pending:
            report.ran = True
            report.skipped_reason = ""
            report.detail = f"cucumber dry run unavailable ({reason}); source scan found gaps"
        return report

    # ------------------------------------------------------------------ #
    def _pending_definitions(self, steps_dir: str) -> list[Gap]:
        """Step definitions whose body is a TODO, found by reading them."""
        directory = self.root / steps_dir
        if not directory.is_dir():
            return []

        gaps: list[Gap] = []
        for path in sorted(directory.rglob("*.ts")):
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "return 'pending'" not in source and 'return "pending"' not in source:
                continue

            # Attribute each pending body to the definition it sits inside, by
            # taking the nearest preceding `Given(`/`When(`/`Then(`.
            definitions = [(m.start(), m.group(1), m.group("pattern")) for m in _STEP_DEF_RE.finditer(source)]
            relative = path.relative_to(self.root).as_posix()
            for marker in _PENDING_RE.finditer(source):
                owner = None
                for start, keyword, pattern in definitions:
                    if start < marker.start():
                        owner = (keyword, pattern)
                    else:
                        break
                if owner is None:
                    continue
                gaps.append(
                    Gap(
                        text=owner[1],
                        kind="pending",
                        keyword=f"{owner[0]} ",
                        file_path=relative,
                        line=source.count("\n", 0, marker.start()) + 1,
                    )
                )
        return gaps

    # ------------------------------------------------------------------ #
    def _undefined_steps(self, timeout: int) -> tuple[list[Gap], bool, str, str]:
        """Ask Cucumber. It is the only thing that knows for certain."""
        if self.runner is None:
            return [], False, "no command runner available", ""
        if not (self.root / "package.json").exists():
            return [], False, "not a node project", ""
        if not _cucumber_configured(self.root):
            return [], False, "no cucumber configuration — features are not run here", ""

        report_path = self.root / ".aiqa" / "dryrun.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if report_path.exists():
            report_path.unlink()

        relative = report_path.relative_to(self.root).as_posix()
        result = self.runner.run(
            command=["npx", "cucumber-js", "--dry-run", "--format", f"json:{relative}"],
            cwd=".",
            timeout=timeout,
        )
        payload = result.data if isinstance(result.data, dict) else {}
        output = f"{payload.get('stdout', '')}\n{payload.get('stderr', '')}".strip()

        if "exit_code" not in payload:
            return [], False, f"dry run could not start: {result.error[:160] or 'no output'}", output
        if not report_path.exists():
            # Cucumber refuses to start when a step pattern will not parse, and
            # prints the reason instead of a report. That reason is the most
            # useful thing available, so it is passed through whole.
            return [], False, f"dry run produced no report: {output[-200:]}", output

        try:
            raw = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [], False, f"dry run report unreadable: {exc}", output

        gaps: list[Gap] = []
        seen: set[str] = set()
        for feature in raw if isinstance(raw, list) else []:
            uri = str(feature.get("uri", ""))
            for element in feature.get("elements", []) or []:
                for step in element.get("steps", []) or []:
                    status = str((step.get("result") or {}).get("status", "")).lower()
                    if status != "undefined":
                        continue
                    name = str(step.get("name", ""))
                    if name in seen:
                        continue
                    seen.add(name)
                    gaps.append(
                        Gap(
                            text=name,
                            kind="undefined",
                            keyword=str(step.get("keyword", "")),
                            file_path=uri,
                            line=int(step.get("line", 0) or 0),
                        )
                    )
        return gaps, True, "", output


def _cucumber_configured(root: Path) -> bool:
    """Is there a runner that would read a .feature file here?

    Reuses the same question the execution layer asks. A repository with no
    cucumber configuration has no undefined steps in any meaningful sense —
    its features are not executed at all, which is a different problem and
    reported elsewhere.
    """
    from services.knowledge_service.indexer import _bdd_runner

    return _bdd_runner(root) == "cucumber-js"
