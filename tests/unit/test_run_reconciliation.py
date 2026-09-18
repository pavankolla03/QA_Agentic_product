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

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.aiqa_types.enums import RunStatus
from services.agent_engine.engine import AgentEngine
from services.observability.db import session_scope
from services.observability.models import RunEventRow, RunRow


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


def _run(run_id: str, status: RunStatus, *, quiet_for: float = 3600.0) -> None:
    """A run that has said nothing for `quiet_for` seconds.

    Silence is what marks a run dead, so a test about dead runs has to create
    one that has actually been silent. The default is an hour, comfortably past
    the grace period; pass a small number for a run that is still working.
    """
    stamp = datetime.now(timezone.utc) - timedelta(seconds=quiet_for)
    with session_scope() as session:
        session.add(
            RunRow(
                id=run_id,
                project_id=PROJECT_ID,
                instruction="anything",
                mode="full",
                status=status.value,
                created_at=stamp,
                started_at=stamp,
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


# --------------------------------------------------------------------------- #
# The other direction, which cost a real run
# --------------------------------------------------------------------------- #
def test_a_run_that_is_still_working_is_left_alone(engine: AgentEngine) -> None:
    """Absence of evidence was deciding this, and it decided wrong.

    A second control plane was started while the first was mid-run. It could not
    bind the port and exited — but it had already run its startup bookkeeping,
    and the premise "at startup nothing is in flight" is false for a process
    that is not the only one. It marked a healthy run, by then at the reporting
    stage, as `failed`.

    Aliveness is now something the run demonstrates. Its own events are the
    heartbeat, and a run that has spoken recently is working, whoever is driving
    it.
    """
    _run("run_alive", RunStatus.RUNNING, quiet_for=5)
    with session_scope() as session:
        session.add(
            RunEventRow(
                id="evt_alive",
                run_id="run_alive",
                type="agent_started",
                message="exploration",
                created_at=datetime.now(timezone.utc),
            )
        )

    assert engine.reconcile_interrupted_runs() == 0
    assert _status("run_alive") == RunStatus.RUNNING.value


def test_a_run_queued_moments_ago_is_not_collected(engine: AgentEngine) -> None:
    """It has emitted nothing because it has not started, not because it died."""
    _run("run_just_queued", RunStatus.QUEUED, quiet_for=2)

    assert engine.reconcile_interrupted_runs() == 0
    assert _status("run_just_queued") == RunStatus.QUEUED.value


def test_a_long_silence_is_still_collected(engine: AgentEngine) -> None:
    """The grace period is generous, not infinite.

    Leaving a dead run as `running` until the next restart is the cheap error;
    leaving it forever is the sidebar showing something that never finishes.
    """
    _run("run_silent", RunStatus.RUNNING, quiet_for=7200)
    with session_scope() as session:
        session.add(
            RunEventRow(
                id="evt_silent",
                run_id="run_silent",
                type="agent_started",
                message="exploration",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=7200),
            )
        )

    assert engine.reconcile_interrupted_runs() >= 1
    assert _status("run_silent") == RunStatus.FAILED.value

