"""Agent engine — creates, drives, suspends and resumes runs.

The engine owns the wiring (router, tools, tracker, standards, budget) and the
durability. Everything an in-flight run needs is written to the ``runs`` row, so
a run suspended on an approval survives a process restart: the extension can
approve hours later and :meth:`AgentEngine.resume` reconstructs the context and
continues at the exact node that asked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from agents.base import AgentContext
from agents.orchestrator.graph import GraphResult, Orchestrator
from configs.settings import get_settings, load_model_config, load_project_standards
from packages.aiqa_types.enums import (
    ApprovalStatus,
    AuditAction,
    RunMode,
    RunStatus,
    Severity,
)
from packages.aiqa_types.models import (
    ApprovalRequest,
    CodeBundle,
    ExecutionResult,
    ExplorationResult,
    FailureAnalysis,
    HealProposal,
    Project,
    RepoProfile,
    Requirement,
    RunEvent,
    RunRequest,
    StandardsReport,
    TestPlan,
    new_id,
)
from services.model_router.router import BudgetExceeded, ModelRouter, RunBudget
from services.observability.db import session_scope
from services.observability.models import ApprovalRow, ProjectRow, RunRow
from services.observability.tracker import CostGovernor, RunTracker
from tools import ToolContext, build_registry

log = logging.getLogger("aiqa.engine")

EventSink = Callable[[RunEvent], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunNotFound(LookupError):
    pass


class AgentEngine:
    """Entry point the API gateway calls."""

    def __init__(self, orchestrator: Orchestrator | None = None, offline: bool | None = None) -> None:
        self.orchestrator = orchestrator or Orchestrator()
        self.settings = get_settings()
        self.offline = offline
        #: run_id -> listeners, for live WebSocket streaming
        self._listeners: dict[str, list[EventSink]] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------ #
    # Subscriptions
    # ------------------------------------------------------------------ #
    def subscribe(self, run_id: str, sink: EventSink) -> Callable[[], None]:
        self._listeners.setdefault(run_id, []).append(sink)

        def unsubscribe() -> None:
            listeners = self._listeners.get(run_id, [])
            if sink in listeners:
                listeners.remove(sink)
            if not listeners:
                self._listeners.pop(run_id, None)

        return unsubscribe

    def _sinks(self, run_id: str) -> list[EventSink]:
        return list(self._listeners.get(run_id, []))

    # ------------------------------------------------------------------ #
    # Creating a run
    # ------------------------------------------------------------------ #
    def create_run(self, request: RunRequest, user_id: str = "", org_id: str = "") -> str:
        project = self._load_project(request.project_id)

        governor = CostGovernor(org_id or project.org_id)
        allowed, reason = governor.check()

        run_id = new_id("run")
        with session_scope() as session:
            session.add(
                RunRow(
                    id=run_id,
                    org_id=org_id or project.org_id,
                    project_id=project.id,
                    user_id=user_id,
                    session_id=new_id("ses"),
                    repository_id=project.repository_path,
                    instruction=request.instruction,
                    mode=request.mode.value,
                    status=RunStatus.BUDGET_EXCEEDED.value if not allowed else RunStatus.QUEUED.value,
                    error="" if allowed else reason,
                    metadata_json={
                        "target_url": request.target_url or project.base_url or "",
                        "jira_issue": request.jira_issue or "",
                        "tags": request.tags,
                        "test_filter": request.test_filter or "",
                        "auto_approve": bool(request.auto_approve),
                        "max_cost_usd": request.max_cost_usd or project.per_run_cost_limit_usd,
                        **(request.metadata or {}),
                    },
                )
            )
        if not allowed:
            log.warning("run %s blocked: %s", run_id, reason)
        return run_id

    # ------------------------------------------------------------------ #
    # Executing
    # ------------------------------------------------------------------ #
    async def execute(self, run_id: str) -> GraphResult:
        """Drive a queued (or approved-and-waiting) run to its next stopping point."""
        row_snapshot = self._run_snapshot(run_id)
        if row_snapshot["status"] == RunStatus.BUDGET_EXCEEDED.value:
            return GraphResult(status=RunStatus.BUDGET_EXCEEDED, error=row_snapshot.get("error", ""))

        project = self._load_project(row_snapshot["project_id"])
        ctx, tracker = self._build_context(run_id, project, row_snapshot)
        start_at = row_snapshot.get("metadata", {}).get("suspended_at", "")

        with session_scope() as session:
            row = session.get(RunRow, run_id)
            if row is not None:
                row.status = RunStatus.RUNNING.value
                row.started_at = row.started_at or _utcnow()
                row.error = ""

        tracker.emit(
            "run_started",
            f"run started in {ctx.mode.value} mode" + (f" (resuming at {start_at})" if start_at else ""),
            data={"mode": ctx.mode.value, "plan": self.orchestrator.plan_for(ctx.mode), "resumed_at": start_at},
            progress=0.02,
        )
        tracker.audit(AuditAction.RUN_CREATE, project.name, "allowed", ctx.instruction[:300])

        started = time.perf_counter()
        try:
            result = await self.orchestrator.run(ctx, start_at=start_at)
        except BudgetExceeded as exc:
            tracker.audit(AuditAction.BUDGET_BLOCK, run_id, "denied", str(exc))
            tracker.emit("run_failed", str(exc), level=Severity.ERROR)
            result = GraphResult(status=RunStatus.BUDGET_EXCEEDED, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("run %s crashed", run_id)
            tracker.emit("run_failed", f"{type(exc).__name__}: {exc}", level=Severity.ERROR)
            result = GraphResult(status=RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")

        ctx.metadata["duration_s"] = time.perf_counter() - started
        self._persist(ctx, tracker, result)

        if result.status == RunStatus.WAITING_APPROVAL:
            approval = ctx.approvals.get(result.approval_id)
            if approval is not None:
                self._persist_approval(approval)
                tracker.emit(
                    "approval_required",
                    approval.title,
                    level=Severity.WARNING,
                    data={
                        "approval_id": approval.id,
                        "kind": approval.kind.value,
                        "risk": approval.risk.value,
                        "description": approval.description,
                        "diff": approval.diff_preview[:200_000],
                        "payload": approval.payload,
                    },
                )
        elif result.status == RunStatus.SUCCEEDED:
            tracker.emit(
                "run_finished",
                ctx.report.headline if ctx.report else "run complete",
                data={
                    "cost_usd": tracker.cost_summary().total_cost_usd,
                    "report": ctx.report.model_dump(mode="json") if ctx.report else None,
                },
                progress=1.0,
            )
        else:
            tracker.emit("run_failed", result.error or "run failed", level=Severity.ERROR, progress=1.0)

        return result

    async def start(self, run_id: str) -> asyncio.Task[GraphResult]:
        """Fire-and-forget execution so the HTTP request returns immediately."""
        existing = self._running.get(run_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self.execute(run_id))
        self._running[run_id] = task
        task.add_done_callback(lambda _t: self._running.pop(run_id, None))
        return task

    # ------------------------------------------------------------------ #
    # Approvals
    # ------------------------------------------------------------------ #
    def respond_to_approval(
        self,
        approval_id: str,
        approved: bool,
        user_id: str = "",
        comment: str = "",
        changes_requested: bool = False,
    ) -> str:
        """Record a human decision. Returns the run id."""
        with session_scope() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise RunNotFound(f"approval {approval_id} not found")
            if row.status != ApprovalStatus.PENDING.value:
                return row.run_id

            row.status = (
                ApprovalStatus.APPROVED.value
                if approved
                else ApprovalStatus.CHANGES_REQUESTED.value
                if changes_requested
                else ApprovalStatus.REJECTED.value
            )
            row.responded_by = user_id
            row.response_comment = comment[:4000]
            row.responded_at = _utcnow()
            run_id, kind = row.run_id, row.kind

            run = session.get(RunRow, run_id)
            if run is not None:
                metadata = dict(run.metadata_json or {})
                if approved:
                    granted = set(metadata.get("granted", []))
                    granted.add(kind)
                    metadata["granted"] = sorted(granted)
                    run.status = RunStatus.QUEUED.value
                else:
                    metadata["rejected"] = sorted(set(metadata.get("rejected", [])) | {kind})
                    metadata["rejection_comment"] = comment[:2000]
                    run.status = RunStatus.CANCELLED.value
                    run.ended_at = _utcnow()
                    run.error = f"{kind} rejected by reviewer" + (f": {comment[:200]}" if comment else "")
                run.metadata_json = metadata

        tracker = RunTracker(run_id, listeners=self._sinks(run_id), persist=True)
        tracker.audit(
            AuditAction.APPROVAL_GRANT if approved else AuditAction.APPROVAL_REJECT,
            f"{kind}:{approval_id}",
            "allowed" if approved else "denied",
            comment,
            user_id=user_id,
        )
        tracker.emit(
            "approval_resolved",
            f"{kind} {'approved' if approved else 'rejected'}"
            + (f" — {comment[:120]}" if comment else ""),
            level=Severity.INFO if approved else Severity.WARNING,
            data={"approval_id": approval_id, "kind": kind, "approved": approved},
        )
        return run_id

    def pending_approvals(self, run_id: str = "", project_id: str = "") -> list[dict[str, Any]]:
        with session_scope() as session:
            stmt = select(ApprovalRow).where(ApprovalRow.status == ApprovalStatus.PENDING.value)
            if run_id:
                stmt = stmt.where(ApprovalRow.run_id == run_id)
            if project_id:
                stmt = stmt.where(ApprovalRow.project_id == project_id)
            rows = list(session.execute(stmt.order_by(ApprovalRow.created_at.desc())).scalars())
            return [
                {
                    "id": r.id, "run_id": r.run_id, "project_id": r.project_id, "kind": r.kind,
                    "title": r.title, "description": r.description, "risk": r.risk,
                    "payload": r.payload, "diff_preview": r.diff_preview,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]

    def cancel(self, run_id: str, user_id: str = "") -> None:
        with session_scope() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise RunNotFound(run_id)
            row.status = RunStatus.CANCELLED.value
            row.ended_at = _utcnow()
            row.error = row.error or "cancelled by user"
        task = self._running.get(run_id)
        if task and not task.done():
            task.cancel()
        RunTracker(run_id, listeners=self._sinks(run_id)).audit(
            AuditAction.RUN_CANCEL, run_id, "allowed", "", user_id=user_id
        )

    # ------------------------------------------------------------------ #
    # Context construction / persistence
    # ------------------------------------------------------------------ #
    def _load_project(self, project_id: str) -> Project:
        with session_scope() as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                raise RunNotFound(f"project {project_id} not found")
            return Project(
                id=row.id, org_id=row.org_id, name=row.name, repository_path=row.repository_path,
                repository_url=row.repository_url or None, default_branch=row.default_branch,
                framework=row.framework, language=row.language,
                base_url=row.base_url or None, api_base_url=row.api_base_url or None,
                database_dsn_ref=row.database_dsn_ref or None,
                standards_override=row.standards_override or {}, tags=row.tags or [],
                per_run_cost_limit_usd=float(row.per_run_cost_limit_usd or 2.0),
            )

    def _run_snapshot(self, run_id: str) -> dict[str, Any]:
        with session_scope() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise RunNotFound(run_id)
            return {
                "id": row.id, "org_id": row.org_id, "project_id": row.project_id,
                "user_id": row.user_id, "session_id": row.session_id,
                "repository_id": row.repository_id, "instruction": row.instruction,
                "mode": row.mode, "status": row.status, "error": row.error,
                "iteration": row.iteration, "metadata": dict(row.metadata_json or {}),
                "requirement": row.requirement, "test_plan": row.test_plan,
                "code_bundle": row.code_bundle, "standards_report": row.standards_report,
                "execution": row.execution, "exploration": row.exploration,
                "analyses": row.analyses or [], "heals": row.heals or [],
                "repo_profile": (row.metadata_json or {}).get("repo_profile"),
            }

    def _build_context(
        self, run_id: str, project: Project, snapshot: dict[str, Any]
    ) -> tuple[AgentContext, RunTracker]:
        metadata = dict(snapshot.get("metadata") or {})

        tracker = RunTracker(
            run_id=run_id,
            project_id=project.id,
            user_id=snapshot.get("user_id", ""),
            org_id=snapshot.get("org_id", "") or project.org_id,
            session_id=snapshot.get("session_id", ""),
            repository_id=snapshot.get("repository_id", "") or project.repository_path,
            listeners=self._sinks(run_id),
        )

        router = ModelRouter(trace_sink=tracker.record_llm_call, offline=self.offline)

        tool_ctx = ToolContext(
            project_root=project.repository_path,
            project_id=project.id,
            run_id=run_id,
            user_id=snapshot.get("user_id", ""),
            tracker=tracker,
            metadata={
                "base_url": metadata.get("target_url") or project.base_url or "",
                "api_base_url": project.api_base_url or "",
                "database_dsn_ref": project.database_dsn_ref or "",
            },
        )
        registry = build_registry(tool_ctx)

        standards = load_project_standards(project.repository_path)
        if project.standards_override:
            standards = {**standards, **project.standards_override}

        # Run budget: config defaults, overridden per project/request. Consumption
        # is restored from the row so a resumed run cannot reset its own ceiling.
        budget = RunBudget.from_config(
            load_model_config().get("run_budget"),
            max_cost_usd=float(metadata.get("max_cost_usd") or self.settings.per_run_cost_limit_usd),
            max_requests=int(metadata["max_requests"]) if metadata.get("max_requests") else None,
        )
        consumed = metadata.get("budget_consumed") or {}
        budget.requests = int(consumed.get("requests", 0))
        budget.input_tokens = int(consumed.get("input_tokens", 0))
        budget.output_tokens = int(consumed.get("output_tokens", 0))
        budget.cached_tokens = int(consumed.get("cached_tokens", 0))
        budget.spent_usd = float(consumed.get("spent_usd", metadata.get("spent_usd", 0.0)) or 0.0)
        budget.escalations = int(consumed.get("escalations", 0))
        budget.tokens_saved = int(consumed.get("tokens_saved", 0))

        try:
            mode = RunMode(snapshot.get("mode", "full"))
        except ValueError:
            mode = RunMode.FULL

        ctx = AgentContext(
            run_id=run_id,
            project=project,
            instruction=snapshot.get("instruction", ""),
            mode=mode,
            router=router,
            tools=registry,
            tracker=tracker,
            budget=budget,
            standards=standards,
            auto_approve=bool(metadata.get("auto_approve", False)),
            iteration=int(snapshot.get("iteration") or 0),
            granted=set(metadata.get("granted", [])),
            metadata=metadata,
        )

        # Rehydrate artifacts so a resumed run does not redo finished work.
        ctx.requirement = _model_or_none(Requirement, snapshot.get("requirement"))
        ctx.repo_profile = _model_or_none(RepoProfile, snapshot.get("repo_profile"))
        ctx.exploration = _model_or_none(ExplorationResult, snapshot.get("exploration"))
        ctx.test_plan = _model_or_none(TestPlan, snapshot.get("test_plan"))
        ctx.code_bundle = _model_or_none(CodeBundle, snapshot.get("code_bundle"))
        ctx.standards_report = _model_or_none(StandardsReport, snapshot.get("standards_report"))
        ctx.execution = _model_or_none(ExecutionResult, snapshot.get("execution"))
        ctx.analyses = [a for a in (_model_or_none(FailureAnalysis, x) for x in snapshot.get("analyses", [])) if a]
        ctx.heals = [h for h in (_model_or_none(HealProposal, x) for x in snapshot.get("heals", [])) if h]
        return ctx, tracker

    def _persist(self, ctx: AgentContext, tracker: RunTracker, result: GraphResult) -> None:
        """Write the full run state back so it is replayable and resumable."""
        metadata = dict(ctx.metadata)
        metadata["granted"] = sorted(ctx.granted)
        metadata["warnings"] = ctx.warnings[-40:]
        metadata["notes"] = ctx.notes[-80:]
        # A resumed run reports only the segment it executed; keep the whole trail.
        previous_visited = list(metadata.get("visited", []))
        metadata["visited"] = previous_visited + [n for n in result.visited]
        metadata["budget_consumed"] = ctx.budget.snapshot()
        metadata["spent_usd"] = ctx.budget.spent_usd
        metadata["suspended_at"] = result.suspended_at
        if ctx.repo_profile is not None:
            # Keep the profile but drop the bulky symbol list from the hot row.
            profile = ctx.repo_profile.model_dump(mode="json")
            profile["symbols"] = profile.get("symbols", [])[:400]
            metadata["repo_profile"] = profile

        cost = tracker.cost_summary()
        with session_scope() as session:
            row = session.get(RunRow, ctx.run_id)
            if row is None:
                return
            row.status = result.status.value
            row.error = result.error[:4000]
            row.iteration = ctx.iteration
            row.progress = 1.0 if result.status.terminal else row.progress
            row.current_agent = result.suspended_at or ""
            row.metadata_json = metadata

            row.requirement = ctx.requirement.model_dump(mode="json") if ctx.requirement else None
            row.test_plan = ctx.test_plan.model_dump(mode="json") if ctx.test_plan else None
            row.code_bundle = ctx.code_bundle.model_dump(mode="json") if ctx.code_bundle else None
            row.standards_report = ctx.standards_report.model_dump(mode="json") if ctx.standards_report else None
            row.execution = ctx.execution.model_dump(mode="json") if ctx.execution else None
            row.exploration = ctx.exploration.model_dump(mode="json") if ctx.exploration else None
            row.analyses = [a.model_dump(mode="json") for a in ctx.analyses]
            row.heals = [h.model_dump(mode="json") for h in ctx.heals]
            row.report = ctx.report.model_dump(mode="json") if ctx.report else None

            row.total_cost_usd = cost.total_cost_usd
            row.total_tokens = cost.total_tokens
            row.prompt_tokens = cost.prompt_tokens
            row.completion_tokens = cost.completion_tokens
            row.llm_calls = cost.llm_calls
            row.tool_calls = len(tracker.tool_traces)

            if ctx.test_plan:
                row.files_changed = len(ctx.code_bundle.changes) if ctx.code_bundle else 0
            if ctx.execution:
                row.tests_total = ctx.execution.total
                row.tests_passed = ctx.execution.passed
                row.tests_failed = ctx.execution.failed

            if result.status.terminal:
                row.ended_at = _utcnow()
                row.duration_s = float(ctx.metadata.get("duration_s", 0.0))

    @staticmethod
    def _persist_approval(approval: ApprovalRequest) -> None:
        with session_scope() as session:
            if session.get(ApprovalRow, approval.id) is not None:
                return
            session.add(
                ApprovalRow(
                    id=approval.id,
                    run_id=approval.run_id,
                    project_id=approval.project_id,
                    kind=approval.kind.value,
                    title=approval.title,
                    description=approval.description,
                    risk=approval.risk.value,
                    payload=approval.payload,
                    diff_preview=approval.diff_preview,
                    status=ApprovalStatus.PENDING.value,
                    requested_by=approval.requested_by,
                )
            )

    # ------------------------------------------------------------------ #
    async def run_to_completion(self, run_id: str, auto_approve: bool = False, max_gates: int = 12) -> GraphResult:
        """Drive a run through every approval gate.

        Used by CI, the ``aiqa`` CLI and the test-suite. With
        ``auto_approve=False`` this returns at the first gate, exactly like the
        interactive path.
        """
        result = await self.execute(run_id)
        gates = 0
        while result.status == RunStatus.WAITING_APPROVAL and auto_approve and gates < max_gates:
            gates += 1
            self.respond_to_approval(result.approval_id, approved=True, user_id="ci:auto-approve",
                                     comment="auto-approved by run_to_completion")
            result = await self.execute(run_id)
        return result


def _model_or_none(model: Any, payload: Any) -> Any:
    if not payload:
        return None
    try:
        return model(**payload) if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 - a schema change must not brick an old run
        log.debug("could not rehydrate %s", getattr(model, "__name__", model))
        return None


_engine: AgentEngine | None = None


def get_engine() -> AgentEngine:
    global _engine
    if _engine is None:
        _engine = AgentEngine()
    return _engine


def reset_engine() -> None:
    global _engine
    _engine = None
