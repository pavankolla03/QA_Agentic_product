"""Run tracker — the observability spine.

One :class:`RunTracker` exists per run. Agents record spans through it; it
persists agent traces, LLM calls, tool calls, events, costs and audit entries,
and fans events out to live WebSocket subscribers.

It is deliberately fail-soft: a tracing error must never fail a QA run.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select

from configs.settings import get_settings
from packages.agent_protocol import is_control_signal
from packages.aiqa_types.enums import AgentName, AgentStatus, AuditAction, Severity
from packages.aiqa_types.models import (
    AgentTrace,
    CostSummary,
    LLMCallTrace,
    RunEvent,
    ToolCallTrace,
    new_id,
)
from packages.security.redaction import redact
from services.observability.db import session_scope
from services.observability.models import (
    AgentTraceRow,
    AuditRow,
    CostDailyRow,
    LLMCallRow,
    RunEventRow,
    RunRow,
    ToolCallRow,
)

log = logging.getLogger("aiqa.tracker")

EventListener = Callable[[RunEvent], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview(value: Any, limit: int = 600) -> str:
    text = value if isinstance(value, str) else str(value)
    return redact(text)[:limit]


class RunTracker:
    """Per-run observability recorder."""

    def __init__(
        self,
        run_id: str,
        project_id: str = "",
        user_id: str = "",
        org_id: str = "",
        session_id: str = "",
        repository_id: str = "",
        listeners: list[EventListener] | None = None,
        persist: bool = True,
    ) -> None:
        self.run_id = run_id
        self.project_id = project_id
        self.user_id = user_id
        self.org_id = org_id
        self.session_id = session_id or new_id("ses")
        self.repository_id = repository_id
        self.listeners: list[EventListener] = listeners or []
        self.persist = persist

        self.sequence = 0
        self.events: list[RunEvent] = []
        self.agent_traces: list[AgentTrace] = []
        self.llm_traces: list[LLMCallTrace] = []
        self.tool_traces: list[ToolCallTrace] = []
        self._current_trace_id: str = ""
        self._current_agent: AgentName | None = None

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def emit(
        self,
        type: str,
        message: str = "",
        *,
        agent: AgentName | None = None,
        level: Severity = Severity.INFO,
        data: dict[str, Any] | None = None,
        progress: float | None = None,
    ) -> RunEvent:
        event = RunEvent(
            run_id=self.run_id,
            type=type,
            agent=agent or self._current_agent,
            level=level,
            message=redact(message)[:2000],
            data=data or {},
            progress=progress,
        )
        self.events.append(event)

        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not break the run
                log.debug("event listener failed", exc_info=True)

        if self.persist:
            with contextlib.suppress(Exception):
                with session_scope() as s:
                    s.add(
                        RunEventRow(
                            id=event.id,
                            run_id=self.run_id,
                            type=type,
                            agent=str(event.agent.value) if event.agent else "",
                            level=str(level.value),
                            message=event.message,
                            data=event.data,
                            progress=progress,
                        )
                    )
        return event

    def log(self, message: str, level: Severity = Severity.INFO, **data: Any) -> RunEvent:
        return self.emit("log", message, level=level, data=data or None)

    # ------------------------------------------------------------------ #
    # Agent spans
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def agent_span(self, agent: AgentName, input_summary: str = "", progress: float | None = None) -> Iterator[AgentTrace]:
        """Context manager wrapping one agent's execution."""
        self.sequence += 1
        trace = AgentTrace(
            run_id=self.run_id,
            session_id=self.session_id,
            project_id=self.project_id,
            user_id=self.user_id,
            repository_id=self.repository_id,
            agent=agent,
            status=AgentStatus.RUNNING,
            sequence=self.sequence,
            input_summary=_preview(input_summary),
        )
        self.agent_traces.append(trace)
        prev_trace_id, prev_agent = self._current_trace_id, self._current_agent
        self._current_trace_id, self._current_agent = trace.id, agent

        started = time.perf_counter()
        self.emit("agent_started", f"{agent.value} started", agent=agent, progress=progress,
                  data={"trace_id": trace.id, "sequence": trace.sequence})
        try:
            yield trace
        except Exception as exc:
            trace.latency_ms = int((time.perf_counter() - started) * 1000)
            trace.ended_at = _utcnow()
            if is_control_signal(exc):
                # The agent is waiting for a human, not broken.
                trace.status = AgentStatus.WAITING_APPROVAL
                self._persist_trace(trace)
                self.emit(
                    "agent_suspended",
                    f"{agent.value} is waiting for approval: {exc}",
                    agent=agent, level=Severity.INFO,
                    data={"trace_id": trace.id, "reason": str(exc)[:300]},
                )
            else:
                trace.status = AgentStatus.FAILED
                trace.error = str(exc)[:2000]
                self._persist_trace(trace)
                self.emit("agent_failed", f"{agent.value} failed: {exc}", agent=agent, level=Severity.ERROR,
                          data={"trace_id": trace.id, "error": str(exc)[:500]})
            raise
        else:
            if trace.status == AgentStatus.RUNNING:
                trace.status = AgentStatus.SUCCEEDED
            trace.latency_ms = int((time.perf_counter() - started) * 1000)
            trace.ended_at = _utcnow()
            self._persist_trace(trace)
            self.emit("agent_finished", trace.output_summary or f"{agent.value} finished",
                      agent=agent, progress=progress,
                      data={"trace_id": trace.id, "cost_usd": trace.cost_usd,
                            "tokens": trace.usage.total_tokens, "latency_ms": trace.latency_ms,
                            "status": trace.status.value})
        finally:
            self._current_trace_id, self._current_agent = prev_trace_id, prev_agent

    def _persist_trace(self, trace: AgentTrace) -> None:
        if not self.persist:
            return
        with contextlib.suppress(Exception):
            with session_scope() as s:
                s.merge(
                    AgentTraceRow(
                        id=trace.id,
                        run_id=trace.run_id,
                        project_id=trace.project_id,
                        user_id=trace.user_id,
                        session_id=trace.session_id,
                        repository_id=trace.repository_id,
                        agent=str(trace.agent.value),
                        status=str(trace.status.value),
                        sequence=trace.sequence,
                        input_summary=trace.input_summary,
                        output_summary=trace.output_summary,
                        provider=trace.provider,
                        model=trace.model,
                        prompt_tokens=trace.usage.prompt_tokens,
                        completion_tokens=trace.usage.completion_tokens,
                        total_tokens=trace.usage.total_tokens,
                        cost_usd=trace.cost_usd,
                        latency_ms=trace.latency_ms,
                        llm_calls=trace.llm_calls,
                        tool_calls=trace.tool_calls,
                        error=trace.error,
                        started_at=trace.started_at,
                        ended_at=trace.ended_at,
                    )
                )

    # ------------------------------------------------------------------ #
    # LLM calls (wired into ModelRouter.trace_sink)
    # ------------------------------------------------------------------ #
    def record_llm_call(self, trace: LLMCallTrace) -> None:
        trace.run_id = trace.run_id or self.run_id
        trace.trace_id = trace.trace_id or self._current_trace_id
        trace.agent = trace.agent or self._current_agent
        self.llm_traces.append(trace)

        # Roll the cost into the owning agent span.
        for agent_trace in self.agent_traces:
            if agent_trace.id == trace.trace_id:
                agent_trace.usage = agent_trace.usage + trace.usage
                agent_trace.cost_usd = round(agent_trace.cost_usd + trace.cost_usd, 8)
                agent_trace.llm_calls += 1
                agent_trace.provider = agent_trace.provider or trace.provider
                agent_trace.model = agent_trace.model or trace.model
                break

        self.emit(
            "llm_call",
            f"{trace.provider}/{trace.model} · {trace.usage.total_tokens} tok · ${trace.cost_usd:.6f}",
            agent=trace.agent,
            level=Severity.ERROR if trace.status == "failed" else Severity.INFO,
            data={
                "provider": trace.provider, "model": trace.model,
                "prompt_tokens": trace.usage.prompt_tokens,
                "completion_tokens": trace.usage.completion_tokens,
                "cost_usd": trace.cost_usd, "latency_ms": trace.latency_ms,
                "status": trace.status, "fallback_from": trace.fallback_from,
            },
        )

        if self.persist:
            with contextlib.suppress(Exception):
                with session_scope() as s:
                    s.add(
                        LLMCallRow(
                            id=trace.id, run_id=self.run_id, trace_id=trace.trace_id,
                            agent=str(trace.agent.value) if trace.agent else "",
                            provider=trace.provider, model=trace.model,
                            capability=str(trace.capability.value),
                            prompt_tokens=trace.usage.prompt_tokens,
                            completion_tokens=trace.usage.completion_tokens,
                            total_tokens=trace.usage.total_tokens,
                            cached_tokens=trace.usage.cached_tokens,
                            cost_usd=trace.cost_usd, latency_ms=trace.latency_ms,
                            status=trace.status, error=trace.error[:2000],
                            prompt_chars=trace.prompt_chars, completion_chars=trace.completion_chars,
                            prompt_preview=trace.prompt_preview,
                            fallback_from=trace.fallback_from or "",
                        )
                    )
            self._roll_up_cost(trace)

    def _roll_up_cost(self, trace: LLMCallTrace) -> None:
        day = _utcnow().strftime("%Y-%m-%d")
        with contextlib.suppress(Exception):
            with session_scope() as s:
                row = s.execute(
                    select(CostDailyRow).where(
                        CostDailyRow.day == day,
                        CostDailyRow.org_id == self.org_id,
                        CostDailyRow.project_id == self.project_id,
                        CostDailyRow.user_id == self.user_id,
                        CostDailyRow.provider == trace.provider,
                        CostDailyRow.model == trace.model,
                    )
                ).scalar_one_or_none()
                if row is None:
                    s.add(
                        CostDailyRow(
                            day=day, month=day[:7], org_id=self.org_id, project_id=self.project_id,
                            user_id=self.user_id, agent=str(trace.agent.value) if trace.agent else "",
                            provider=trace.provider, model=trace.model,
                            cost_usd=trace.cost_usd, total_tokens=trace.usage.total_tokens, calls=1,
                        )
                    )
                else:
                    row.cost_usd = round(row.cost_usd + trace.cost_usd, 8)
                    row.total_tokens += trace.usage.total_tokens
                    row.calls += 1

    # ------------------------------------------------------------------ #
    # Tool calls
    # ------------------------------------------------------------------ #
    def record_tool_call(self, trace: ToolCallTrace) -> None:
        trace.run_id = trace.run_id or self.run_id
        trace.trace_id = trace.trace_id or self._current_trace_id
        trace.agent = trace.agent or self._current_agent
        self.tool_traces.append(trace)

        for agent_trace in self.agent_traces:
            if agent_trace.id == trace.trace_id:
                agent_trace.tool_calls.append(trace.tool)
                break

        self.emit(
            "tool_call",
            f"{trace.category.value}.{trace.tool} → {trace.status}",
            agent=trace.agent,
            level=Severity.ERROR if trace.status == "failed" else Severity.INFO,
            data={"tool": trace.tool, "category": str(trace.category.value),
                  "status": trace.status, "latency_ms": trace.latency_ms,
                  "args": trace.arguments_preview, "error": trace.error[:300]},
        )

        if self.persist:
            with contextlib.suppress(Exception):
                with session_scope() as s:
                    s.add(
                        ToolCallRow(
                            id=trace.id, run_id=self.run_id, trace_id=trace.trace_id,
                            agent=str(trace.agent.value) if trace.agent else "",
                            category=str(trace.category.value), tool=trace.tool,
                            arguments_preview=trace.arguments_preview, status=trace.status,
                            error=trace.error[:2000], latency_ms=trace.latency_ms,
                            result_preview=trace.result_preview,
                        )
                    )

    @contextlib.contextmanager
    def tool_span(self, category: Any, tool: str, arguments: Any = "") -> Iterator[ToolCallTrace]:
        trace = ToolCallTrace(category=category, tool=tool, arguments_preview=_preview(arguments, 400))
        started = time.perf_counter()
        try:
            yield trace
        except Exception as exc:
            trace.status = "failed"
            trace.error = str(exc)[:2000]
            trace.latency_ms = int((time.perf_counter() - started) * 1000)
            self.record_tool_call(trace)
            raise
        else:
            trace.latency_ms = int((time.perf_counter() - started) * 1000)
            self.record_tool_call(trace)

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #
    def audit(
        self,
        action: AuditAction | str,
        resource: str = "",
        outcome: str = "allowed",
        detail: str = "",
        user_id: str = "",
    ) -> None:
        act = action.value if isinstance(action, AuditAction) else str(action)
        if self.persist:
            with contextlib.suppress(Exception):
                with session_scope() as s:
                    s.add(
                        AuditRow(
                            id=new_id("aud"), org_id=self.org_id, project_id=self.project_id,
                            run_id=self.run_id, user_id=user_id or self.user_id, action=act,
                            resource=_preview(resource, 500), outcome=outcome,
                            detail=_preview(detail, 2000),
                        )
                    )
        self.emit(
            "audit",
            f"{act} · {outcome} · {resource}",
            level=Severity.WARNING if outcome != "allowed" else Severity.INFO,
            data={"action": act, "resource": _preview(resource, 200), "outcome": outcome},
        )

    # ------------------------------------------------------------------ #
    # Roll-ups
    # ------------------------------------------------------------------ #
    def cost_summary(self) -> CostSummary:
        summary = CostSummary(run_id=self.run_id, project_id=self.project_id, user_id=self.user_id)
        for call in self.llm_traces:
            if call.status != "succeeded":
                continue
            summary.total_cost_usd = round(summary.total_cost_usd + call.cost_usd, 8)
            summary.total_tokens += call.usage.total_tokens
            summary.prompt_tokens += call.usage.prompt_tokens
            summary.completion_tokens += call.usage.completion_tokens
            summary.llm_calls += 1
            agent_key = call.agent.value if call.agent else "unknown"
            summary.by_agent[agent_key] = round(summary.by_agent.get(agent_key, 0.0) + call.cost_usd, 8)
            summary.by_model[call.model] = round(summary.by_model.get(call.model, 0.0) + call.cost_usd, 8)
            summary.by_provider[call.provider] = round(summary.by_provider.get(call.provider, 0.0) + call.cost_usd, 8)
            if call.cost_usd > 0:
                summary.paid_calls += 1
            else:
                summary.free_calls += 1
        return summary

    def flush_run_totals(self, status: str | None = None, **fields: Any) -> None:
        """Write roll-ups back onto the run row."""
        if not self.persist:
            return
        summary = self.cost_summary()
        with contextlib.suppress(Exception):
            with session_scope() as s:
                row = s.get(RunRow, self.run_id)
                if row is None:
                    return
                row.total_cost_usd = summary.total_cost_usd
                row.total_tokens = summary.total_tokens
                row.prompt_tokens = summary.prompt_tokens
                row.completion_tokens = summary.completion_tokens
                row.llm_calls = summary.llm_calls
                row.tool_calls = len(self.tool_traces)
                if status:
                    row.status = status
                for key, value in fields.items():
                    if hasattr(row, key):
                        setattr(row, key, value)


# --------------------------------------------------------------------------- #
# Organisation-wide cost queries
# --------------------------------------------------------------------------- #
class CostGovernor:
    """Enforces daily/monthly spend ceilings before a run is allowed to start."""

    def __init__(self, org_id: str = "") -> None:
        self.org_id = org_id
        self.settings = get_settings()

    def _sum(self, column: str, value: str, org_scoped: bool = True) -> float:
        with session_scope() as s:
            stmt = select(func.coalesce(func.sum(CostDailyRow.cost_usd), 0.0)).where(
                getattr(CostDailyRow, column) == value
            )
            if org_scoped and self.org_id:
                stmt = stmt.where(CostDailyRow.org_id == self.org_id)
            return float(s.execute(stmt).scalar_one() or 0.0)

    def spent_today(self) -> float:
        return self._sum("day", _utcnow().strftime("%Y-%m-%d"))

    def spent_this_month(self) -> float:
        return self._sum("month", _utcnow().strftime("%Y-%m"))

    def check(self) -> tuple[bool, str]:
        """Return ``(allowed, reason)``."""
        daily_limit = self.settings.daily_cost_limit_usd
        monthly_limit = self.settings.monthly_cost_limit_usd
        if daily_limit:
            today = self.spent_today()
            if today >= daily_limit:
                return False, f"Daily LLM budget exhausted: ${today:.4f} of ${daily_limit:.2f}."
        if monthly_limit:
            month = self.spent_this_month()
            if month >= monthly_limit:
                return False, f"Monthly LLM budget exhausted: ${month:.4f} of ${monthly_limit:.2f}."
        return True, ""

    def snapshot(self) -> dict[str, Any]:
        today, month = self.spent_today(), self.spent_this_month()
        return {
            "spent_today_usd": round(today, 6),
            "spent_month_usd": round(month, 6),
            "daily_limit_usd": self.settings.daily_cost_limit_usd,
            "monthly_limit_usd": self.settings.monthly_cost_limit_usd,
            "daily_remaining_usd": round(max(0.0, self.settings.daily_cost_limit_usd - today), 6),
            "monthly_remaining_usd": round(max(0.0, self.settings.monthly_cost_limit_usd - month), 6),
            "daily_used_pct": round(100 * today / self.settings.daily_cost_limit_usd, 2)
            if self.settings.daily_cost_limit_usd
            else 0.0,
        }
