"""An agent may not retry forever.

The request timeout scales with how much output was asked for, which is right —
a model still writing is doing work. But retries multiply: three attempts at a
growing timeout is ten minutes on one agent, and a chat that shows nothing for
ten minutes is broken as far as anyone using it is concerned.

Worse output, available now and clearly labelled, beats better output nobody
waited for.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import base
from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import AgentName
from packages.aiqa_types.models import Project
from packages.llm_provider.base import LLMResponse


class _SlowAgent(BaseAgent):
    """Always replies with something unparseable, so every attempt retries."""

    name = AgentName.TEST_DESIGN

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, ctx: AgentContext) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def ask(self, ctx: AgentContext, system: str, user: str, **kwargs: Any) -> Any:
        self.calls += 1

        # The real LLMResponse, so this exercises the same parsing path.
        return LLMResponse(text="not json at all", finish_reason="stop")


def _ctx() -> AgentContext:
    return AgentContext(
        run_id="run_1",
        project=Project(org_id="org_1", name="p", repository_path="."),
        instruction="anything",
    )


def test_a_slow_agent_stops_retrying_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clock is already past the budget when the first retry is considered."""
    monkeypatch.setattr(base, "_AGENT_WALL_CLOCK_BUDGET", 0)
    agent = _SlowAgent()
    ctx = _ctx()

    result = asyncio.run(
        agent.ask_json(ctx, "sys", "user", task="t", fallback={"fell": "back"}, retries=3)
    )

    assert result == {"fell": "back"}
    assert agent.calls == 1, "the budget stops further attempts, it does not cancel the first"
    assert any("gave up after" in w for w in ctx.warnings), ctx.warnings


def test_a_generous_budget_still_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_AGENT_WALL_CLOCK_BUDGET", 3600)
    agent = _SlowAgent()
    ctx = _ctx()

    result = asyncio.run(
        agent.ask_json(ctx, "sys", "user", task="t", fallback={"fell": "back"}, retries=2)
    )

    assert result == {"fell": "back"}
    assert agent.calls == 3, "one attempt plus two retries"
    assert not any("gave up after" in w for w in ctx.warnings)


def test_the_budget_is_measured_in_minutes_not_seconds() -> None:
    """A budget under a minute would cut off ordinary work on free models."""
    assert base._AGENT_WALL_CLOCK_BUDGET >= 120
