"""Key rotation and per-model backoff.

Free tiers fail in ways paid tiers do not: a burst limit and a spent daily
allowance arrive as the same HTTP 429, and one busy model says nothing about
the next. Both distinctions were got wrong first time and both were caught by
running against real keys:

* treating a burst 429 as a daily exhaustion parked a key until midnight
  because a run sent a few requests too quickly;
* treating a model's 429 as a provider failure disabled every model on that
  provider, and the whole run silently fell through to the offline stub while
  reporting success.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from packages.llm_provider import providers as provider_module
from packages.llm_provider.keyring import KeyRing, fingerprint, looks_daily
from packages.llm_provider.providers import (
    OpenRouterProvider,
    cool_model,
    is_upstream_error,
    model_cooling,
    rejects_structured_output,
)

BURST = '{"error":{"message":"Rate limit exceeded, please slow down"}}'
DAILY = '{"error":{"message":"You have exceeded your free-models-per-day limit"}}'
UPSTREAM = '{"error":{"message":"Provider returned error","metadata":{"provider_name":"Novita"}}}'


@pytest.fixture(autouse=True)
def _clear_model_cooldowns():
    provider_module._MODEL_COOLDOWN.clear()
    provider_module._NO_STRUCTURED_OUTPUT.clear()
    yield
    provider_module._MODEL_COOLDOWN.clear()
    provider_module._NO_STRUCTURED_OUTPUT.clear()


# --------------------------------------------------------------------------- #
# The ring
# --------------------------------------------------------------------------- #
def test_a_burst_limit_does_not_cost_a_whole_day() -> None:
    """The bug that made this file necessary."""
    ring = KeyRing(["key-one", "key-two"])
    assert ring.record_failure(429, BURST) is True

    state = ring._states[0]
    assert state.exhausted_on == "", "a burst limit is not a spent allowance"
    assert state.cooling(), "it should be skipped briefly, not retired"
    assert ring.current() == "key-two"


def test_a_daily_limit_does_park_the_key() -> None:
    ring = KeyRing(["key-one", "key-two"])
    ring.record_failure(429, DAILY)
    assert ring._states[0].exhausted_on == date.today().isoformat()
    assert ring.current() == "key-two"


def test_an_invalid_key_is_dropped_for_the_session() -> None:
    ring = KeyRing(["bad", "good"])
    ring.record_failure(401, "invalid api key")
    assert ring._states[0].invalid is True
    assert ring.current() == "good"


def test_no_credit_is_treated_as_a_spent_allowance() -> None:
    ring = KeyRing(["key-one", "key-two"])
    ring.record_failure(402, "insufficient credits")
    assert ring._states[0].exhausted_on == date.today().isoformat()


def test_a_server_error_does_not_blame_the_key() -> None:
    """A 500 is the provider's problem and says nothing about the key."""
    ring = KeyRing(["key-one", "key-two"])
    assert ring.record_failure(503, "upstream unavailable") is False
    assert ring.current() == "key-one"


def test_a_cooldown_expires() -> None:
    ring = KeyRing(["only"], cooldown_seconds=0.05)
    ring.record_failure(429, BURST)
    assert ring.current() == ""
    time.sleep(0.08)
    assert ring.current() == "only"


def test_success_clears_a_stale_cooldown() -> None:
    ring = KeyRing(["only"], cooldown_seconds=60)
    ring.record_failure(429, BURST)
    ring._index = 0
    ring.record_success()
    assert ring.current() == "only"


def test_a_duplicated_key_is_one_allowance_not_two() -> None:
    ring = KeyRing(["same", "same"])
    assert len(ring) == 1


def test_the_last_key_failing_leaves_nothing_to_retry() -> None:
    ring = KeyRing(["only"])
    assert ring.record_failure(429, DAILY) is False
    assert ring.current() == ""


def test_keys_never_appear_in_a_snapshot() -> None:
    """A snapshot goes into logs and dashboards."""
    ring = KeyRing(["sk-or-v1-supersecretvalue"])
    rendered = str(ring.snapshot())
    assert "supersecret" not in rendered
    assert "...alue" in rendered


def test_fingerprint_survives_a_short_key() -> None:
    assert fingerprint("ab") == "(short key)"


@pytest.mark.parametrize("body,expected", [(DAILY, True), (BURST, False), ("", False)])
def test_daily_detection(body: str, expected: bool) -> None:
    assert looks_daily(body) is expected


# --------------------------------------------------------------------------- #
# Per-model backoff
# --------------------------------------------------------------------------- #
def test_an_upstream_429_is_distinguished_from_an_account_one() -> None:
    assert is_upstream_error(UPSTREAM) is True
    assert is_upstream_error(DAILY) is False


def test_a_busy_model_is_skipped_but_the_provider_is_not() -> None:
    """The failure that made a whole live run fall through to the stub."""
    provider = OpenRouterProvider(api_keys=["key-one"])
    cool_model("busy/model:free")

    assert provider.model_available("busy/model:free") is False
    assert provider.model_available("other/model:free") is True
    assert provider.configured is True, "the key is fine; only that model is busy"


def test_a_model_cooldown_expires() -> None:
    cool_model("busy/model:free", seconds=0.05)
    assert model_cooling("busy/model:free") is True
    time.sleep(0.08)
    assert model_cooling("busy/model:free") is False


# --------------------------------------------------------------------------- #
# Models that cannot do structured output
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "detail",
    [
        "model: x does not support feature: structured-outputs",
        "Unsupported parameter: 'response_format'",
    ],
)
def test_structured_output_rejection_is_recognised(detail: str) -> None:
    assert rejects_structured_output(detail) is True


def test_an_ordinary_400_is_not_mistaken_for_it() -> None:
    assert rejects_structured_output("invalid model id") is False


def test_a_model_known_to_refuse_is_not_sent_the_parameter() -> None:
    from packages.llm_provider.base import ChatMessage, LLMRequest

    provider = OpenRouterProvider(api_keys=["key-one"])
    request = LLMRequest(
        messages=[ChatMessage.user("hi")], model="picky/model:free", max_tokens=50, json_mode=True
    )
    assert "response_format" in provider._payload(request)

    provider_module._NO_STRUCTURED_OUTPUT.add("picky/model:free")
    assert "response_format" not in provider._payload(request)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_the_provider_reports_no_key_rather_than_pretending() -> None:
    provider = OpenRouterProvider(api_keys=[])
    assert provider.configured is False


def test_the_active_key_is_used_for_the_header() -> None:
    provider = OpenRouterProvider(api_keys=["first", "second"])
    assert provider._headers()["Authorization"] == "Bearer first"

    provider.keys.record_failure(429, DAILY)
    assert provider._headers()["Authorization"] == "Bearer second"
