"""Standards / Governance Agent.

Coding standards are enforced by a **deterministic rule engine**, not by asking a
model to be careful. Rules come from ``configs/standards.yaml`` merged with the
project's ``.aiqa/standards.yaml``, so each team's conventions are data.

Two rule families:

* ``detect`` — a regex over the generated content, scoped by artifact type.
* ``kind: semantic`` — a named check implemented below, for things regex cannot
  see (is every scenario tagged? does this Page Object extend the house base
  class? is this helper a duplicate of one that already exists?).

A rule may declare an ``autofix``; safe fixes are applied and reported, and the
agent re-checks afterwards. Errors block the run; warnings travel with the diff
for the human to weigh.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import AgentName, ArtifactKind, Capability, Severity
from packages.aiqa_types.models import FileChange, StandardsReport, StandardsViolation

# Map an artifact kind / path onto the rule scopes used by `applies_to`.
_SCOPE_BY_KIND: dict[ArtifactKind, str] = {
    ArtifactKind.FEATURE: "features",
    ArtifactKind.STEP_DEFINITION: "steps",
    ArtifactKind.PAGE_OBJECT: "pages",
    ArtifactKind.FIXTURE: "fixtures",
    ArtifactKind.UTIL: "utils",
    ArtifactKind.TEST_DATA: "data",
    ArtifactKind.API_TEST: "tests",
    ArtifactKind.DB_CHECK: "tests",
    ArtifactKind.CONFIG: "config",
}

# Lines that are comments in the target language — never flag these.
_COMMENT_RE = re.compile(r"^\s*(//|#|\*|/\*)")


SEMANTIC_SYSTEM = """You are reviewing generated QA automation against conventions that no linter can check.

The code has ALREADY passed: TypeScript compilation, ESLint, Gherkin parsing, folder/naming checks,
and every regex and AST rule in the organization standard. Do not repeat any of that.

Judge ONLY the semantic questions listed, and only report a problem you can point at a specific line
for. Silence is the correct answer when the code is fine — do not invent findings.

Reply with ONE JSON object:
{"violations": [{"rule_id": str, "file_path": str, "line": int, "message": str, "severity": "error|warning"}]}"""


class StandardsAgent(BaseAgent):
    """Deterministic first, model last.

    The ordering is the whole point: a compiler answers "is this valid?" exactly
    and for free, so the model is never asked. It is consulted only for
    conventions that are genuinely semantic — "does this read like our code?" —
    and only once everything checkable has already passed.
    """

    name = AgentName.STANDARDS
    capability = Capability.CHEAP
    description = "Static-first standards enforcement; the LLM sees only what tools cannot decide."

    def progress(self, ctx: AgentContext) -> float:
        return 0.68

    async def run(self, ctx: AgentContext) -> None:
        bundle = ctx.code_bundle
        if bundle is None or not bundle.changes:
            ctx.standards_report = StandardsReport(run_id=ctx.run_id, passed=True, files_checked=0)
            return

        rules = [r for r in (ctx.standards.get("rules") or []) if isinstance(r, dict) and r.get("id")]
        report = StandardsReport(run_id=ctx.run_id, files_checked=len(bundle.changes), rules_applied=len(rules))

        # ---- 1. rule engine (regex + AST/semantic checks), with autofix ---- #
        for attempt in range(2):          # check -> autofix -> re-check
            violations: list[StandardsViolation] = []
            for change in bundle.changes:
                violations.extend(self._check_file(ctx, change, rules))

            if attempt == 0:
                fixed = self._autofix(ctx, bundle, violations, rules)
                report.autofixed = fixed
                if fixed:
                    ctx.note(f"auto-fixed {len(fixed)} standards violation(s): {', '.join(fixed[:6])}")
                    continue                # re-check with the fixes applied
            report.violations = violations
            break

        # ---- 2. toolchain checks (structure, Gherkin, tsc, eslint) --------- #
        static_report = self._run_static(ctx, bundle)
        if static_report is not None:
            report.violations.extend(static_report.violations)
            ctx.metadata["static_checks"] = {
                "ran": static_report.ran,
                "skipped": static_report.skipped,
                "errors": static_report.error_count,
                # tsc cannot run before the files exist, so this pass is always
                # "unverified" for TypeScript. The execution agent re-checks once
                # they are on disk; recording the verdict keeps the distinction
                # visible rather than letting a skip read as a pass.
                "verdict": static_report.verdict,
            }
            ctx.note(static_report.summary())

        report.passed = report.error_count == 0

        # ---- 3. semantic review, only if everything checkable is clean ----- #
        if report.passed:
            semantic = await self._semantic_review(ctx, bundle, rules)
            if semantic:
                report.violations.extend(semantic)
                report.passed = report.error_count == 0
        else:
            ctx.note(
                "skipping semantic AI review - static checks already found "
                f"{report.error_count} error(s), so a model call would add nothing"
            )

        ctx.standards_report = report

        summary = (
            f"standards: {report.error_count} error(s), {report.warning_count} warning(s) "
            f"across {report.files_checked} file(s)"
        )
        if report.passed:
            ctx.note(summary + " — passed")
        else:
            ctx.warn(summary + " — blocking")
            for violation in report.violations:
                if violation.severity in (Severity.ERROR, Severity.CRITICAL):
                    ctx.warn(f"  {violation.rule_id} {violation.file_path}:{violation.line} — {violation.message}")

        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = summary

    # ------------------------------------------------------------------ #
    def _run_static(self, ctx: AgentContext, bundle: Any) -> Any:
        """Structure + Gherkin always; tsc/ESLint only once the files are on disk."""
        from services.execution_service.static_validation import StaticValidationPipeline

        runner = ctx.tools.get("shell.run") if ctx.tools else None
        on_disk = bool(ctx.metadata.get("changes_applied"))
        try:
            pipeline = StaticValidationPipeline(ctx.project_root, ctx.standards, runner=runner)
            return pipeline.run(bundle.changes, compile_check=on_disk)
        except Exception as exc:  # noqa: BLE001 - a checker must not fail the run
            ctx.warn(f"static validation could not complete: {exc}")
            return None

    async def _semantic_review(
        self, ctx: AgentContext, bundle: Any, rules: list[dict[str, Any]]
    ) -> list[StandardsViolation]:
        """Ask a model only about rules no tool can express.

        Costs one cheap call, and only when the deterministic pass is already
        clean — so it never pays to re-discover a compile error.
        """
        semantic_rules = [
            rule
            for rule in rules
            if rule.get("kind") == "semantic" and not rule.get("check")
        ]
        if not semantic_rules:
            return []

        # Only review the files a human would actually scrutinise.
        reviewable = [
            change
            for change in bundle.changes
            if str(getattr(change.kind, "value", change.kind)) in ("page_object", "step_definition")
        ][:4]
        if not reviewable:
            return []

        user = "\n".join(
            [
                "## Conventions to judge",
                *[f"- [{r.get('id')}] {r.get('title') or r.get('message', '')}" for r in semantic_rules[:10]],
                "",
                "## House style",
                (ctx.repo_profile.conventions_summary if ctx.repo_profile else "none recorded")[:1500],
                "",
                "## Files",
                *[f"--- {c.path} ---\n{c.content[:4000]}" for c in reviewable],
            ]
        )
        raw = await self.ask_json(
            ctx, SEMANTIC_SYSTEM, user, task="standards.semantic", fallback={"violations": []}, max_tokens=1200
        )

        out: list[StandardsViolation] = []
        known_paths = {c.path for c in bundle.changes}
        for item in (raw or {}).get("violations", []) or []:
            if not isinstance(item, dict) or not item.get("message"):
                continue
            path = str(item.get("file_path", ""))
            if path not in known_paths:
                continue          # a finding about a file we did not generate is noise
            out.append(
                StandardsViolation(
                    rule_id=str(item.get("rule_id", "SEM-001")),
                    severity=_severity(item.get("severity", "warning")),
                    title="Semantic convention",
                    message=str(item["message"])[:300],
                    file_path=path,
                    line=int(item.get("line", 0) or 0),
                )
            )
        if out:
            ctx.note(f"semantic review raised {len(out)} finding(s) that tools could not detect")
        return out

    # ------------------------------------------------------------------ #
    def _check_file(
        self, ctx: AgentContext, change: FileChange, rules: list[dict[str, Any]]
    ) -> list[StandardsViolation]:
        scope = _SCOPE_BY_KIND.get(change.kind, "tests")
        violations: list[StandardsViolation] = []

        for rule in rules:
            applies = rule.get("applies_to")
            if applies:
                scopes = [applies] if isinstance(applies, str) else list(applies)
                if scope not in scopes:
                    continue

            severity = _severity(rule.get("severity", "warning"))
            if rule.get("kind") == "semantic":
                check_name = str(rule.get("check", ""))
                checker = SEMANTIC_CHECKS.get(check_name)
                if checker is None:
                    continue
                for line_no, snippet, detail in checker(change, ctx, rule):
                    violations.append(
                        StandardsViolation(
                            rule_id=str(rule["id"]),
                            severity=severity,
                            title=str(rule.get("title", check_name)),
                            message=detail or str(rule.get("message", "")),
                            file_path=change.path,
                            line=line_no,
                            snippet=snippet[:200],
                            suggestion=str(rule.get("message", "")),
                        )
                    )
                continue

            pattern = rule.get("detect")
            if not pattern:
                continue
            try:
                regex = re.compile(str(pattern))
            except re.error:
                continue
            for line_no, line in enumerate(change.content.splitlines(), start=1):
                if _COMMENT_RE.match(line):
                    continue
                if regex.search(line):
                    violations.append(
                        StandardsViolation(
                            rule_id=str(rule["id"]),
                            severity=severity,
                            title=str(rule.get("title", "")),
                            message=str(rule.get("message", "")),
                            file_path=change.path,
                            line=line_no,
                            snippet=line.strip()[:200],
                            suggestion=str(rule.get("message", "")),
                            autofixable=bool(rule.get("autofix")),
                        )
                    )
        return violations

    # ------------------------------------------------------------------ #
    def _autofix(
        self,
        ctx: AgentContext,
        bundle: Any,
        violations: list[StandardsViolation],
        rules: list[dict[str, Any]],
    ) -> list[str]:
        """Apply only mechanical, behaviour-preserving fixes."""
        fixers = {str(r["id"]): str(r.get("autofix", "")) for r in rules if r.get("autofix")}
        applied: list[str] = []

        by_file: dict[str, list[StandardsViolation]] = {}
        for violation in violations:
            if violation.rule_id in fixers:
                by_file.setdefault(violation.file_path, []).append(violation)

        for change in bundle.changes:
            file_violations = by_file.get(change.path)
            if not file_violations:
                continue
            lines = change.content.splitlines()
            drop: set[int] = set()
            for violation in file_violations:
                fix = fixers.get(violation.rule_id, "")
                index = violation.line - 1
                if not (0 <= index < len(lines)):
                    continue
                if fix == "remove_console_log" and "console.log" in lines[index]:
                    if lines[index].strip().startswith("console.log"):
                        drop.add(index)
                        applied.append(f"{violation.rule_id}@{change.path}:{violation.line}")
                elif fix == "strip_hard_wait" and re.search(r"waitForTimeout\s*\(", lines[index]):
                    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
                    lines[index] = (
                        f"{indent}// removed by QAgentic standards: replace with a web-first assertion"
                    )
                    applied.append(f"{violation.rule_id}@{change.path}:{violation.line}")
            if drop:
                lines = [line for index, line in enumerate(lines) if index not in drop]
            new_content = "\n".join(lines)
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            if new_content != change.content:
                change.content = new_content
                change.bytes = len(new_content.encode("utf-8"))
        return applied


# =========================================================================== #
# Semantic checks
# =========================================================================== #
SemanticResult = list[tuple[int, str, str]]
SemanticCheck = Callable[[FileChange, AgentContext, dict[str, Any]], SemanticResult]


def _check_scenario_tagged(change: FileChange, ctx: AgentContext, rule: dict[str, Any]) -> SemanticResult:
    out: SemanticResult = []
    lines = change.content.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*(Scenario|Scenario Outline|Example):", line):
            # Walk back over blank lines looking for a tag line.
            probe = index - 1
            while probe >= 0 and not lines[probe].strip():
                probe -= 1
            if probe < 0 or not lines[probe].strip().startswith("@"):
                out.append((index + 1, line.strip(), "Scenario has no tag on the preceding line."))
    return out


def _check_max_scenario_steps(change: FileChange, ctx: AgentContext, rule: dict[str, Any]) -> SemanticResult:
    limit = int(rule.get("max_steps", 15))
    out: SemanticResult = []
    lines = change.content.splitlines()
    current_line, current_name, count = 0, "", 0

    def flush() -> None:
        if current_name and count > limit:
            out.append((current_line, current_name, f"Scenario has {count} steps (limit {limit})."))

    for index, line in enumerate(lines, start=1):
        if re.match(r"^\s*(Scenario|Scenario Outline|Example):", line):
            flush()
            current_line, current_name, count = index, line.strip(), 0
        elif re.match(r"^\s*(Given|When|Then|And|But|\*)\s+", line):
            count += 1
    flush()
    return out


def _check_feature_has_context(change: FileChange, ctx: AgentContext, rule: dict[str, Any]) -> SemanticResult:
    content = change.content
    if re.search(r"^\s*Background:", content, re.MULTILINE):
        return []
    if re.search(r"^\s*Given\s+", content, re.MULTILINE):
        return []
    return [(1, "Feature:", "Feature declares no Background and no Given step.")]


def _check_page_methods_async(change: FileChange, ctx: AgentContext, rule: dict[str, Any]) -> SemanticResult:
    """Action methods that touch the page must be async."""
    out: SemanticResult = []
    lines = change.content.splitlines()
    method_re = re.compile(
        r"^\s{2,}(?!(?:private|public|protected)?\s*(?:get|set)\s)"
        r"(?:public\s+|protected\s+|private\s+)?(?P<async>async\s+)?"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::[^{;]+)?\{"
    )
    reserved = {"constructor", "if", "for", "while", "switch", "catch", "function", "return"}
    for index, line in enumerate(lines, start=1):
        match = method_re.match(line)
        if not match or match.group("async"):
            continue
        name = match.group("name")
        if name in reserved:
            continue
        # Only flag it if the body actually awaits/uses the page.
        body = "\n".join(lines[index : index + 12])
        if re.search(r"\b(await|this\.page|expect\()", body):
            out.append((index, line.strip(), f"Method '{name}' interacts with the page but is not async."))
    return out


def _check_no_duplicate_helpers(change: FileChange, ctx: AgentContext, rule: dict[str, Any]) -> SemanticResult:
    """Refuse to redefine a symbol the repository already exports."""
    if ctx.repo_profile is None:
        return []
    existing: dict[str, str] = {}
    for symbol in ctx.repo_profile.symbols:
        if symbol.kind in ("fixture", "util", "page_object") and symbol.file_path != change.path:
            existing.setdefault(symbol.name, symbol.file_path)
    if not existing:
        return []

    out: SemanticResult = []
    # A *definition* is an exported binding, or a function/class declaration.
    # A local `let loginPage: LoginPage;` is a variable that merely *holds* the
    # reused thing — flagging it would train people to ignore this rule.
    declaration_re = re.compile(
        r"^\s*(?:"
        r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+"
        r"|(?:abstract\s+)?class\s+"
        r"|(?:async\s+)?function\s+"
        r")(?P<name>[A-Za-z_$][\w$]*)"
    )
    for index, line in enumerate(change.content.splitlines(), start=1):
        match = declaration_re.match(line)
        if not match:
            continue
        name = match.group("name")
        if name in existing:
            out.append(
                (
                    index,
                    line.strip(),
                    f"'{name}' already exists in {existing[name]} — import and reuse it instead of redefining it.",
                )
            )
    return out


def _check_extends_base_page(change: FileChange, ctx: AgentContext, rule: dict[str, Any]) -> SemanticResult:
    """Project-specific rule: Page Objects must extend the house base class."""
    base = ((ctx.repo_profile.naming_conventions if ctx.repo_profile else {}) or {}).get(
        "page_object_base_class", "BasePage"
    )
    out: SemanticResult = []
    for index, line in enumerate(change.content.splitlines(), start=1):
        match = re.match(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)(?P<rest>.*)$", line)
        if not match:
            continue
        name = match.group("name")
        if name == base or not name.endswith("Page"):
            continue
        if f"extends {base}" not in match.group("rest"):
            out.append((index, line.strip(), f"Page Object '{name}' does not extend {base}."))
    return out


def _check_locator_strategy(change: FileChange, ctx: AgentContext, rule: dict[str, Any]) -> SemanticResult:
    """Discourage low-priority locator strategies when better ones are configured."""
    priority = (ctx.standards.get("locators", {}) or {}).get("strategy_priority", []) or []
    if not priority:
        return []
    weak = set(priority[max(0, len(priority) - 2):])   # the last two are the fallbacks
    if not weak:
        return []
    out: SemanticResult = []
    for index, line in enumerate(change.content.splitlines(), start=1):
        if _COMMENT_RE.match(line):
            continue
        if "xpath" in weak and re.search(r"xpath\s*=|locator\(\s*['\"]//", line):
            out.append((index, line.strip(), "XPath is the lowest-priority locator strategy for this organization."))
        elif "css" in weak and re.search(r"locator\(\s*['\"][.#\[]", line) and "getBy" not in line:
            out.append((index, line.strip(), "Prefer getByTestId/getByRole over a raw CSS selector."))
    return out


def _check_no_assertions_in_page_objects(
    change: FileChange, ctx: AgentContext, rule: dict[str, Any]
) -> SemanticResult:
    """Teams that keep assertions in steps want Page Objects to stay pure actions.

    Only flagged when the project opts in — plenty of good suites do the
    opposite and expose `expectX()` helpers on the Page Object.
    """
    out: SemanticResult = []
    for index, line in enumerate(change.content.splitlines(), start=1):
        if _COMMENT_RE.match(line):
            continue
        if re.search(r"\b(await\s+)?expect\s*\(", line):
            out.append((index, line.strip(), "Assertion found in a Page Object; this project keeps them in steps."))
    return out


SEMANTIC_CHECKS: dict[str, SemanticCheck] = {
    "no_assertions_in_page_objects": _check_no_assertions_in_page_objects,
    "scenario_tagged": _check_scenario_tagged,
    "max_scenario_steps": _check_max_scenario_steps,
    "feature_has_context": _check_feature_has_context,
    "page_methods_async": _check_page_methods_async,
    "no_duplicate_helpers": _check_no_duplicate_helpers,
    "extends_base_page": _check_extends_base_page,
    "locator_strategy": _check_locator_strategy,
}


def _severity(value: Any) -> Severity:
    try:
        return Severity(str(value).lower())
    except ValueError:
        return Severity.WARNING


def lint_existing_repo(ctx: AgentContext, paths: list[str] | None = None) -> StandardsReport:
    """Audit files already in the repository (not just generated ones).

    Exposed as a standalone command so a team can ask "how compliant is our
    existing suite?" without running a generation cycle.
    """
    from pathlib import Path

    from tools.filesystem.fs_tools import iter_source_files

    agent = StandardsAgent()
    rules = [r for r in (ctx.standards.get("rules") or []) if isinstance(r, dict) and r.get("id")]
    root = Path(ctx.project_root)
    report = StandardsReport(run_id=ctx.run_id, rules_applied=len(rules))
    violations: list[StandardsViolation] = []

    candidates: list[Path]
    if paths:
        candidates = [root / p for p in paths]
    else:
        candidates = [p for p in iter_source_files(root, {".ts", ".js", ".feature", ".py"})]

    for file in candidates:
        try:
            rel = file.relative_to(root).as_posix()
            text = file.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        change = FileChange(path=rel, kind=_kind_for_path(rel), content=text)
        violations.extend(agent._check_file(ctx, change, rules))
        report.files_checked += 1

    report.violations = violations
    report.passed = report.error_count == 0
    return report


def _kind_for_path(path: str) -> ArtifactKind:
    lowered = path.lower()
    if lowered.endswith(".feature"):
        return ArtifactKind.FEATURE
    name = PurePosixPath(lowered).name
    if "step" in lowered:
        return ArtifactKind.STEP_DEFINITION
    if "page" in lowered or "screen" in lowered:
        return ArtifactKind.PAGE_OBJECT
    if "fixture" in lowered:
        return ArtifactKind.FIXTURE
    if "util" in lowered or "helper" in lowered:
        return ArtifactKind.UTIL
    if name.endswith((".json", ".csv")):
        return ArtifactKind.TEST_DATA
    return ArtifactKind.API_TEST
