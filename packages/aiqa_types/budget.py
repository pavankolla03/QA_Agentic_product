"""Budgets and complexity scoring — the cost-control primitives.

Three independent ceilings guard every run: **requests**, **tokens** and **dollars**.
Requests matter because free tiers are request-limited; tokens matter because
context is what actually costs; dollars are what a finance team asks about.
Any one of them tripping stops the run and flags it for human review rather than
letting it burn.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any


class BudgetExceeded(RuntimeError):
    """A run hit one of its ceilings. Not an error in the platform — a policy stop."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind          # requests | input_tokens | output_tokens | cost | retries


@dataclass
class RunBudget:
    """Per-run spend guard. Checked before every call and updated after."""

    max_requests: int = 40
    max_input_tokens: int = 400_000
    max_output_tokens: int = 120_000
    max_cost_usd: float = 2.00
    max_retries: int = 2

    #: v1 compatibility. A single combined token ceiling; when supplied it seeds
    #: the input/output limits (roughly 3:1, which is the observed shape of QA
    #: prompts — large context in, modest code out).
    max_tokens: int = 0

    # Loop protection (see LangGraph orchestrator).
    max_correction_attempts: int = 3
    max_healing_attempts: int = 2
    max_execution_retries: int = 3

    # -- consumption --------------------------------------------------- #
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    spent_usd: float = 0.0
    retries: int = 0
    escalations: int = 0
    free_calls: int = 0
    paid_calls: int = 0
    #: Tokens we did *not* send because knowledge was cached/retrieved instead.
    tokens_saved: int = 0

    def __post_init__(self) -> None:
        if self.max_tokens:
            self.max_input_tokens = int(self.max_tokens * 0.75)
            self.max_output_tokens = int(self.max_tokens * 0.25)

    # -- v1 compatibility aliases --------------------------------------- #
    @property
    def used_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def calls(self) -> int:
        return self.requests

    def check(self) -> None:
        if self.max_requests and self.requests >= self.max_requests:
            raise BudgetExceeded(
                "requests", f"LLM request limit reached: {self.requests} of {self.max_requests}."
            )
        if self.max_input_tokens and self.input_tokens >= self.max_input_tokens:
            raise BudgetExceeded(
                "input_tokens",
                f"Input token limit reached: {self.input_tokens:,} of {self.max_input_tokens:,}.",
            )
        if self.max_output_tokens and self.output_tokens >= self.max_output_tokens:
            raise BudgetExceeded(
                "output_tokens",
                f"Output token limit reached: {self.output_tokens:,} of {self.max_output_tokens:,}.",
            )
        if self.max_cost_usd and self.spent_usd >= self.max_cost_usd:
            raise BudgetExceeded(
                "cost", f"Cost limit reached: ${self.spent_usd:.4f} of ${self.max_cost_usd:.2f}."
            )

    def would_exceed(self, estimated_input: int, estimated_cost: float = 0.0) -> str:
        """Pre-flight check: refuse a call we already know cannot fit."""
        if self.max_input_tokens and self.input_tokens + estimated_input > self.max_input_tokens:
            return "input_tokens"
        if self.max_cost_usd and self.spent_usd + estimated_cost > self.max_cost_usd:
            return "cost"
        if self.max_requests and self.requests + 1 > self.max_requests:
            return "requests"
        return ""

    def record(
        self,
        *,
        cost: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        free: bool = True,
    ) -> None:
        self.requests += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_tokens += cached_tokens
        self.spent_usd = round(self.spent_usd + cost, 8)
        if free:
            self.free_calls += 1
        else:
            self.paid_calls += 1

    def record_saving(self, tokens: int) -> None:
        """Record context we avoided sending (cache/retrieval hit)."""
        self.tokens_saved += max(0, tokens)

    # -- reporting ------------------------------------------------------ #
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_cost_usd - self.spent_usd)

    @property
    def remaining_requests(self) -> int:
        return max(0, self.max_requests - self.requests)

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "max_requests": self.max_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "tokens_saved": self.tokens_saved,
            "spent_usd": round(self.spent_usd, 6),
            "max_cost_usd": self.max_cost_usd,
            "free_calls": self.free_calls,
            "paid_calls": self.paid_calls,
            "escalations": self.escalations,
            "retries": self.retries,
        }

    @classmethod
    def from_config(cls, config: dict[str, Any] | None, **overrides: Any) -> RunBudget:
        cfg = dict(config or {})
        budget = cls(
            max_requests=int(cfg.get("max_requests", 40)),
            max_input_tokens=int(cfg.get("max_input_tokens", 400_000)),
            max_output_tokens=int(cfg.get("max_output_tokens", 120_000)),
            max_cost_usd=float(cfg.get("max_cost_usd", 2.0)),
            max_retries=int(cfg.get("max_retries", 2)),
            max_correction_attempts=int(cfg.get("max_correction_attempts", 3)),
            max_healing_attempts=int(cfg.get("max_healing_attempts", 2)),
            max_execution_retries=int(cfg.get("max_execution_retries", 3)),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(budget, key):
                setattr(budget, key, value)
        return budget


# --------------------------------------------------------------------------- #
# Provider request accounting
# --------------------------------------------------------------------------- #
@dataclass
class ProviderQuota:
    """Daily request ledger for a request-limited provider (e.g. OpenRouter free tier)."""

    provider: str
    daily_limit: int = 0
    reserve_threshold: int = 0
    day: str = field(default_factory=lambda: date.today().isoformat())
    used: int = 0

    def _roll(self) -> None:
        today = date.today().isoformat()
        if today != self.day:
            self.day, self.used = today, 0

    def record(self, count: int = 1) -> None:
        self._roll()
        self.used += count

    @property
    def remaining(self) -> int:
        self._roll()
        return max(0, self.daily_limit - self.used) if self.daily_limit else math.inf  # type: ignore[return-value]

    def available(self) -> bool:
        """False once we are into the reserve, so paid work can still get through."""
        if not self.daily_limit:
            return True
        self._roll()
        return (self.daily_limit - self.used) > self.reserve_threshold

    def snapshot(self) -> dict[str, Any]:
        self._roll()
        return {
            "provider": self.provider,
            "day": self.day,
            "used": self.used,
            "limit": self.daily_limit,
            "remaining": self.daily_limit - self.used if self.daily_limit else None,
            "available": self.available(),
        }


# --------------------------------------------------------------------------- #
# Complexity scoring
# --------------------------------------------------------------------------- #
#: Substrings that signal a genuinely hard reasoning problem.
_HARD_SIGNALS = (
    "root cause", "why did", "conflicting", "ambiguous", "trade-off", "race condition",
    "intermittent", "flaky", "regression", "refactor", "architecture", "edge case",
)
_EASY_SIGNALS = ("summarize", "classify", "list", "extract", "rename", "format", "tag")


def score_complexity(
    prompt: str,
    *,
    artifacts: int = 0,
    retry: int = 0,
    prior_failure: bool = False,
) -> float:
    """Deterministically score task difficulty in ``0..1``.

    Cheap, explainable, and — crucially — free. It decides whether a task may
    escalate from a free model to a paid one, so it must never itself cost money.
    """
    score = 0.0

    # Size is the strongest single signal.
    length = len(prompt or "")
    score += min(0.35, length / 40_000)

    # Breadth of artifacts under consideration.
    score += min(0.20, artifacts * 0.02)

    lowered = (prompt or "").lower()
    score += min(0.25, sum(0.06 for signal in _HARD_SIGNALS if signal in lowered))
    score -= min(0.15, sum(0.05 for signal in _EASY_SIGNALS if signal in lowered))

    # A retry means the cheap attempt already failed to satisfy us.
    score += min(0.25, retry * 0.15)
    if prior_failure:
        score += 0.15

    return max(0.0, min(1.0, round(score, 4)))
