"""Where a run waits to be executed.

Until now a run was an `asyncio.create_task` inside the API process. That has
two consequences, one obvious and one not:

* **Obvious.** Restart the API and every in-flight run dies. The database row
  survives saying `running`, which is a lie that startup reconciliation now
  cleans up after the fact.
* **Less obvious, and the one people actually feel.** The pipeline does real
  CPU work — parsing repositories, rendering TypeScript, diffing — on the same
  event loop that serves chat. A message typed while a run is generating code
  waits behind it. Separating the planes in the API is worth little if both
  planes still share one process.

So a run is handed to a queue. Two backends exist and both are honest about
what they are:

`InProcessQueue`
    What the platform has always done, kept as the default. Zero configuration,
    nothing to install, and it dies with the process — which it says out loud
    rather than implying durability it does not have.

`RedisQueue`
    A durable queue the API only writes to. Workers are separate processes, so
    a restart, a crash or a closed laptop loses nothing, and the API stays
    responsive because it is no longer doing the work.

Selection is by configuration and by what is actually reachable. A Redis URL
that does not answer falls back to in-process *and says so* — the alternative
is a control plane that accepts runs into a queue nobody is reading.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("aiqa.queue")

#: The arq function name workers register. One entry point; the run id is the
#: only argument, because everything else a run needs is already in its row.
EXECUTE_RUN = "execute_run"


@dataclass
class QueueHealth:
    """What this queue is and whether it can be relied on."""

    backend: str
    durable: bool
    reachable: bool
    detail: str = ""

    def summary(self) -> str:
        if not self.reachable:
            return f"{self.backend}: unreachable ({self.detail})"
        durability = "durable" if self.durable else "runs die with this process"
        return f"{self.backend}: {durability}"


class TaskQueue(ABC):
    """Hands a run to whatever will execute it."""

    backend = "abstract"
    durable = False

    @abstractmethod
    async def enqueue(self, run_id: str) -> str:
        """Submit a run. Returns a job identifier."""

    @abstractmethod
    async def cancel(self, run_id: str) -> bool:
        """Stop a run if it has not finished. True if something was stopped."""

    @abstractmethod
    async def health(self) -> QueueHealth:
        """Can this queue be relied on right now?"""

    async def close(self) -> None:
        return None


class InProcessQueue(TaskQueue):
    """Runs execute in the API process, as they always have.

    Kept deliberately, not as a stub: it is the only backend that needs no
    infrastructure, and a QA engineer trying the platform on a laptop should
    not have to stand up Redis to see it work. What it must never do is claim
    to be durable.
    """

    backend = "in-process"
    durable = False

    def __init__(self, execute: Any) -> None:
        #: `async def execute(run_id) -> Any`
        self._execute = execute
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._on_done: Any = None

    def set_completion_handler(self, handler: Any) -> None:
        """Called with (run_id, task) when a task finishes, however it finishes."""
        self._on_done = handler

    async def enqueue(self, run_id: str) -> str:
        existing = self._tasks.get(run_id)
        if existing and not existing.done():
            return run_id

        task = asyncio.create_task(self._execute(run_id))
        self._tasks[run_id] = task

        def finished(completed: asyncio.Task[Any]) -> None:
            self._tasks.pop(run_id, None)
            if self._on_done is not None:
                self._on_done(run_id, completed)

        task.add_done_callback(finished)
        return run_id

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def health(self) -> QueueHealth:
        return QueueHealth(self.backend, durable=False, reachable=True)

    def task_for(self, run_id: str) -> asyncio.Task[Any] | None:
        return self._tasks.get(run_id)


class RedisQueue(TaskQueue):
    """A durable queue. The API writes; separate workers read.

    Cancellation is deliberately not "delete the job". A run already picked up
    by a worker cannot be un-picked-up, and deleting its queue entry would
    leave the worker happily continuing with nothing watching. Instead the run
    row is the signal and the worker checks it — which also means a cancel
    issued from any channel, or from a different process entirely, is seen.
    """

    backend = "redis"
    durable = True

    def __init__(self, url: str) -> None:
        self.url = url
        self._pool: Any = None

    async def _connection(self) -> Any:
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self._pool = await create_pool(RedisSettings.from_dsn(self.url))
        return self._pool

    async def enqueue(self, run_id: str) -> str:
        pool = await self._connection()
        # `_job_id` is the run id, so submitting the same run twice is a no-op
        # rather than two workers racing over one row.
        job = await pool.enqueue_job(EXECUTE_RUN, run_id, _job_id=f"run:{run_id}")
        return job.job_id if job is not None else f"run:{run_id}"

    async def cancel(self, run_id: str) -> bool:
        """Ask the worker to stop. The run row carries the request."""
        from arq.jobs import Job

        pool = await self._connection()
        job = Job(f"run:{run_id}", pool)
        try:
            # Only removes it if no worker has started it. A started run is
            # stopped by the status check inside the worker loop.
            await job.abort(timeout=0.1)
            return True
        except Exception:  # noqa: BLE001 - already running, or already gone
            return False

    async def health(self) -> QueueHealth:
        try:
            pool = await self._connection()
            await pool.ping()
        except Exception as exc:  # noqa: BLE001
            return QueueHealth(self.backend, durable=True, reachable=False, detail=str(exc)[:160])
        return QueueHealth(self.backend, durable=True, reachable=True)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


async def build_queue(execute: Any, url: str = "") -> TaskQueue:
    """The best queue actually available, never a queue that only looks available.

    A configured Redis that does not answer is the dangerous case: accepting
    runs into a queue nobody reads produces a control plane that takes work and
    silently does none of it. Falling back is the right behaviour, and saying so
    loudly is the rest of it.
    """
    if not url:
        return InProcessQueue(execute)

    queue = RedisQueue(url)
    health = await queue.health()
    if health.reachable:
        log.info("task queue: redis at %s", url)
        return queue

    await queue.close()
    log.warning(
        "task queue: redis configured at %s but unreachable (%s) — falling back to in-process. "
        "Runs will not survive a restart.",
        url,
        health.detail,
    )
    return InProcessQueue(execute)
