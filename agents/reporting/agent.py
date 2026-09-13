"""Reporting Agent.

Produces the artifact a human actually reads: what was built, what passed, what
failed and *why*, what was repaired, what looks like a product defect, and what
it cost. The numbers are computed deterministically — only the narrative
paragraph is model-written, so a hallucination cannot misstate a result.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import AgentName, Capability
from packages.aiqa_types.models import RunReport

SYSTEM = """You are writing the narrative summary of an automated QA run for a QA lead.

You receive verified facts. Write 2-4 short paragraphs: what was automated, the outcome, and what needs
human attention. Lead with anything that looks like a product defect. Be direct and specific — no filler,
no restating the numbers the reader can already see in the table, no marketing tone.
Plain prose only, no headings, no JSON."""


class ReportingAgent(BaseAgent):
    name = AgentName.REPORTING
    capability = Capability.CHEAP
    description = "Assembles the run report and delivers it to Slack/Teams."

    def progress(self, ctx: AgentContext) -> float:
        return 0.97

    async def run(self, ctx: AgentContext) -> None:
        facts = _facts(ctx)

        narrative = ""
        response = await self.ask(
            ctx, SYSTEM, _facts_prompt(facts), task="reporting.summarize", max_tokens=900, temperature=0.2
        )
        narrative = (response.text or "").strip()
        if len(narrative) < 40:
            narrative = _fallback_narrative(facts)

        report = RunReport(
            run_id=ctx.run_id,
            title=f"AI QA run — {facts['feature']}",
            headline=_headline(facts),
            scenarios_designed=facts["scenarios"],
            files_changed=facts["files_changed"],
            tests_total=facts["total"],
            tests_passed=facts["passed"],
            tests_failed=facts["failed"],
            heals_applied=facts["heals_verified"],
            product_defects=facts["product_defects"],
            cost_usd=facts["cost_usd"],
            duration_s=facts["duration_s"],
            next_actions=_next_actions(ctx, facts),
        )
        report.markdown = _markdown(ctx, facts, narrative, report)
        report.html = _html(report, facts)
        ctx.report = report

        # Everything this run learned becomes cheaper next time.
        self._persist_knowledge(ctx)

        self._persist_artifact(ctx, report)
        self._notify(ctx, report, facts)

        ctx.note(f"report ready: {report.headline}")
        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = report.headline

    # ------------------------------------------------------------------ #
    def _persist_knowledge(self, ctx: AgentContext) -> None:
        """Write this run's scenarios into the Test Knowledge Store and QA graph.

        This is what makes the platform get cheaper with use: the next request
        for a related feature finds prior work instead of paying to redesign it.
        """
        from services.knowledge_service.test_knowledge import (
            QAKnowledgeGraph,
            TestKnowledge,
            TestKnowledgeStore,
        )

        if ctx.test_plan is None:
            return
        try:
            store = ctx.test_knowledge or TestKnowledgeStore(ctx.project.id)
            graph = ctx.knowledge_graph or QAKnowledgeGraph(ctx.project.id)

            changes = ctx.code_bundle.changes if ctx.code_bundle else []
            feature_files = [c.path for c in changes if str(getattr(c.kind, "value", c.kind)) == "feature"]
            step_files = [c.path for c in changes if str(getattr(c.kind, "value", c.kind)) == "step_definition"]
            page_objects = [
                Path(c.path).stem for c in changes if str(getattr(c.kind, "value", c.kind)) == "page_object"
            ] + (ctx.test_plan.page_objects_reused or [])

            statuses = {
                (case.test_id or case.name): case.status.value
                for case in (ctx.execution.results if ctx.execution else [])
            }
            healed = {heal.test_id for heal in ctx.heals if heal.verified}
            # Which routes a test *touches* is the basis of the coverage report,
            # so it has to mean something. Every route exploration happened to
            # visit is not it: attributing all of them to every scenario made a
            # single run look like 86% route coverage. A test touches the route
            # its page object drives, which the generated file states outright.
            routes = _routes_of(changes)
            components = list((ctx.application_map.components or {}).keys()) if ctx.application_map else []

            for feature in ctx.test_plan.features:
                for scenario in feature.scenarios:
                    knowledge = TestKnowledge(
                        test_id=scenario.test_id or scenario.name,
                        name=scenario.name,
                        feature=feature.name,
                        requirement=ctx.requirement.title if ctx.requirement else ctx.instruction[:120],
                        tags=scenario.tags,
                        feature_file=feature_files[0] if feature_files else "",
                        step_file=step_files[0] if step_files else "",
                        page_objects=sorted(set(page_objects)),
                        fixtures=list(ctx.test_plan.fixtures_reused or []),
                        components=components[:10],
                        # Checks are structured objects now; the knowledge store
                        # wants a stable label it can show and match on.
                        apis=[_api_label(c) for c in (ctx.test_plan.api_checks or [])],
                        db_tables=[_db_label(c) for c in (ctx.test_plan.db_checks or [])],
                        routes=[r for r in routes if r][:6],
                        steps=[step.render() for step in scenario.steps],
                        covers_criteria=_criteria_text(scenario, ctx),
                    )
                    status = statuses.get(knowledge.test_id, "")
                    if status:
                        knowledge.runs = 1
                        knowledge.passes = 1 if status == "passed" else 0
                        knowledge.failures = 1 if status in ("failed", "timed_out") else 0
                        knowledge.last_status = status
                    if knowledge.test_id in healed:
                        knowledge.heals = 1
                    store.put(knowledge)
                    graph.ingest_test(knowledge)

            for analysis in ctx.analyses:
                graph.ingest_failure(analysis.test_id, analysis.category.value, analysis.root_cause[:120])
            graph.save()

            # Feed execution outcomes back into locator confidence.
            if ctx.application_map is not None and ctx.execution is not None:
                for case in ctx.execution.failures:
                    if case.failed_locator:
                        ctx.application_map.record_locator_outcome(case.failed_locator, success=False)
                ctx.application_map.save(ctx.project_root)

            ctx.note(
                f"knowledge updated: {store.stats()['tests_known']} test(s) known, "
                f"graph has {graph.coverage()['nodes']} node(s)"
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail a run
            ctx.warn(f"could not persist run knowledge: {exc}")

    def _persist_artifact(self, ctx: AgentContext, report: RunReport) -> None:

        from configs.settings import get_settings
        from packages.aiqa_types.models import new_id
        from services.observability.db import session_scope
        from services.observability.models import ArtifactRow

        try:
            directory = get_settings().artifacts_dir / ctx.run_id
            directory.mkdir(parents=True, exist_ok=True)
            md_path = directory / "report.md"
            html_path = directory / "report.html"
            md_path.write_text(report.markdown, encoding="utf-8")
            html_path.write_text(report.html, encoding="utf-8")

            with session_scope() as session:
                for path, kind, content_type in (
                    (md_path, "report", "text/markdown"),
                    (html_path, "report", "text/html"),
                ):
                    session.add(
                        ArtifactRow(
                            id=new_id("art"), run_id=ctx.run_id, project_id=ctx.project.id,
                            kind=kind, name=path.name, path=str(path),
                            content_type=content_type, size_bytes=path.stat().st_size,
                        )
                    )
            ctx.metadata["report_path"] = str(md_path)
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"could not persist the report artifact: {exc}")

    def _notify(self, ctx: AgentContext, report: RunReport, facts: dict[str, Any]) -> None:
        if not ctx.metadata.get("notify", True):
            return
        status = (
            "failed" if facts["failed"] else "partial" if facts["product_defects"] else "succeeded"
        )
        payload = {
            "project": ctx.project.name,
            "scenarios_designed": report.scenarios_designed,
            "files_changed": report.files_changed,
            "tests_total": report.tests_total,
            "tests_passed": report.tests_passed,
            "tests_failed": report.tests_failed,
            "heals_applied": report.heals_applied,
            "cost_usd": report.cost_usd,
            "duration_s": report.duration_s,
            "failures": facts["failure_lines"][:6],
            "product_defects": report.product_defects[:5],
            "run_url": ctx.metadata.get("run_url", ""),
        }
        for tool_name in ("slack.notify", "teams.notify"):
            result = self.tool(
                ctx, tool_name, title=report.title, text=report.headline, status=status, payload=payload
            )
            if result.ok:
                ctx.note(f"summary delivered via {tool_name.split('.')[0]}")
            elif result.rule != "notify.unconfigured":
                ctx.warn(f"{tool_name} failed: {result.error[:160]}")


# =========================================================================== #
# Deterministic fact assembly
# =========================================================================== #
def _facts(ctx: AgentContext) -> dict[str, Any]:
    execution = ctx.execution
    plan = ctx.test_plan
    bundle = ctx.code_bundle
    cost = ctx.tracker.cost_summary() if ctx.tracker and hasattr(ctx.tracker, "cost_summary") else None

    failure_lines: list[str] = []
    if execution:
        for case in execution.failures[:10]:
            first_line = (case.error_message or "").splitlines()
            failure_lines.append(
                f"{case.test_id or case.name}: {first_line[0][:150] if first_line else case.status.value}"
            )

    analysis_by_category: dict[str, int] = {}
    for analysis in ctx.analyses:
        analysis_by_category[analysis.category.value] = analysis_by_category.get(analysis.category.value, 0) + 1

    return {
        "feature": (ctx.requirement.title if ctx.requirement else ctx.instruction[:80]),
        "instruction": ctx.instruction,
        "mode": ctx.mode.value,
        "project": ctx.project.name,
        "scenarios": plan.scenario_count if plan else 0,
        "features": len(plan.features) if plan else 0,
        "files_changed": len(bundle.changes) if bundle else 0,
        "file_list": [{"path": c.path, "kind": c.kind.value, "bytes": c.bytes} for c in (bundle.changes if bundle else [])],
        "reused_pages": plan.page_objects_reused if plan else [],
        "reused_fixtures": plan.fixtures_reused if plan else [],
        "total": execution.total if execution else 0,
        "passed": execution.passed if execution else 0,
        "failed": execution.failed if execution else 0,
        "skipped": execution.skipped if execution else 0,
        "flaky": execution.flaky if execution else 0,
        "pass_rate": round((execution.pass_rate * 100) if execution else 0.0, 1),
        "exec_duration_s": round((execution.duration_ms / 1000) if execution else 0.0, 1),
        "execution_blocked": ctx.metadata.get("execution_blocked", ""),
        "failure_lines": failure_lines,
        "analysis_by_category": analysis_by_category,
        "product_defects": [
            f"{a.test_id}: {a.defect_summary or a.root_cause}"[:220]
            for a in ctx.analyses
            if a.is_product_defect
        ],
        "heals_proposed": len(ctx.heals),
        "heals_verified": sum(1 for h in ctx.heals if h.verified),
        "heals_reverted": sum(1 for h in ctx.heals if h.reverted),
        "standards_errors": ctx.standards_report.error_count if ctx.standards_report else 0,
        "standards_warnings": ctx.standards_report.warning_count if ctx.standards_report else 0,
        "standards_autofixed": len(ctx.standards_report.autofixed) if ctx.standards_report else 0,
        "locators_verified": ctx.metadata.get("locators_verified", False),
        "explored_pages": len(ctx.exploration.snapshots) if ctx.exploration else 0,
        "cost_usd": round(cost.total_cost_usd, 6) if cost else round(ctx.budget.spent_usd, 6),
        "tokens": cost.total_tokens if cost else ctx.budget.used_tokens,
        "llm_calls": cost.llm_calls if cost else ctx.budget.calls,
        "cost_by_agent": dict(cost.by_agent) if cost else {},
        "cost_by_model": dict(cost.by_model) if cost else {},
        "duration_s": round(ctx.metadata.get("duration_s", 0.0), 1),
        "branch": ctx.metadata.get("branch", ""),
        "commit": (ctx.metadata.get("commit") or {}).get("sha", ""),
        "warnings": ctx.warnings[:12],
        "iteration": ctx.iteration,
    }


def _headline(facts: dict[str, Any]) -> str:
    if facts["execution_blocked"]:
        return (
            f"Generated {facts['scenarios']} scenario(s) in {facts['files_changed']} file(s); "
            f"execution blocked ({facts['execution_blocked'][:80]})"
        )
    if facts["total"] == 0:
        return f"Generated {facts['scenarios']} scenario(s) across {facts['files_changed']} file(s); not executed"
    parts = [f"{facts['passed']}/{facts['total']} tests passing ({facts['pass_rate']}%)"]
    if facts["heals_verified"]:
        parts.append(f"{facts['heals_verified']} test(s) self-healed")
    if facts["product_defects"]:
        parts.append(f"{len(facts['product_defects'])} suspected product defect(s)")
    if facts["failed"]:
        parts.append(f"{facts['failed']} still failing")
    return " · ".join(parts)


def _next_actions(ctx: AgentContext, facts: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if facts["product_defects"]:
        actions.append("Triage the suspected product defect(s) with the product owner and raise tickets")
    if facts["failed"]:
        actions.append(f"Investigate {facts['failed']} failing test(s) that could not be repaired automatically")
    if facts["standards_errors"]:
        actions.append(f"Resolve {facts['standards_errors']} coding-standard error(s) before merging")
    if not facts["locators_verified"] and facts["files_changed"]:
        actions.append("Verify TODO(aiqa) locators against the running application")
    if facts["execution_blocked"]:
        actions.append("Install project dependencies (`npm install`, `npx playwright install`) and re-run")
    if facts["heals_reverted"]:
        actions.append(f"Review {facts['heals_reverted']} repair(s) that were reverted after failing verification")
    if facts["branch"] and not facts["commit"]:
        actions.append(f"Review the diff on branch {facts['branch']} and commit when satisfied")
    if not actions:
        actions.append("Review the generated tests and merge")
    return actions


def _facts_prompt(facts: dict[str, Any]) -> str:
    lines = [
        f"Feature automated: {facts['feature']}",
        f"Run mode: {facts['mode']}",
        f"Scenarios designed: {facts['scenarios']} across {facts['features']} feature file(s)",
        f"Files written: {facts['files_changed']}",
        f"Existing assets reused: pages={facts['reused_pages']}, fixtures={facts['reused_fixtures']}",
        f"Locators verified against a live application: {facts['locators_verified']} "
        f"({facts['explored_pages']} page(s) crawled)",
        f"Standards: {facts['standards_errors']} error(s), {facts['standards_warnings']} warning(s), "
        f"{facts['standards_autofixed']} auto-fixed",
    ]
    if facts["execution_blocked"]:
        lines.append(f"Execution could not run: {facts['execution_blocked']}")
    else:
        lines.append(
            f"Test results: {facts['passed']} passed, {facts['failed']} failed, "
            f"{facts['skipped']} skipped, {facts['flaky']} flaky of {facts['total']}"
        )
    if facts["failure_lines"]:
        lines.append("Failures: " + " | ".join(facts["failure_lines"][:6]))
    if facts["analysis_by_category"]:
        lines.append(
            "Failure categories: " + ", ".join(f"{k}={v}" for k, v in facts["analysis_by_category"].items())
        )
    if facts["product_defects"]:
        lines.append("SUSPECTED PRODUCT DEFECTS: " + " | ".join(facts["product_defects"]))
    if facts["heals_proposed"]:
        lines.append(
            f"Self-healing: {facts['heals_proposed']} proposed, {facts['heals_verified']} verified, "
            f"{facts['heals_reverted']} reverted"
        )
    if facts["warnings"]:
        lines.append("Warnings raised during the run: " + " | ".join(facts["warnings"][:6]))
    lines.append(f"Cost: ${facts['cost_usd']:.4f} over {facts['llm_calls']} LLM call(s), {facts['tokens']} tokens")
    return "\n".join(lines) + "\n\nWrite the summary."


def _fallback_narrative(facts: dict[str, Any]) -> str:
    if facts["execution_blocked"]:
        return (
            f"Automation for {facts['feature']} was designed and written "
            f"({facts['scenarios']} scenarios, {facts['files_changed']} files), but the suite could not be "
            f"executed: {facts['execution_blocked']}. The generated code is in the workspace for review."
        )
    if facts["product_defects"]:
        return (
            f"Automation for {facts['feature']} is in place and {facts['passed']} of {facts['total']} tests pass. "
            f"{len(facts['product_defects'])} failure(s) look like genuine application defects rather than test "
            f"problems and were deliberately not auto-repaired. Those need human triage first."
        )
    return (
        f"Automation for {facts['feature']} was designed, generated and executed: "
        f"{facts['scenarios']} scenarios across {facts['files_changed']} files, "
        f"{facts['passed']} of {facts['total']} passing. "
        + (f"{facts['heals_verified']} test(s) were repaired automatically and verified. " if facts["heals_verified"] else "")
        + "Review the diff before merging."
    )


# =========================================================================== #
# Rendering
# =========================================================================== #
def _markdown(ctx: AgentContext, facts: dict[str, Any], narrative: str, report: RunReport) -> str:
    lines: list[str] = [
        f"# {report.title}",
        "",
        f"**{report.headline}**",
        "",
        narrative,
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Project | {facts['project']} |",
        f"| Run mode | {facts['mode']} |",
        f"| Scenarios designed | {facts['scenarios']} |",
        f"| Files written | {facts['files_changed']} |",
        f"| Tests | {facts['passed']} passed / {facts['failed']} failed / {facts['skipped']} skipped of {facts['total']} |",
        f"| Pass rate | {facts['pass_rate']}% |",
        f"| Flaky | {facts['flaky']} |",
        f"| Self-heals verified | {facts['heals_verified']} of {facts['heals_proposed']} proposed |",
        f"| Standards | {facts['standards_errors']} error(s), {facts['standards_warnings']} warning(s) |",
        f"| Locators verified live | {'yes' if facts['locators_verified'] else 'no'} |",
        f"| LLM cost | ${facts['cost_usd']:.4f} ({facts['llm_calls']} calls, {facts['tokens']:,} tokens) |",
        f"| Duration | {facts['duration_s']}s (tests {facts['exec_duration_s']}s) |",
    ]
    if facts["branch"]:
        lines.append(f"| Branch | `{facts['branch']}` |")
    if facts["commit"]:
        lines.append(f"| Commit | `{facts['commit']}` |")

    if facts["product_defects"]:
        lines += ["", "## ⚠ Suspected product defects", ""]
        lines += [f"- {defect}" for defect in facts["product_defects"]]
        lines.append("")
        lines.append("_These were classified as application behaviour, not test problems, and were not auto-repaired._")

    if facts["file_list"]:
        lines += ["", "## Files written", "", "| File | Kind | Size |", "| --- | --- | --- |"]
        lines += [f"| `{f['path']}` | {f['kind']} | {f['bytes']} B |" for f in facts["file_list"]]

    if ctx.test_plan:
        lines += ["", "## Scenarios", ""]
        for feature in ctx.test_plan.features:
            lines.append(f"**{feature.name}** (`{feature.file_name}`)")
            lines.append("")
            for scenario in feature.scenarios:
                lines.append(
                    f"- `{scenario.test_id}` {scenario.name} "
                    f"— {' '.join(scenario.tags)}{' · data-driven' if scenario.examples else ''}"
                )
            lines.append("")

    if facts["failure_lines"]:
        lines += ["", "## Failures", ""]
        for analysis in ctx.analyses:
            lines.append(
                f"- **{analysis.test_id}** — `{analysis.category.value}` "
                f"(confidence {analysis.confidence:.2f}): {analysis.root_cause}"
            )
            if analysis.recommended_action:
                lines.append(f"  - Action: {analysis.recommended_action}")
        if not ctx.analyses:
            lines += [f"- {line}" for line in facts["failure_lines"]]

    if ctx.heals:
        lines += ["", "## Automated repairs", "", "| Test | Strategy | Confidence | Verified |", "| --- | --- | --- | --- |"]
        for heal in ctx.heals:
            state = "✅ yes" if heal.verified else ("↩ reverted" if heal.reverted else "⚠ unverified")
            lines.append(f"| `{heal.test_id}` | {heal.strategy.value} | {heal.confidence:.2f} | {state} |")

    if facts["cost_by_agent"]:
        lines += ["", "## Cost by agent", "", "| Agent | Cost |", "| --- | --- |"]
        lines += [
            f"| {agent} | ${amount:.6f} |"
            for agent, amount in sorted(facts["cost_by_agent"].items(), key=lambda kv: -kv[1])
        ]

    if facts["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in facts["warnings"]]

    lines += ["", "## Next actions", ""] + [f"{i}. {a}" for i, a in enumerate(report.next_actions, start=1)]
    lines += ["", "---", f"_Run `{ctx.run_id}` · generated by AI QA Engineer_"]
    return "\n".join(lines) + "\n"


def _html(report: RunReport, facts: dict[str, Any]) -> str:
    """Self-contained HTML report — openable from CI artifacts with no server."""
    def esc(value: Any) -> str:
        return html.escape(str(value))

    rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>"
        for k, v in [
            ("Project", facts["project"]),
            ("Scenarios designed", facts["scenarios"]),
            ("Files written", facts["files_changed"]),
            ("Tests", f"{facts['passed']} passed / {facts['failed']} failed of {facts['total']}"),
            ("Pass rate", f"{facts['pass_rate']}%"),
            ("Self-heals verified", facts["heals_verified"]),
            ("LLM cost", f"${facts['cost_usd']:.4f}"),
            ("Duration", f"{facts['duration_s']}s"),
        ]
    )
    defects = "".join(f"<li>{esc(d)}</li>" for d in report.product_defects)
    actions = "".join(f"<li>{esc(a)}</li>" for a in report.next_actions)
    status_class = "fail" if facts["failed"] else "warn" if report.product_defects else "pass"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{esc(report.title)}</title>
<style>
 :root {{ color-scheme: light dark; --fg:#1a1a1a; --bg:#fff; --muted:#666; --line:#e3e3e3;
          --pass:#2eb886; --fail:#d63b3b; --warn:#e8a33d; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e8e8e8; --bg:#161616; --muted:#9a9a9a; --line:#2e2e2e; }} }}
 body {{ font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif; color:var(--fg); background:var(--bg);
         margin:0; padding:2rem; max-width:56rem; }}
 h1 {{ font-size:1.5rem; margin:0 0 .25rem; }}
 .headline {{ font-weight:600; padding:.6rem .9rem; border-radius:6px; display:inline-block; margin:.5rem 0 1.5rem; }}
 .pass {{ background:color-mix(in srgb, var(--pass) 18%, transparent); }}
 .fail {{ background:color-mix(in srgb, var(--fail) 18%, transparent); }}
 .warn {{ background:color-mix(in srgb, var(--warn) 20%, transparent); }}
 table {{ border-collapse:collapse; width:100%; margin:1rem 0; }}
 td, th {{ border-bottom:1px solid var(--line); padding:.5rem .6rem; text-align:left; }}
 td:first-child {{ color:var(--muted); width:14rem; }}
 h2 {{ font-size:1.05rem; margin-top:2rem; border-bottom:1px solid var(--line); padding-bottom:.3rem; }}
 footer {{ margin-top:2.5rem; color:var(--muted); font-size:.85rem; }}
</style></head><body>
<h1>{esc(report.title)}</h1>
<div class="headline {status_class}">{esc(report.headline)}</div>
<table>{rows}</table>
{'<h2>Suspected product defects</h2><ul>' + defects + '</ul>' if defects else ''}
<h2>Next actions</h2><ul>{actions}</ul>
<footer>Run {esc(report.run_id)} · generated by AI QA Engineer</footer>
</body></html>
"""


def _criteria_text(scenario: Any, ctx: AgentContext) -> list[str]:
    """Resolve a scenario's criterion references to criterion text.

    The design call may reference a criterion by id or by wording. Ids are
    regenerated on every run, so only the text is worth remembering.
    """
    criteria = ctx.requirement.acceptance_criteria if ctx.requirement else []
    by_id = {c.id: c.text for c in criteria}
    known = {c.text for c in criteria}

    out: list[str] = []
    for reference in scenario.covers_criteria or []:
        text = by_id.get(str(reference)) or (str(reference) if str(reference) in known else "")
        if text and text not in out:
            out.append(text)
    return out


def _api_label(check: Any) -> str:
    """`POST /residents` — what an engineer would call this endpoint."""
    if isinstance(check, dict):
        method = str(check.get("method") or "GET").upper()
        path = str(check.get("path") or "").strip()
        if path:
            return f"{method} {path}"
        return str(check.get("name") or "")[:120]
    return str(check)[:120]


def _db_label(check: Any) -> str:
    if isinstance(check, dict):
        return str(check.get("table") or check.get("name") or "")[:120]
    return str(check)[:120]


#: `readonly path = '/residents/new';` in a generated Page Object.
_PATH_RE = re.compile(r"readonly\s+path\s*=\s*['\"]([^'\"]+)['\"]")


def _routes_of(changes: list[Any]) -> list[str]:
    """The routes the page objects in this bundle actually navigate to."""
    routes: list[str] = []
    for change in changes:
        if str(getattr(change.kind, "value", change.kind)) != "page_object":
            continue
        match = _PATH_RE.search(change.content or "")
        if match and match.group(1) not in routes:
            routes.append(match.group(1))
    return routes[:6]
