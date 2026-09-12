"""Provider-agnostic LLM interface.

Every provider implements :class:`BaseProvider`. Agents never import a provider
directly — they ask the model router for a capability ("coding", "reasoning")
and get back whatever is configured and healthy. That is what makes models
replaceable, as the spec requires.
"""

from __future__ import annotations

import abc
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from packages.aiqa_types.enums import Role
from packages.aiqa_types.models import TokenUsage
from packages.security.redaction import redact_with_hits


# --------------------------------------------------------------------------- #
# Messages & requests
# --------------------------------------------------------------------------- #
@dataclass
class ChatMessage:
    role: Role | str = Role.USER
    content: str = ""
    name: str | None = None

    def to_dict(self) -> dict[str, str]:
        role = self.role.value if isinstance(self.role, Role) else str(self.role)
        return {"role": role, "content": self.content}

    @staticmethod
    def system(content: str) -> ChatMessage:
        return ChatMessage(Role.SYSTEM, content)

    @staticmethod
    def user(content: str) -> ChatMessage:
        return ChatMessage(Role.USER, content)

    @staticmethod
    def assistant(content: str) -> ChatMessage:
        return ChatMessage(Role.ASSISTANT, content)


@dataclass
class LLMRequest:
    messages: list[ChatMessage] = field(default_factory=list)
    model: str = ""
    temperature: float = 0.1
    max_tokens: int = 4096
    json_mode: bool = False
    stop: list[str] = field(default_factory=list)
    timeout_seconds: int = 180
    task: str = ""            # agent/task marker — used for routing + mock scripting
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_text(self) -> str:
        return "\n\n".join(f"[{m.to_dict()['role']}]\n{m.content}" for m in self.messages)

    def redacted(self) -> tuple[LLMRequest, list[str]]:
        """Return a copy whose message contents carry no secrets.

        This is called by every provider *before* the payload leaves the
        process. It is the last line of defence behind the tool-level guards.
        """
        hits: list[str] = []
        safe_messages: list[ChatMessage] = []
        for m in self.messages:
            result = redact_with_hits(m.content)
            hits.extend(result.hits)
            safe_messages.append(ChatMessage(role=m.role, content=result.text, name=m.name))
        clone = LLMRequest(
            messages=safe_messages,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            json_mode=self.json_mode,
            stop=list(self.stop),
            timeout_seconds=self.timeout_seconds,
            task=self.task,
            metadata=dict(self.metadata),
        )
        return clone, sorted(set(hits))


@dataclass
class LLMResponse:
    text: str = ""
    model: str = ""
    provider: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    finish_reason: str = "stop"
    cost_usd: float = 0.0
    redacted_kinds: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- structured output helpers ------------------------------------- #
    def json(self, default: Any = None) -> Any:
        """Best-effort extraction of a JSON object from the completion.

        Models wrap JSON in prose or fences far too often for ``json.loads``
        alone to be reliable, so we try progressively looser strategies.
        """
        return extract_json(self.text, default)


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str, default: Any = None) -> Any:
    if not text:
        return default
    candidates: list[str] = []

    stripped = text.strip()
    candidates.append(stripped)

    for match in _FENCE_RE.findall(text):
        candidates.append(match.strip())

    # Widest balanced {...} / [...] span
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            # Tolerate trailing commas, a very common model slip.
            repaired = re.sub(r",(\s*[}\]])", r"\1", cand)
            try:
                return json.loads(repaired)
            except (json.JSONDecodeError, TypeError):
                continue
    return default


def estimate_tokens(text: str) -> int:
    """Cheap heuristic used when a provider does not report usage."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class ProviderError(RuntimeError):
    """Raised when a provider call fails in a way the router should fall back from."""

    def __init__(self, provider: str, message: str, retryable: bool = True, status: int = 0) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message
        self.retryable = retryable
        self.status = status


# --------------------------------------------------------------------------- #
# Provider contract
# --------------------------------------------------------------------------- #
class BaseProvider(abc.ABC):
    name: str = "base"
    supports_embeddings: bool = False
    requires_api_key: bool = True

    def __init__(self, api_key: str = "", base_url: str = "", default_model: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model

    # -- lifecycle ------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return bool(self.api_key) if self.requires_api_key else True

    async def health(self) -> bool:
        """Cheap liveness probe. Providers may override with a real call."""
        return self.configured

    async def close(self) -> None:  # pragma: no cover - most providers are stateless
        return None

    # -- core ----------------------------------------------------------- #
    @abc.abstractmethod
    async def _chat(self, req: LLMRequest) -> LLMResponse:
        """Provider-specific implementation. Receives an already-redacted request."""

    async def chat(self, req: LLMRequest) -> LLMResponse:
        """Public entry point: redact → call → time → normalise usage."""
        safe, hits = req.redacted()
        if not safe.model:
            safe.model = self.default_model
        started = time.perf_counter()
        resp = await self._chat(safe)
        resp.latency_ms = resp.latency_ms or int((time.perf_counter() - started) * 1000)
        resp.provider = self.name
        resp.model = resp.model or safe.model
        resp.redacted_kinds = hits
        if not resp.usage.total_tokens:
            pt = resp.usage.prompt_tokens or estimate_tokens(safe.prompt_text)
            ct = resp.usage.completion_tokens or estimate_tokens(resp.text)
            resp.usage = TokenUsage(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct)
        return resp

    async def embed(self, texts: Sequence[str], model: str = "") -> list[list[float]]:
        raise NotImplementedError(f"{self.name} does not support embeddings")

    async def stream(self, req: LLMRequest) -> AsyncIterator[str]:  # pragma: no cover - optional
        """Default streaming: yield the full completion once."""
        resp = await self.chat(req)
        yield resp.text
