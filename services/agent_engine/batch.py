"""Batch runs — a whole epic, not one ticket at a time.

Automating one ticket per invocation is why a QA backlog never shrinks: the
feature team ships an epic a sprint, and the QA team automates a story a day.
This module takes a list of requirements and turns them into a queue.

The whole design is shaped by one fact: **a batch is the most expensive thing
this platform can do.** Thirty issues is thirty runs, and a mistyped epic key
should not cost thirty runs' worth of tokens. So:

* Planning and running are separate calls. `plan_batch()` never starts anything;
  it returns the queue and what it is expected to cost, drawn from this
  project's own measured history rather than a guess.
* The batch carries its own ceiling, independent of the per-run one. It stops
  when the ceiling is reached and says which items were not attempted, rather
  than quietly spending past it.
* Items are ordered by priority, so if the ceiling does stop the batch, what
  got done is the part that mattered most.
* One failing item never kills the queue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from packages.aiqa_types.enums import RunMode, RunStatus
from packages.aiqa_types.models import RunRequest

log = logging.getLogger("aiqa.batch")

#: Used only when the project has no measured history to draw on. Deliberately
#: pessimistic: a batch that costs less than predicted is a good surprise.
FALLBACK_COST_PER_RUN_USD = 0.15

#: Jira priority names, most urgent first. Anything unrecognised sorts last.
_PRIORITY_ORDER = ("highest", "blocker", "critical", "high", "major", "medium", "normal", "low", "lowest", "trivial")

#: Modes whose whole purpose is to produce scenarios. In `execute_only` or
#: `heal_only` a scenario count of zero is expected, not a non-result.
_SCENARIO_MODES = ("full", "plan_only", "generate")


@dataclass
class BatchItem:
    """One requirement in the queue."""

    key: str
    instruction: str
    priority: str = "medium"
    url: str = ""
    run_id: str = ""
    #: pending | running | succeeded | no_work | failed | skipped
    #:
    #: `no_work` is the honest answer for a run that completed but generated
    #: nothing — usually because the suite already covered the request. Folding
    #: that into `succeeded` would let a batch report ten green items when it
    #: actually produced two.
    status: str = "pending"
    error: str = ""
    cost_usd: float = 0.0
    scenarios: int = 0
    files_changed: int = 0

    @property
    def rank(self) -> int:
        try:
            return _PRIORITY_ORDER.index(self.priority.strip().lower())
        except ValueError:
            return len(_PRIORITY_ORDER)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchPlan:
    """What a batch would do, before it does anything."""

    project_id: str
    items: list[BatchItem] = field(default_factory=list)
    mode: str = "full"
    estimated_cost_usd: float = 0.0
    estimate_basis: str = ""
    max_cost_usd: float = 0.0

    @property
    def within_budget(self) -> bool:
        return self.max_cost_usd <= 0 or self.estimated_cost_usd <= self.max_cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "mode": self.mode,
            "item_count": len(self.items),
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "estimate_basis": self.estimate_basis,
            "max_cost_usd": self.max_cost_usd,
            "within_budget": self.within_budget,
            "items": [item.to_dict() for item in self.items],
            "note": "Nothing has run. Call execute_batch to start it.",
        }


@dataclass
class BatchResult:
    plan: BatchPlan
    completed: list[BatchItem] = field(default_factory=list)
    failed: list[BatchItem] = field(default_factory=list)
    skipped: list[BatchItem] = field(default_factory=list)
    total_cost_usd: float = 0.0
    stopped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": len(self.completed) + len(self.failed),
            "succeeded": len(self.completed) - len(self.produced_nothing),
            "already_covered": len(self.produced_nothing),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "stopped_reason": self.stopped_reason,
            "items": [i.to_dict() for i in [*self.completed, *self.failed, *self.skipped]],
            "summary": self.summary(),
        }

    @property
    def produced_nothing(self) -> list[BatchItem]:
        return [item for item in self.completed if item.status == "no_work"]

    def summary(self) -> str:
        generated = len(self.completed) - len(self.produced_nothing)
        parts = [f"{generated} generated tests", f"{len(self.failed)} failed"]
        if self.produced_nothing:
            parts.append(f"{len(self.produced_nothing)} already covered")
        if self.skipped:
            parts.append(f"{len(self.skipped)} not attempted")
        text = ", ".join(parts) + f" (${self.total_cost_usd:.4f})"
        return f"{text}. {self.stopped_reason}" if self.stopped_reason else text + "."


# --------------------------------------------------------------------------- #
def _measured_cost_per_run(project_id: str) -> tuple[float, str]:
    """This project's own average run cost, if it has any history."""
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import RunRow

    try:
        with session_scope() as session:
            rows = list(
                session.execute(
                    select(RunRow.total_cost_usd)
                    .where(RunRow.project_id == project_id, RunRow.status == RunStatus.SUCCEEDED.value)
                    .order_by(RunRow.created_at.desc())
                    .limit(20)
                ).scalars()
            )
    except Exception:  # noqa: BLE001 - an estimate must never break planning
        rows = []

    costs = [float(c) for c in rows if c and float(c) > 0]
    if not costs:
        return FALLBACK_COST_PER_RUN_USD, "no measured history for this project; using a pessimistic default"
    average = sum(costs) / len(costs)
    return average, f"mean of the last {len(costs)} successful run(s) on this project"


def plan_batch(
    project_id: str,
    requirements: list[dict[str, Any]],
    *,
    mode: str = "full",
    max_cost_usd: float = 0.0,
    max_items: int = 50,
) -> BatchPlan:
    """Build the queue and price it. Starts nothing."""
    items: list[BatchItem] = []
    seen: set[str] = set()
    for raw in requirements:
        instruction = str(raw.get("instruction") or raw.get("requirement_text") or raw.get("summary") or "").strip()
        if len(instruction) < 8:
            continue                       # nothing to act on
        key = str(raw.get("key") or raw.get("id") or instruction[:40]).strip()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            BatchItem(
                key=key,
                instruction=instruction[:4000],
                priority=str(raw.get("priority") or "medium"),
                url=str(raw.get("url") or ""),
            )
        )

    # Highest priority first: if the ceiling stops the batch, what ran is what
    # mattered most.
    items.sort(key=lambda item: (item.rank, item.key))
    items = items[:max_items]

    per_run, basis = _measured_cost_per_run(project_id)
    return BatchPlan(
        project_id=project_id,
        items=items,
        mode=mode,
        estimated_cost_usd=per_run * len(items),
        estimate_basis=basis,
        max_cost_usd=max_cost_usd,
    )


async def execute_batch(
    engine: Any,
    plan: BatchPlan,
    *,
    user_id: str = "",
    org_id: str = "",
    auto_approve: bool = False,
    per_run_cost_usd: float = 0.0,
) -> BatchResult:
    """Run the queue, stopping cleanly at the ceiling.

    Sequential on purpose. Runs share the repository index, the application map
    and the test knowledge store, and each one makes the next cheaper — running
    them concurrently would have every item pay full price for knowledge its
    siblings were in the middle of building.
    """
    result = BatchResult(plan=plan)

    for index, item in enumerate(plan.items):
        if plan.max_cost_usd > 0 and result.total_cost_usd >= plan.max_cost_usd:
            item.status = "skipped"
            result.skipped.append(item)
            if not result.stopped_reason:
                result.stopped_reason = (
                    f"Stopped at the batch ceiling of ${plan.max_cost_usd:.2f} after "
                    f"{index} item(s); {len(plan.items) - index} not attempted."
                )
            continue

        item.status = "running"
        try:
            run_id = engine.create_run(
                RunRequest(
                    project_id=plan.project_id,
                    instruction=item.instruction,
                    mode=RunMode(plan.mode),
                    auto_approve=auto_approve,
                    max_cost_usd=per_run_cost_usd or None,
                ),
                user_id=user_id,
                org_id=org_id,
            )
            item.run_id = run_id
            run = await engine.run_to_completion(run_id, auto_approve=auto_approve)

            cost, scenarios, files = _run_outcome(run_id)
            item.cost_usd = cost
            item.scenarios = scenarios
            item.files_changed = files
            result.total_cost_usd += cost

            if run.status == RunStatus.SUCCEEDED:
                # Scenarios are the unit of work here. A run can still touch
                # files (the repository map, a data fixture) while designing
                # nothing, and counting that as a success overstates the batch.
                produced_nothing = scenarios == 0 and plan.mode in _SCENARIO_MODES
                item.status = "no_work" if produced_nothing else "succeeded"
                if produced_nothing:
                    item.error = "completed without generating anything — already covered?"
                result.completed.append(item)
            else:
                item.status = "failed"
                item.error = (run.error or str(run.status))[:400]
                result.failed.append(item)
        except Exception as exc:  # noqa: BLE001 - one bad ticket must not end the queue
            log.exception("batch item %s failed", item.key)
            item.status = "failed"
            item.error = str(exc)[:400]
            result.failed.append(item)

    return result


def _run_outcome(run_id: str) -> tuple[float, int, int]:
    """(cost, scenarios designed, files changed) for one finished run."""
    from services.observability.db import session_scope
    from services.observability.models import RunRow

    try:
        with session_scope() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return 0.0, 0, 0
            plan = row.test_plan or {}
            scenarios = sum(len(f.get("scenarios", [])) for f in plan.get("features", []))
            return float(row.total_cost_usd or 0.0), scenarios, int(row.files_changed or 0)
    except Exception:  # noqa: BLE001
        return 0.0, 0, 0


def run_batch_sync(engine: Any, plan: BatchPlan, **kwargs: Any) -> BatchResult:
    """Blocking wrapper, for the CLI."""
    return asyncio.run(execute_batch(engine, plan, **kwargs))


__all__ = [
    "BatchItem",
    "BatchPlan",
    "BatchResult",
    "FALLBACK_COST_PER_RUN_USD",
    "execute_batch",
    "plan_batch",
    "run_batch_sync",
]
