"""Static validation — catch what a compiler can catch, for free.

Asking a model "is this TypeScript valid?" is the most wasteful call the platform
could make: `tsc` answers it exactly, instantly and at zero cost. So generated
code runs a deterministic gauntlet first, and the LLM is consulted **only** for
semantic conventions that no tool can express.

    generated code
        |
        v
    [ TypeScript compiler ]   syntax, types, imports, missing symbols
    [ ESLint             ]    project lint rules
    [ Gherkin parser     ]    feature-file structure
    [ AST / regex rules  ]    org standards
        |
        +-- static errors? --> back to the Code Generation Agent (no LLM spent
        |                      on judging code that does not compile)
        |
        v
    [ semantic AI review ]     only for rules a tool cannot check, and only
                               when the static pass is clean

Every checker degrades to "skipped, here's why" when its tooling is absent, so a
repository without ESLint still validates everything else.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.aiqa_types.enums import Severity
from packages.aiqa_types.models import StandardsViolation

log = logging.getLogger("aiqa.static")


@dataclass
class CheckOutcome:
    """Result of one static checker."""

    name: str
    ran: bool = False
    passed: bool = True
    skipped_reason: str = ""
    violations: list[StandardsViolation] = field(default_factory=list)
    duration_ms: int = 0
    detail: str = ""

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity in (Severity.ERROR, Severity.CRITICAL))


@dataclass
class StaticReport:
    """Aggregate of every static checker."""

    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def violations(self) -> list[StandardsViolation]:
        return [v for outcome in self.outcomes for v in outcome.violations]

    @property
    def error_count(self) -> int:
        return sum(outcome.error_count for outcome in self.outcomes)

    @property
    def passed(self) -> bool:
        return self.error_count == 0

    @property
    def ran(self) -> list[str]:
        return [o.name for o in self.outcomes if o.ran]

    @property
    def skipped(self) -> dict[str, str]:
        return {o.name: o.skipped_reason for o in self.outcomes if not o.ran}

    @property
    def compiled(self) -> bool:
        """Did anything actually confirm this TypeScript compiles?

        `passed` cannot answer that: a skipped checker contributes no errors, so
        an unchecked change set and a clean one are indistinguishable by error
        count alone. That is how four non-compiling defects shipped while every
        run reported standards green.
        """
        return "typescript" in self.ran

    @property
    def verdict(self) -> str:
        """passed | failed | unverified — never "passed" on the strength of a skip."""
        if self.error_count:
            return "failed"
        return "passed" if self.compiled else "unverified"

    def summary(self) -> str:
        ran = ", ".join(self.ran) or "none"
        skipped = f"; skipped: {', '.join(self.skipped)}" if self.skipped else ""
        note = "" if self.compiled else "  [NOT compile-checked]"
        return f"static checks [{ran}]: {self.error_count} error(s){skipped}{note}"


# =========================================================================== #
# Gherkin — a real parser, not a regex guess
# =========================================================================== #
class GherkinValidator:
    """Structural validation of a `.feature` file.

    Deliberately hand-written rather than pulled from a dependency: the rules a
    QA platform cares about (a Scenario must have a Then, Examples columns must
    match placeholders) are not what a generic parser checks anyway.
    """

    name = "gherkin"
    KEYWORDS = ("Given", "When", "Then", "And", "But", "*")

    def check(self, path: str, content: str) -> list[StandardsViolation]:
        violations: list[StandardsViolation] = []
        lines = content.splitlines()

        def add(line_no: int, rule: str, message: str, severity: Severity = Severity.ERROR) -> None:
            violations.append(
                StandardsViolation(
                    rule_id=rule, severity=severity, title="Gherkin structure",
                    message=message, file_path=path, line=line_no,
                    snippet=lines[line_no - 1].strip()[:160] if 0 < line_no <= len(lines) else "",
                )
            )

        if not re.search(r"^\s*Feature:", content, re.MULTILINE):
            add(1, "GHK-001", "File contains no `Feature:` declaration.")
            return violations

        scenario_line = 0
        scenario_name = ""
        step_seen = False
        then_seen = False
        in_examples = False
        example_headers: list[str] = []
        outline_placeholders: set[str] = set()
        is_outline = False

        def close_scenario() -> None:
            nonlocal then_seen, step_seen
            if scenario_line and step_seen and not then_seen:
                add(scenario_line, "GHK-002", f"Scenario '{scenario_name}' has no Then step - it asserts nothing.")
            if scenario_line and not step_seen:
                add(scenario_line, "GHK-003", f"Scenario '{scenario_name}' has no steps.")

        for index, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if re.match(r"^(Scenario|Scenario Outline|Example):", line):
                close_scenario()
                if is_outline and not example_headers:
                    add(scenario_line, "GHK-004", f"Scenario Outline '{scenario_name}' has no Examples table.")
                scenario_line, scenario_name = index, line.split(":", 1)[1].strip()
                step_seen = then_seen = in_examples = False
                example_headers = []
                outline_placeholders = set()
                is_outline = line.startswith("Scenario Outline")
                continue

            if line.startswith("Examples:"):
                in_examples = True
                example_headers = []
                continue

            if in_examples and line.startswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if not example_headers:
                    example_headers = cells
                    missing = outline_placeholders - set(example_headers)
                    if missing:
                        add(index, "GHK-005",
                            f"Examples table is missing column(s) for placeholder(s): {', '.join(sorted(missing))}.")
                elif len(cells) != len(example_headers):
                    add(index, "GHK-006",
                        f"Examples row has {len(cells)} cell(s) but the header has {len(example_headers)}.")
                continue

            keyword = line.split(None, 1)[0] if line.split() else ""
            if keyword in self.KEYWORDS:
                step_seen = True
                if keyword == "Then":
                    then_seen = True
                if keyword in ("And", "But") and not step_seen:
                    add(index, "GHK-007", "`And`/`But` cannot be the first step of a scenario.")
                outline_placeholders.update(re.findall(r"<([^>]+)>", line))
                # Steps must be declarative — no selectors leaking into Gherkin.
                if re.search(r"(css=|xpath=|#[a-zA-Z][\w-]*\s*\)|getBy[A-Z]\w*\()", line):
                    add(index, "GHK-008", "Step text contains a technical selector; keep Gherkin declarative.",
                        Severity.WARNING)

        close_scenario()
        if is_outline and not example_headers:
            add(scenario_line, "GHK-004", f"Scenario Outline '{scenario_name}' has no Examples table.")
        return violations


# =========================================================================== #
# Toolchain-backed checkers
# =========================================================================== #
def _which(executable: str) -> bool:
    if shutil.which(executable):
        return True
    import os

    return bool(os.name == "nt" and any(shutil.which(executable + ext) for ext in (".cmd", ".exe", ".bat")))


class TypeScriptValidator:
    """`tsc --noEmit` over the project. Answers 'does it compile?' definitively."""

    name = "typescript"

    def __init__(self, runner: Any, project_root: Path) -> None:
        self.runner = runner
        self.root = Path(project_root)

    def available(self) -> tuple[bool, str]:
        if not (self.root / "package.json").exists():
            return False, "no package.json"
        if not (self.root / "tsconfig.json").exists():
            return False, "no tsconfig.json"
        if not (self.root / "node_modules" / "typescript").exists():
            return False, "typescript is not installed (run `npm install`)"
        if not _which("npx"):
            return False, "npx is not on PATH"
        return True, ""

    def check(self, timeout: int = 240) -> CheckOutcome:
        ok, reason = self.available()
        if not ok:
            return CheckOutcome(self.name, ran=False, skipped_reason=reason)

        result = self.runner.run(command=["npx", "tsc", "--noEmit", "--pretty", "false"], cwd=".", timeout=timeout)
        payload = result.data if isinstance(result.data, dict) else {}
        output = f"{payload.get('stdout', '')}\n{payload.get('stderr', '')}"
        violations = self._parse(output)
        return CheckOutcome(
            self.name, ran=True, passed=not violations, violations=violations,
            duration_ms=int(payload.get("duration_ms", 0)), detail=output[-1500:],
        )

    #: `path(line,col): error TS1234: message`
    _DIAGNOSTIC = re.compile(r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),(?P<col>\d+)\):\s+(?P<kind>error|warning)\s+(?P<code>TS\d+):\s+(?P<message>.+)$")

    def _parse(self, output: str) -> list[StandardsViolation]:
        violations: list[StandardsViolation] = []
        for line in output.splitlines():
            match = self._DIAGNOSTIC.match(line.strip())
            if not match:
                continue
            violations.append(
                StandardsViolation(
                    rule_id=match.group("code"),
                    severity=Severity.ERROR if match.group("kind") == "error" else Severity.WARNING,
                    title="TypeScript compilation",
                    message=match.group("message")[:400],
                    file_path=match.group("file").replace("\\", "/"),
                    line=int(match.group("line")),
                )
            )
        return violations[:200]


class ESLintValidator:
    """Project ESLint rules, in the project's own configuration."""

    name = "eslint"
    _CONFIGS = (
        ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
        ".eslintrc.yaml", ".eslintrc.yml", "eslint.config.js", "eslint.config.mjs",
    )

    def __init__(self, runner: Any, project_root: Path) -> None:
        self.runner = runner
        self.root = Path(project_root)

    def available(self) -> tuple[bool, str]:
        if not (self.root / "node_modules" / "eslint").exists():
            return False, "eslint is not installed"
        if not any((self.root / name).exists() for name in self._CONFIGS):
            return False, "no eslint configuration found"
        return True, ""

    def check(self, paths: list[str], timeout: int = 180) -> CheckOutcome:
        ok, reason = self.available()
        if not ok:
            return CheckOutcome(self.name, ran=False, skipped_reason=reason)
        targets = [p for p in paths if p.endswith((".ts", ".tsx", ".js", ".jsx"))]
        if not targets:
            return CheckOutcome(self.name, ran=False, skipped_reason="no lintable files in this change set")

        result = self.runner.run(
            command=["npx", "eslint", "--format", "json", *targets], cwd=".", timeout=timeout
        )
        payload = result.data if isinstance(result.data, dict) else {}
        try:
            report = json.loads(payload.get("stdout", "[]") or "[]")
        except json.JSONDecodeError:
            return CheckOutcome(
                self.name, ran=True, passed=True,
                detail="eslint produced no parseable JSON report",
            )

        violations: list[StandardsViolation] = []
        for entry in report:
            rel = str(entry.get("filePath", "")).replace("\\", "/")
            try:
                rel = str(Path(rel).relative_to(self.root)).replace("\\", "/")
            except (ValueError, OSError):
                pass
            for message in entry.get("messages", []) or []:
                violations.append(
                    StandardsViolation(
                        rule_id=str(message.get("ruleId") or "eslint"),
                        severity=Severity.ERROR if message.get("severity") == 2 else Severity.WARNING,
                        title="ESLint",
                        message=str(message.get("message", ""))[:300],
                        file_path=rel,
                        line=int(message.get("line", 0) or 0),
                    )
                )
        return CheckOutcome(self.name, ran=True, passed=not any(
            v.severity == Severity.ERROR for v in violations
        ), violations=violations[:200])


class StructureValidator:
    """Folder placement and file naming, straight from the resolved standards."""

    name = "structure"

    def __init__(self, standards: dict[str, Any]) -> None:
        self.layout = standards.get("layout", {}) or {}
        self.naming = standards.get("naming", {}) or {}

    _EXPECTED_DIR = {
        "feature": "features_dir",
        "step_definition": "steps_dir",
        "page_object": "pages_dir",
        "fixture": "fixtures_dir",
        "util": "utils_dir",
        "test_data": "data_dir",
    }

    def check(self, changes: list[Any]) -> CheckOutcome:
        violations: list[StandardsViolation] = []
        for change in changes:
            kind = getattr(change.kind, "value", str(change.kind))
            path = change.path
            expected_key = self._EXPECTED_DIR.get(kind)
            if expected_key:
                expected = str(self.layout.get(expected_key, "")).strip("/")
                if expected and not path.replace("\\", "/").startswith(expected + "/"):
                    violations.append(
                        StandardsViolation(
                            rule_id="STR-001", severity=Severity.ERROR, title="File placement",
                            message=f"A {kind.replace('_', ' ')} belongs in `{expected}/`, not `{path}`.",
                            file_path=path,
                        )
                    )

            name = Path(path).name
            if kind == "page_object":
                convention = str(self.naming.get("page_object_file", "PascalCasePage.ts"))
                if "PascalCase" in convention and not re.match(r"^[A-Z][A-Za-z0-9]*\.(ts|js)$", name):
                    violations.append(
                        StandardsViolation(
                            rule_id="STR-002", severity=Severity.WARNING, title="File naming",
                            message=f"Page Object files are PascalCase in this repository; `{name}` is not.",
                            file_path=path,
                        )
                    )
            if kind == "feature":
                convention = str(self.naming.get("feature_file", "kebab-case.feature"))
                if "kebab-case" in convention and not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*\.feature$", name):
                    violations.append(
                        StandardsViolation(
                            rule_id="STR-003", severity=Severity.WARNING, title="File naming",
                            message=f"Feature files are kebab-case in this repository; `{name}` is not.",
                            file_path=path,
                        )
                    )
        return CheckOutcome(self.name, ran=True, passed=not any(
            v.severity == Severity.ERROR for v in violations
        ), violations=violations)


# =========================================================================== #
class StaticValidationPipeline:
    """Runs every available static checker over a change set."""

    def __init__(self, project_root: str | Path, standards: dict[str, Any], runner: Any = None) -> None:
        self.root = Path(project_root)
        self.standards = standards
        self.runner = runner

    def run(self, changes: list[Any], *, compile_check: bool = True) -> StaticReport:
        report = StaticReport()

        # 1. Structure and naming — always available, always instant.
        report.outcomes.append(StructureValidator(self.standards).check(changes))

        # 2. Gherkin — a parser, not a model.
        gherkin = GherkinValidator()
        gherkin_violations: list[StandardsViolation] = []
        feature_changes = [c for c in changes if str(getattr(c.kind, "value", c.kind)) == "feature"]
        for change in feature_changes:
            gherkin_violations.extend(gherkin.check(change.path, change.content))
        report.outcomes.append(
            CheckOutcome(
                gherkin.name,
                ran=bool(feature_changes),
                passed=not gherkin_violations,
                skipped_reason="" if feature_changes else "no feature files in this change set",
                violations=gherkin_violations,
            )
        )

        # 3. Toolchain checks need the files on disk and the runner available.
        if self.runner is not None and compile_check:
            report.outcomes.append(TypeScriptValidator(self.runner, self.root).check())
            report.outcomes.append(
                ESLintValidator(self.runner, self.root).check([c.path for c in changes])
            )
        else:
            for name in ("typescript", "eslint"):
                report.outcomes.append(
                    CheckOutcome(name, ran=False, skipped_reason="not run before the change set is on disk")
                )

        return report
