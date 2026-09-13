"""Batch runs.

A batch is the most expensive thing this platform can do: one mistyped epic key
is thirty runs' worth of tokens. Every test here is about the ceiling, the
ordering and the separation between deciding and spending.
"""

from __future__ import annotations

import pytest

from packages.aiqa_types.enums import RunStatus
from services.agent_engine.batch import (
    FALLBACK_COST_PER_RUN_USD,
    BatchPlan,
    execute_batch,
    plan_batch,
)

ISSUES = [
    {"key": "QA-3", "requirement_text": "Automate resident search", "priority": "Low"},
    {"key": "QA-1", "requirement_text": "Automate resident registration", "priority": "Highest"},
    {"key": "QA-2", "requirement_text": "Automate resident editing", "priority": "Medium"},
]


class _Run:
    def __init__(self, status: RunStatus = RunStatus.SUCCEEDED, error: str = "") -> None:
        self.status = status
        self.error = error


class _Engine:
    """A stand-in engine: records what it was asked to run."""

    def __init__(self, fail_on: set[str] | None = None, raise_on: set[str] | None = None) -> None:
        self.started: list[str] = []
        self.fail_on = fail_on or set()
        self.raise_on = raise_on or set()

    def create_run(self, request, user_id: str = "", org_id: str = "") -> str:
        self.started.append(request.instruction)
        if request.instruction in self.raise_on:
            raise RuntimeError("control plane exploded")
        return f"run_{len(self.started)}"

    async def run_to_completion(self, run_id: str, auto_approve: bool = False):
        instruction = self.started[int(run_id.split("_")[1]) - 1]
        if instruction in self.fail_on:
            return _Run(RunStatus.FAILED, "tests did not pass")
        return _Run()


# --------------------------------------------------------------------------- #
# Planning spends nothing
# --------------------------------------------------------------------------- #
def test_planning_starts_nothing() -> None:
    plan = plan_batch("prj_1", ISSUES)
    assert len(plan.items) == 3
    assert all(item.status == "pending" and not item.run_id for item in plan.items)
    assert "Nothing has run" in plan.to_dict()["note"]


def test_the_queue_is_ordered_by_priority() -> None:
    """If the ceiling stops the batch, what ran must be what mattered most."""
    plan = plan_batch("prj_1", ISSUES)
    assert [item.key for item in plan.items] == ["QA-1", "QA-2", "QA-3"]


def test_an_unknown_priority_sorts_last_rather_than_first() -> None:
    plan = plan_batch(
        "prj_1",
        [{"key": "A", "summary": "something urgent", "priority": "Bananas"},
         {"key": "B", "summary": "something normal", "priority": "High"}],
    )
    assert [item.key for item in plan.items] == ["B", "A"]


def test_empty_and_duplicate_requirements_are_dropped() -> None:
    plan = plan_batch(
        "prj_1",
        [
            {"key": "QA-1", "requirement_text": "Automate resident registration"},
            {"key": "QA-1", "requirement_text": "Automate resident registration"},
            {"key": "QA-9", "requirement_text": "short"},
            {"key": "QA-8", "requirement_text": ""},
        ],
    )
    assert [item.key for item in plan.items] == ["QA-1"]


def test_the_estimate_is_pessimistic_without_history() -> None:
    plan = plan_batch("prj_no_history", ISSUES)
    assert plan.estimated_cost_usd == pytest.approx(FALLBACK_COST_PER_RUN_USD * 3)
    assert "pessimistic" in plan.estimate_basis


def test_an_estimate_over_the_ceiling_is_reported_before_running() -> None:
    plan = plan_batch("prj_1", ISSUES, max_cost_usd=0.01)
    assert plan.within_budget is False
    assert plan.to_dict()["within_budget"] is False


def test_max_items_caps_the_queue() -> None:
    many = [{"key": f"QA-{i}", "requirement_text": f"Automate feature number {i}"} for i in range(80)]
    assert len(plan_batch("prj_1", many, max_items=10).items) == 10


# --------------------------------------------------------------------------- #
# Executing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_every_item_runs_when_nothing_stops_it() -> None:
    engine = _Engine()
    result = await execute_batch(engine, plan_batch("prj_1", ISSUES))

    assert len(result.completed) == 3
    assert result.failed == [] and result.skipped == []
    assert engine.started[0] == "Automate resident registration", "highest priority ran first"


@pytest.mark.asyncio
async def test_one_failing_item_does_not_end_the_queue() -> None:
    engine = _Engine(fail_on={"Automate resident editing"})
    result = await execute_batch(engine, plan_batch("prj_1", ISSUES))

    assert len(engine.started) == 3
    assert [i.key for i in result.failed] == ["QA-2"]
    assert len(result.completed) == 2


@pytest.mark.asyncio
async def test_an_exception_is_contained_to_its_item() -> None:
    engine = _Engine(raise_on={"Automate resident editing"})
    result = await execute_batch(engine, plan_batch("prj_1", ISSUES))

    assert len(result.completed) == 2
    assert result.failed[0].error == "control plane exploded"


@pytest.mark.asyncio
async def test_the_batch_ceiling_stops_spending_and_says_what_was_skipped(monkeypatch) -> None:
    """The ceiling is the whole point; silently spending past it is the bug."""
    plan = plan_batch("prj_1", ISSUES, max_cost_usd=0.10)

    monkeypatch.setattr(
        "services.agent_engine.batch._run_outcome", lambda run_id: (0.06, 4, 3)
    )
    engine = _Engine()
    result = await execute_batch(engine, plan)

    assert len(engine.started) == 2, "stopped once the ceiling was reached"
    assert [i.key for i in result.skipped] == ["QA-3"]
    assert "ceiling" in result.stopped_reason
    assert "not attempted" in result.summary()


@pytest.mark.asyncio
async def test_no_ceiling_means_no_early_stop(monkeypatch) -> None:
    monkeypatch.setattr("services.agent_engine.batch._run_outcome", lambda run_id: (5.0, 1, 3))
    result = await execute_batch(_Engine(), plan_batch("prj_1", ISSUES, max_cost_usd=0))
    assert result.skipped == []


@pytest.mark.asyncio
async def test_results_serialise_with_every_item_accounted_for(monkeypatch) -> None:
    monkeypatch.setattr("services.agent_engine.batch._run_outcome", lambda run_id: (0.02, 5, 3))
    result = await execute_batch(_Engine(fail_on={"Automate resident search"}), plan_batch("prj_1", ISSUES))
    payload = result.to_dict()

    assert payload["succeeded"] + payload["failed"] + payload["skipped"] == 3
    assert len(payload["items"]) == 3
    assert payload["total_cost_usd"] == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_an_empty_plan_is_not_an_error() -> None:
    result = await execute_batch(_Engine(), BatchPlan(project_id="prj_1"))
    assert result.to_dict()["attempted"] == 0


@pytest.mark.asyncio
async def test_a_run_that_generated_nothing_is_not_reported_as_a_success(monkeypatch) -> None:
    """Otherwise a batch reports ten green items having produced two suites."""
    monkeypatch.setattr("services.agent_engine.batch._run_outcome", lambda run_id: (0.01, 0, 0))
    result = await execute_batch(_Engine(), plan_batch("prj_1", ISSUES))

    assert [item.status for item in result.completed] == ["no_work"] * 3
    payload = result.to_dict()
    assert payload["succeeded"] == 0
    assert payload["already_covered"] == 3
    assert "already covered" in result.summary()
    assert "0 generated tests" in result.summary()
