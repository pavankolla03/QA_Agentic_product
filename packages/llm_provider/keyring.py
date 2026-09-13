"""Rotating between several API keys for one provider.

A free tier is rate-limited per key, not per account. With one key a long run
stops dead partway through the day; with two it carries on. That is the whole
purpose of this module.

**Not every 429 means the same thing.** OpenRouter enforces both a per-minute
burst limit and a per-day allowance, and both arrive as HTTP 429. An early
version of this file treated them identically and parked a key until midnight
because a run sent a handful of requests too quickly — throwing away a whole
day's allowance over a few seconds of burst. So a 429 now means *cool down and
rotate* unless the response actually says the daily quota is gone.

The other rules exist for the same reason — retire a key only for the reason it
actually failed:

* **402** — valid key, no credit. Parked until tomorrow.
* **401 / 403** — the key is wrong and will never work. Dropped for the session.
* **5xx** — the *provider* is unwell; this says nothing about the key, so the
  key keeps its place and normal fallback handles it.

Rotation is bounded to one pass through the ring per request: without that, a
provider-wide outage would burn every key's retry budget and turn a fast
failure into a minute of pointless traffic.

Keys never appear in logs. Only a fingerprint — the last four characters — is
ever rendered, so a stack trace or a shared terminal cannot leak one.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

#: Burst limit. Rotate away, come back shortly.
RATE_LIMIT_STATUSES = (429,)
#: The key is wrong; retrying it is pointless.
INVALID_KEY_STATUSES = (401, 403)
#: Valid key, no credit. Same handling as a spent daily allowance.
NO_CREDIT_STATUSES = (402,)

#: How long a burst-limited key is skipped before it is tried again.
DEFAULT_COOLDOWN_SECONDS = 45.0

#: Language that distinguishes "you are out for the day" from "you are going too
#: fast". Matched against the provider's error body.
_DAILY_LIMIT_PATTERNS = (
    r"per[- ]day",
    r"daily",
    r"free[- ]models? per day",
    r"quota (?:exceeded|exhausted)",
    r"limit.*\bday\b",
    r"add (?:more )?credits",
)
_DAILY_RE = re.compile("|".join(_DAILY_LIMIT_PATTERNS), re.IGNORECASE)


def fingerprint(key: str) -> str:
    """A safe way to refer to a key in a log line."""
    return f"...{key[-4:]}" if len(key) >= 4 else "(short key)"


def looks_daily(detail: str) -> bool:
    """Does this error body describe a daily allowance rather than a burst?"""
    return bool(detail) and bool(_DAILY_RE.search(detail))


@dataclass
class KeyState:
    key: str
    exhausted_on: str = ""          # ISO date the daily allowance ran out
    cooldown_until: float = 0.0     # monotonic clock; burst-limit backoff
    invalid: bool = False           # rejected outright; never retried this session
    requests: int = 0
    failures: int = 0

    def available(self) -> bool:
        if self.invalid:
            return False
        if self.exhausted_on == date.today().isoformat():
            return False
        return time.monotonic() >= self.cooldown_until

    def cooling(self) -> bool:
        return not self.invalid and time.monotonic() < self.cooldown_until

    def snapshot(self) -> dict[str, Any]:
        remaining = max(0.0, self.cooldown_until - time.monotonic())
        return {
            "key": fingerprint(self.key),
            "available": self.available(),
            "invalid": self.invalid,
            "exhausted_on": self.exhausted_on,
            "cooldown_s": round(remaining, 1),
            "requests": self.requests,
            "failures": self.failures,
        }


class KeyRing:
    """An ordered set of interchangeable API keys.

    Thread-safe: the router may complete several requests concurrently, and two
    of them rotating at once must not skip a key or hand out the same spent one
    twice.
    """

    def __init__(
        self, keys: list[str] | str | None = None, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    ) -> None:
        raw = [keys] if isinstance(keys, str) else list(keys or [])
        seen: set[str] = set()
        ordered: list[str] = []
        for key in raw:
            cleaned = (key or "").strip()
            # A duplicated key is not a second allowance; it is the same bucket.
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                ordered.append(cleaned)
        self._states = [KeyState(key=key) for key in ordered]
        self._index = 0
        self._cooldown = cooldown_seconds
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self._states)

    def __len__(self) -> int:
        return len(self._states)

    @property
    def configured(self) -> int:
        return len(self._states)

    def current(self) -> str:
        """The key to use now, or an empty string when none can be used."""
        with self._lock:
            return self._current_locked()

    def _current_locked(self) -> str:
        if not self._states:
            return ""
        for offset in range(len(self._states)):
            index = (self._index + offset) % len(self._states)
            if self._states[index].available():
                self._index = index
                return self._states[index].key
        return ""

    def record_success(self) -> None:
        with self._lock:
            if self._states:
                state = self._states[self._index]
                state.requests += 1
                # A success proves the key is fine; clear any leftover backoff.
                state.cooldown_until = 0.0

    def record_failure(self, status: int, detail: str = "") -> bool:
        """Note a failure and decide whether another key is worth trying.

        ``detail`` is the provider's error body, which is the only thing that
        separates a burst limit from a spent daily allowance.

        Returns True when the caller should retry with a different key.
        """
        with self._lock:
            if not self._states:
                return False
            state = self._states[self._index]
            state.failures += 1

            if status in INVALID_KEY_STATUSES:
                state.invalid = True
            elif status in NO_CREDIT_STATUSES:
                state.exhausted_on = date.today().isoformat()
            elif status in RATE_LIMIT_STATUSES:
                if looks_daily(detail):
                    state.exhausted_on = date.today().isoformat()
                else:
                    # Going too fast, not out of allowance. Parking this key
                    # until midnight would throw away the rest of the day.
                    state.cooldown_until = time.monotonic() + self._cooldown
            else:
                # Not the key's fault. Keep it, and let the caller's normal
                # retry/fallback logic deal with a provider-side problem.
                return False

            retired = state.key
            self._index = (self._index + 1) % len(self._states)
            successor = self._current_locked()
            # Worth retrying only if the ring found a *different* usable key.
            return bool(successor) and successor != retired

    def available_keys(self) -> int:
        with self._lock:
            return sum(1 for state in self._states if state.available())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": len(self._states),
                "available": sum(1 for s in self._states if s.available()),
                "cooling": sum(1 for s in self._states if s.cooling()),
                "active": fingerprint(self._states[self._index].key) if self._states else "",
                "keys": [state.snapshot() for state in self._states],
            }


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "INVALID_KEY_STATUSES",
    "NO_CREDIT_STATUSES",
    "RATE_LIMIT_STATUSES",
    "KeyRing",
    "KeyState",
    "fingerprint",
    "looks_daily",
]
