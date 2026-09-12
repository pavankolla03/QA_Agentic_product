"""Execution Agent.

Applies the approved change set to the workspace (the second human gate), then
runs the suite and collects structured results.

Two ordering decisions that matter:

* **Approval before write.** The diff a human approves is the diff that lands.
* **Write, then run, then (optionally) commit.** Committing before a green run
  would put unverified generated code into history.
"""

from __future__ import annotations

from typing import Any

from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import (
    AgentName,
    ApprovalKind,
    Capability,
    RiskLevel,
    RunMode,
    TestStatus,
)
from packages.aiqa_types.models import ExecutionResult


class ExecutionAgent(BaseAgent):
    name = AgentName.EXECUTION
    capability = Capability.FAST
    description = "Applies approved changes to the workspace and runs the test suite."

    def progress(self, ctx: AgentContext) -> float:
        return 0.78

    def skip_reason(self, ctx: AgentContext) -> str:
        if ctx.mode in (RunMode.PLAN_ONLY, RunMode.GENERATE):
            return f"mode={ctx.mode.value} — generation only, not executing"
        return ""

    async def run(self, ctx: AgentContext) -> None:
        # --- 1. Write the approved bundle ------------------------------- #
        if ctx.code_bundle and ctx.code_bundle.changes and not ctx.metadata.get("changes_applied"):
            self._request_write_approval(ctx)
            self._apply(ctx)

        # --- 2. Run the suite ------------------------------------------- #
        filter_expr, tags = self._selection(ctx)
        result = self.tool(
            ctx, "playwright.run_tests",
            test_filter=filter_expr, tags=tags,
            retries=ctx.metadata.get("retries", 0),
            timeout=ctx.metadata.get("test_timeout", 900),
        )

        if not result.ok:
            ctx.warn(f"test execution could not run: {result.error[:300]}")
            ctx.execution = ExecutionResult(
                run_id=ctx.run_id,
                command=filter_expr or "playwright test",
                cwd=ctx.project_root,
                exit_code=-1,
                stderr_tail=result.error[:2000],
                simulated=True,
            )
            ctx.metadata["execution_blocked"] = result.error[:300]
            return

        execution = ExecutionResult(**result.data)
        execution.run_id = ctx.run_id
        ctx.execution = execution

        summary = (
            f"executed {execution.total} test(s): {execution.passed} passed, "
            f"{execution.failed} failed, {execution.skipped} skipped, {execution.flaky} flaky "
            f"in {execution.duration_ms / 1000:.1f}s"
        )
        if execution.failed:
            ctx.warn(summary)
            for failure in execution.failures[:8]:
                ctx.warn(f"  FAIL {failure.test_id or failure.name}: {failure.error_message.splitlines()[0][:160] if failure.error_message else 'no message'}")
        else:
            ctx.note(summary)

        self._record_flakiness(ctx, execution)

        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = summary

    # ------------------------------------------------------------------ #
    def _request_write_approval(self, ctx: AgentContext) -> None:
        bundle = ctx.code_bundle
        assert bundle is not None
        report = ctx.standards_report

        if report and not report.passed:
            # A human can still override, but they must see what they are accepting.
            blocking = [
                f"{v.rule_id} {v.file_path}:{v.line} — {v.message}"
                for v in report.violations
                if v.severity.value in ("error", "critical")
            ]
            description = (
                f"⚠ {len(blocking)} standards error(s) must be acknowledged:\n"
                + "\n".join(f"  • {line}" for line in blocking[:12])
            )
            risk = RiskLevel.HIGH
        else:
            warnings = report.warning_count if report else 0
            description = (
                f"{len(bundle.changes)} file(s), {bundle.total_bytes} bytes. "
                f"Standards: clean" + (f" ({warnings} warning(s))" if warnings else "")
            )
            risk = RiskLevel.MEDIUM

        unverified = sum(1 for change in bundle.changes if "TODO(aiqa)" in change.content)
        if unverified:
            description += f"\n\n{unverified} file(s) contain TODO(aiqa) markers where a locator could not be verified."

        diff_preview = "\n".join(change.diff or f"--- new file {change.path} ---\n{change.content}" for change in bundle.changes)

        self.request_approval(
            ctx,
            ApprovalKind.CODE_WRITE,
            title=f"Apply {len(bundle.changes)} generated file(s) to the workspace",
            description=description,
            risk=risk,
            payload={
                "files": [
                    {"path": c.path, "kind": c.kind.value, "bytes": c.bytes, "change_type": c.change_type.value}
                    for c in bundle.changes
                ],
                "standards_passed": bool(report.passed) if report else True,
            },
            diff_preview=diff_preview,
        )

    def _apply(self, ctx: AgentContext) -> None:
        bundle = ctx.code_bundle
        assert bundle is not None

        # Work on a feature branch when the repository is a git checkout.
        status = self.tool(ctx, "git.status")
        if status.ok and (status.data or {}).get("is_repo"):
            current = (status.data or {}).get("branch", "")
            if (status.data or {}).get("protected") or not current.startswith("aiqa/"):
                slug = (ctx.requirement.title if ctx.requirement else ctx.instruction)[:48]
                branch = self.tool(ctx, "git.branch", slug=slug)
                if branch.ok:
                    ctx.note(f"working on branch {branch.data}")
                    ctx.metadata["branch"] = branch.data
                else:
                    ctx.warn(f"could not create a feature branch ({branch.error[:160]}); staying on {current}")

        result = self.tool(
            ctx, "fs.apply_changes",
            changes=[
                {"path": c.path, "content": c.content, "change_type": c.change_type.value}
                for c in bundle.changes
            ],
        )
        if not result.ok:
            raise RuntimeError(f"could not apply generated files: {result.error}")
        ctx.metadata["changes_applied"] = True
        ctx.metadata["applied_files"] = result.data
        ctx.note(f"applied {len(result.data or [])} file(s) to {ctx.project_root}")

    # ------------------------------------------------------------------ #
    def _selection(self, ctx: AgentContext) -> tuple[str, list[str]]:
        """Run only what this change affects — full suites are for CI, not iteration."""
        explicit = ctx.metadata.get("test_filter")
        if explicit:
            return str(explicit), []

        if ctx.mode == RunMode.EXECUTE_ONLY:
            tags = ctx.metadata.get("tags") or []
            return "", list(tags)

        if ctx.code_bundle:
            features = [
                c.path for c in ctx.code_bundle.changes if c.kind.value == "feature"
            ]
            if len(features) == 1:
                return features[0], []
            if features:
                return "", []

        return "", list(ctx.metadata.get("tags") or [])

    def _record_flakiness(self, ctx: AgentContext, execution: ExecutionResult) -> None:
        """Maintain the per-test flakiness ledger that drives quarantine advice."""
        from services.observability.db import session_scope
        from services.observability.models import FlakyTestRow
        from sqlalchemy import select

        if not execution.results:
            return
        try:
            with session_scope() as session:
                for case in execution.results:
                    key = case.test_id or case.name
                    if not key:
                        continue
                    row = session.execute(
                        select(FlakyTestRow).where(
                            FlakyTestRow.project_id == ctx.project.id,
                            FlakyTestRow.test_id == key,
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        row = FlakyTestRow(
                            project_id=ctx.project.id, test_id=key, test_name=case.name,
                            file_path=case.file_path,
                        )
                        session.add(row)
                    row.runs += 1
                    row.test_name = case.name or row.test_name
                    row.file_path = case.file_path or row.file_path
                    if case.status == TestStatus.FLAKY:
                        row.flakes += 1
                    elif case.status in (TestStatus.FAILED, TestStatus.TIMED_OUT):
                        row.failures += 1
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail a run
            ctx.warn(f"could not update the flakiness ledger: {exc}")


class CommitAgent(BaseAgent):
    """Commits verified work. Separate from execution so it only runs on green."""

    name = AgentName.EXECUTION
    capability = Capability.FAST
    description = "Commits the generated tests once they have been verified."

    def skip_reason(self, ctx: AgentContext) -> str:
        if not ctx.metadata.get("changes_applied"):
            return "no changes were applied"
        if ctx.execution is None:
            return "nothing was executed, so nothing is verified"
        if ctx.execution.failed:
            return f"{ctx.execution.failed} test(s) still failing — not committing"
        return ""

    async def run(self, ctx: AgentContext) -> None:
        files = ctx.metadata.get("applied_files") or []
        title = ctx.requirement.title if ctx.requirement else ctx.instruction[:60]
        scenarios = ctx.test_plan.scenario_count if ctx.test_plan else 0
        message = (
            f"test: automate {title}\n\n"
            f"Generated by AI QA Engineer.\n"
            f"- {scenarios} scenario(s), {len(files)} file(s)\n"
            f"- verified: {ctx.execution.passed}/{ctx.execution.total} passing\n"
            f"- run: {ctx.run_id}\n"
        )

        self.request_approval(
            ctx,
            ApprovalKind.GIT_COMMIT,
            title=f"Commit {len(files)} verified test file(s)",
            description=message,
            risk=RiskLevel.MEDIUM,
            payload={"files": files, "branch": ctx.metadata.get("branch", "")},
        )

        result = self.tool(ctx, "git.commit", message=message, paths=files)
        if result.ok:
            ctx.note(f"committed {(result.data or {}).get('sha', '')} on {(result.data or {}).get('branch', '')}")
            ctx.metadata["commit"] = result.data
        else:
            ctx.warn(f"commit failed: {result.error[:200]}")
