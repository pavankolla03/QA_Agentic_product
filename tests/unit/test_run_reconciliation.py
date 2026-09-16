"""A run whose process is gone must not still say it is running.

A run lives in two places: a row in the database and a task in the engine's
process. Stop the process — a restart, a crash, a closed laptop — and the row
is left saying `running` with nothing behind it. The sidebar shows a run that
will never finish, and the chat can attach to a stream that will never emit,
which looks exactly like the product hanging.

Two of these accumulated over one afternoon of restarts, one of them the "hi"
that started this whole thread.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.aiqa_types.enums import RunStatus
from services.agent_engine.engine import AgentEngine
from services.observability.db import session_scope
from services.observability.models import RunRow


@pytest.fixture
def engine(project) -> AgentEngine:  # noqa: ANN001 - conftest fixture
    """`project` comes first so the runs below satisfy their foreign key."""
    return AgentEngine(offline=True)


@pytest.fixture(autouse=True)
def _project_id(project) -> str:  # noqa: ANN001 - conftest fixture
    global PROJECT_ID
    PROJECT_ID = project.id
    return project.id


PROJECT_ID = ""


def _run(run_id: str, status: RunStatus) -> None:
    with session_scope() as session:
        session.add(
            RunRow(
                id=run_id,
                project_id=PROJECT_ID,
                instruction="anything",
                mode="full",
                status=status.value,
            )
        )


def _status(run_id: str) -> str:
    with session_scope() as session:
        row = session.get(RunRow, run_id)
        assert row is not None
        return row.status


def _error(run_id: str) -> str:
    with session_scope() as session:
        row = session.get(RunRow, run_id)
        assert row is not None
        return row.error or ""


def test_a_running_row_with_no_process_is_failed(engine: AgentEngine) -> None:
    _run("run_reconcile_1", RunStatus.RUNNING)
    assert engine.reconcile_interrupted_runs() >= 1
    assert _status("run_reconcile_1") == RunStatus.FAILED.value
    assert "interrupted" in _error("run_reconcile_1"), "say why, or it reads as a real failure"


def test_a_queued_row_is_failed_too(engine: AgentEngine) -> None:
    """Queued means "about to start in a process that no longer exists"."""
    _run("run_reconcile_2", RunStatus.QUEUED)
    engine.reconcile_interrupted_runs()
    assert _status("run_reconcile_2") == RunStatus.FAILED.value


@pytest.mark.parametrize(
    "status",
    [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.WAITING_APPROVAL],
)
def test_a_run_that_reached_a_conclusion_is_left_alone(engine: AgentEngine, status: RunStatus) -> None:
    """Including `waiting_approval` — that run is not lost, it is waiting for a person."""
    run_id = f"run_reconcile_{status.value}"
    _run(run_id, status)
    engine.reconcile_interrupted_runs()
    assert _status(run_id) == status.value


def test_reconciling_twice_changes_nothing_the_second_time(engine: AgentEngine) -> None:
    _run("run_reconcile_twice", RunStatus.RUNNING)
    engine.reconcile_interrupted_runs()
    before = _error("run_reconcile_twice")
    assert engine.reconcile_interrupted_runs() == 0
    assert _error("run_reconcile_twice") == before


def teardown_module() -> None:
    with session_scope() as session:
        for row in session.execute(
            select(RunRow).where(RunRow.id.like("run_reconcile_%"))
        ).scalars():
            session.delete(row)
