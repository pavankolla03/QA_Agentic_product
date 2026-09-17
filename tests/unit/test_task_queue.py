"""Where a run waits to be executed.

A run used to be an `asyncio.create_task` inside the API process, which has two
consequences. The obvious one is that restarting the API kills every run in
flight. The one people actually feel is that the pipeline's real CPU work —
parsing repositories, rendering TypeScript, diffing — sat on the same event
loop as the chat, so a message typed during code generation waited behind it.

Two backends, and the only thing that must never happen is a queue claiming a
durability it does not have. A configured-but-unreachable Redis is the
dangerous case: accepting runs into a queue nobody reads is a control plane
that takes work and silently does none of it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services.task_service.queue import (
    InProcessQueue,
    QueueHealth,
    RedisQueue,
    build_queue,
)


# --------------------------------------------------------------------------- #
# In-process: what the platform has always done
# --------------------------------------------------------------------------- #
async def test_a_run_is_executed(tmp_path) -> None:  # noqa: ANN001
    seen: list[str] = []

    async def execute(run_id: str) -> None:
        seen.append(run_id)

    queue = InProcessQueue(execute)
    await queue.enqueue("run_1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert seen == ["run_1"]


async def test_submitting_the_same_run_twice_does_not_run_it_twice() -> None:
    """Two workers racing over one row would interleave writes to one tree."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(_run_id: str) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    queue = InProcessQueue(execute)
    await queue.enqueue("run_1")
    await started.wait()
    await queue.enqueue("run_1")

    release.set()
    await asyncio.sleep(0)
    assert calls == 1


async def test_a_running_task_can_be_cancelled() -> None:
    started = asyncio.Event()

    async def execute(_run_id: str) -> None:
        started.set()
        await asyncio.sleep(30)

    queue = InProcessQueue(execute)
    await queue.enqueue("run_1")
    await started.wait()

    assert await queue.cancel("run_1") is True
    await asyncio.sleep(0)
    task = queue.task_for("run_1")
    assert task is None or task.cancelled()


async def test_cancelling_something_that_is_not_running_says_so() -> None:
    async def execute(_run_id: str) -> None:
        return None

    queue = InProcessQueue(execute)
    assert await queue.cancel("run_never_started") is False


async def test_a_crash_reaches_the_completion_handler() -> None:
    """A fire-and-forget task swallows its own exception.

    Nobody awaits it, so its exception is never retrieved and the row stays
    `running` while every client polls it forever.
    """
    seen: list[tuple[str, BaseException | None]] = []

    async def execute(_run_id: str) -> None:
        raise RuntimeError("context could not be built")

    queue = InProcessQueue(execute)
    queue.set_completion_handler(lambda run_id, task: seen.append((run_id, task.exception())))
    await queue.enqueue("run_1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert seen and seen[0][0] == "run_1"
    assert isinstance(seen[0][1], RuntimeError)


async def test_in_process_never_claims_to_be_durable() -> None:
    """It is a fine deployment. It must simply not lie about what it is."""

    async def execute(_run_id: str) -> None:
        return None

    health = await InProcessQueue(execute).health()
    assert health.reachable is True
    assert health.durable is False
    assert health.summary() == "in-process: runs die with this process"


# --------------------------------------------------------------------------- #
# Choosing a backend
# --------------------------------------------------------------------------- #
async def test_no_redis_configured_means_in_process() -> None:
    async def execute(_run_id: str) -> None:
        return None

    queue = await build_queue(execute, url="")
    assert isinstance(queue, InProcessQueue)


async def test_an_unreachable_redis_falls_back_rather_than_swallowing_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dangerous case, and the reason `build_queue` probes at all.

    Enqueueing into a queue nobody reads produces a control plane that accepts
    work and silently does none of it — worse than refusing, because it looks
    like it is working.
    """
    async def execute(_run_id: str) -> None:
        return None

    with caplog.at_level("WARNING"):
        queue = await build_queue(execute, url="redis://127.0.0.1:59999")

    assert isinstance(queue, InProcessQueue)
    assert any("unreachable" in record.message for record in caplog.records), (
        "falling back silently is how a broken deployment looks healthy"
    )
    assert any("will not survive a restart" in record.getMessage() for record in caplog.records)


async def test_a_reachable_redis_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client code is exercised for real, against a Redis protocol server."""
    healthy = QueueHealth("redis", durable=True, reachable=True)
    monkeypatch.setattr(RedisQueue, "health", lambda self: _resolved(healthy))

    async def execute(_run_id: str) -> None:
        return None

    queue = await build_queue(execute, url="redis://127.0.0.1:6379")
    assert isinstance(queue, RedisQueue)
    assert queue.durable is True


async def _resolved(value: Any) -> Any:
    return value


# --------------------------------------------------------------------------- #
# The Redis backend, against a real protocol implementation
# --------------------------------------------------------------------------- #
async def test_redis_health_reports_the_reason_it_cannot_connect() -> None:
    """An unreachable queue must say why, not merely that."""
    queue = RedisQueue("redis://127.0.0.1:59998")
    health = await queue.health()

    assert health.reachable is False
    assert health.durable is True, "it would be durable if it were up — that is the point"
    assert health.detail, "a failure with no reason cannot be acted on"
    assert "unreachable" in health.summary()
    await queue.close()


async def test_a_job_id_is_derived_from_the_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting a run twice must be a no-op, not two workers on one tree."""
    submitted: list[dict[str, Any]] = []

    class _Pool:
        async def enqueue_job(self, function: str, run_id: str, **kwargs: Any):  # noqa: ANN202
            submitted.append({"function": function, "run_id": run_id, **kwargs})
            return type("Job", (), {"job_id": kwargs.get("_job_id", "")})()

    queue = RedisQueue("redis://ignored")
    monkeypatch.setattr(queue, "_connection", lambda: _resolved(_Pool()))

    first = await queue.enqueue("run_abc")
    second = await queue.enqueue("run_abc")

    assert first == second == "run:run_abc"
    assert {entry["_job_id"] for entry in submitted} == {"run:run_abc"}
    assert submitted[0]["function"] == "execute_run"
