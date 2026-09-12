"""Secret redaction.

**Every** string that is about to (a) enter an LLM prompt, (b) be written to a
trace/log, or (c) leave the process over the network passes through
:func:`redact`. This is the single hard guarantee that credentials never reach
a model provider.

Design notes
------------
* Patterns come from ``configs/security.yaml`` so security teams can extend
  them without touching code.
* A pattern may target a capture ``group`` so that ``password = "hunter2"``
  redacts only ``hunter2`` and keeps the surrounding context readable.
* Redaction is *stable*: the same secret always yields the same placeholder
  within a process, which keeps diffs and traces comparable without leaking
  the value.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from configs.settings import load_security_config


@dataclass(frozen=True)
class RedactionPattern:
    name: str
    regex: re.Pattern[str]
    group: int = 0
    severity: str = "high"


@dataclass
class RedactionResult:
    text: str
    hits: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.hits


class Redactor:
    """Compiled, reusable secret redactor."""

    _ENV_KEY_HINT = re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)[A-Z0-9_]*)\s*=\s*(\S+)"
    )

    def __init__(self, patterns: Iterable[RedactionPattern] | None = None) -> None:
        self.patterns: list[RedactionPattern] = list(patterns) if patterns is not None else _load_patterns()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _placeholder(name: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:8]
        return f"[REDACTED:{name.upper()}:{digest}]"

    def redact(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult(text=text)

        hits: list[str] = []
        out = text

        for pat in self.patterns:
            def _sub(match: re.Match[str], _name: str = pat.name, _grp: int = pat.group) -> str:
                try:
                    secret = match.group(_grp) if _grp else match.group(0)
                except IndexError:  # pragma: no cover - malformed pattern config
                    secret = match.group(0)
                if not secret:
                    return match.group(0)
                hits.append(_name)
                token = self._placeholder(_name, secret)
                if _grp:
                    whole = match.group(0)
                    return whole.replace(secret, token)
                return token

            out = pat.regex.sub(_sub, out)

        # Catch-all for `.env`-style assignments whose key *name* looks secret.
        def _env_sub(match: re.Match[str]) -> str:
            key, value = match.group(1), match.group(2)
            if value.startswith("[REDACTED:"):
                return match.group(0)
            hits.append("env_assignment")
            return f"{key}={self._placeholder('env', value)}"

        out = self._ENV_KEY_HINT.sub(_env_sub, out)
        return RedactionResult(text=out, hits=sorted(set(hits)))

    # ------------------------------------------------------------------ #
    def scan(self, text: str) -> list[str]:
        """Report secret kinds present without rewriting the text."""
        return self.redact(text).hits

    def redact_obj(self, obj: Any) -> Any:
        """Recursively redact strings inside dicts/lists/tuples."""
        if isinstance(obj, str):
            return self.redact(obj).text
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            mapped = [self.redact_obj(v) for v in obj]
            return type(obj)(mapped) if isinstance(obj, tuple) else mapped
        return obj


def _load_patterns() -> list[RedactionPattern]:
    cfg = load_security_config().get("redaction", {}) or {}
    if not cfg.get("enabled", True):
        return []
    patterns: list[RedactionPattern] = []
    for raw in cfg.get("patterns", []) or []:
        try:
            patterns.append(
                RedactionPattern(
                    name=raw["name"],
                    regex=re.compile(raw["regex"]),
                    group=int(raw.get("group", 0)),
                    severity=raw.get("severity", "high"),
                )
            )
        except (re.error, KeyError, TypeError):
            continue
    return patterns


_default: Redactor | None = None


def get_redactor() -> Redactor:
    global _default
    if _default is None:
        _default = Redactor()
    return _default


def redact(text: str) -> str:
    """Convenience: return the redacted text only."""
    return get_redactor().redact(text).text


def redact_with_hits(text: str) -> RedactionResult:
    return get_redactor().redact(text)


def contains_secret(text: str) -> bool:
    return bool(get_redactor().scan(text))


def reset_redactor() -> None:
    """Test hook — re-read patterns from config."""
    global _default
    _default = None
