"""The worker process: where runs actually execute.

Separating this from the API is the point of the whole phase, and the reason is
not only durability. The pipeline does real CPU work — parsing repositories,
rendering TypeScript, diffing — and while it ran inside the API process that
work sat on the same event loop as the chat. A message typed during code
generation waited behind it. Splitting the conversation plane from the task
plane inside one process was worth something; splitting the processes is what
makes it true.

Run it beside the API:

    aiqa worker                 # or: arq services.worker.main.WorkerSettings

A worker holds no state the API needs. Everything a run requires is already in
its row, which is what makes it safe to have several, to restart them, and to
lose one mid-run — the next startup reconciles what the dead one left behind.
"""

from __future__ import annotations

import logging
from typing import Any

from configs.settings import get_settings
from packages.aiqa_types.enums import RunStatus
from services.observability.db import init_db, session_scope
from services.observability.models import RunRow

log = logging.getLogger("aiqa.worker")


async def execute_run(_context: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Drive one run to its next stopping point.

    Deliberately thin. Everything about how a run executes lives in the engine,
    so a worker and an in-process execution take exactly the same path and
    cannot drift — a bug reproducible in one is reproducible in the other.
    """
    from services.agent_engine.engine import get_engine

    if _cancelled(run_id):
        # The row is the cancellation signal, and it can be written by any
        # process at any time. Checking it here means a cancel issued while the
        # job sat in the queue is honoured before any work is done.
        log.info("run %s was cancelled before it started", run_id)
        return {"run_id": run_id, "status": RunStatus.CANCELLED.value}

    engine = get_engine()
    result = await engine.execute(run_id)
    return {
        "run_id": run_id,
        "status": getattr(result, "status", ""),
        "error": getattr(result, "error", ""),
    }


def _cancelled(run_id: str) -> bool:
    try:
        with session_scope() as session:
            row = session.get(RunRow, run_id)
            return row is not None and row.status == RunStatus.CANCELLED.value
    except Exception:  # noqa: BLE001 - a bookkeeping read must not lose the job
        log.debug("could not read run %s while checking for cancellation", run_id, exc_info=True)
        return False


async def startup(_context: dict[str, Any]) -> None:
    init_db()
    log.info("worker ready")


async def shutdown(_context: dict[str, Any]) -> None:
    log.info("worker stopping")


def _redis_settings() -> Any:
    from arq.connections import RedisSettings

    url = getattr(get_settings(), "redis_url", "")
    if not url:
        raise SystemExit(
            "AIQA_REDIS_URL is not set. A worker with no queue to read has nothing to do — "
            "set it, or run without workers and the API will execute runs in-process."
        )
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    """arq's entry point.

    `max_jobs = 1` by default and on purpose. A run drives a browser, a
    compiler and a test runner against one working tree; two runs sharing that
    tree would interleave file writes and produce a diff belonging to neither.
    Scale by adding workers — each with its own checkout — not by raising this.
    """

    functions = [execute_run]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 1
    #: A full run on free models is minutes. This has to exceed the slowest
    #: plausible one or arq kills work that is progressing.
    job_timeout = 3600
    #: Runs are not idempotent — they write files and spend budget. A retry
    #: after a worker dies mid-run would apply a half-finished change set on top
    #: of itself, so a dead run is reconciled at startup instead.
    max_tries = 1
    keep_result = 3600

    @property
    def redis_settings(self) -> Any:  # pragma: no cover - arq reads this at startup
        return _redis_settings()
