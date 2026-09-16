"""The timeout has to fit the answer being asked for.

A flat timeout is two different limits wearing one hat: "this request has
hung" and "this answer is too long to wait for". Raising the retry ceiling
from 12,000 to 24,000 tokens without touching the 150-second timeout turned
truncation — which is recoverable, the retry just asks for more room — into a
timeout, which is not. One test-design call sat for twelve minutes before
anyone looked at the process list.
"""

from __future__ import annotations

from packages.llm_provider.base import LLMRequest


def test_a_short_request_keeps_the_configured_timeout() -> None:
    """The floor is the config; nothing gets *less* time than it asked for."""
    assert LLMRequest(max_tokens=2000, timeout_seconds=150).effective_timeout == 150
    assert LLMRequest(max_tokens=500, timeout_seconds=150).effective_timeout == 150


def test_a_long_request_gets_proportionate_time() -> None:
    """Free models run at roughly 60-100 tokens a second."""
    generous = LLMRequest(max_tokens=16000, timeout_seconds=150).effective_timeout
    assert generous > 150
    # At least 50 tokens per second of allowance, which is below the slowest
    # rate observed — the point is to not cut off work that is progressing.
    assert generous >= 16000 / 50


def test_the_allowance_is_monotonic() -> None:
    previous = 0
    for tokens in (1000, 4000, 9000, 16000, 24000):
        current = LLMRequest(max_tokens=tokens, timeout_seconds=150).effective_timeout
        assert current >= previous
        previous = current


def test_a_generous_config_is_never_reduced() -> None:
    """Someone who set 600s for a slow local model keeps it."""
    assert LLMRequest(max_tokens=1000, timeout_seconds=600).effective_timeout == 600
