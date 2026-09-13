"""Concrete LLM providers: Ollama, OpenRouter, OpenAI, Anthropic, Gemini, hashing.

All network providers share an httpx client with sane timeouts and surface
failures as :class:`ProviderError` so the model router can fall back.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Sequence
from typing import Any

import httpx

from packages.aiqa_types.models import TokenUsage
from packages.llm_provider.base import BaseProvider, LLMRequest, LLMResponse, ProviderError
from packages.llm_provider.keyring import KeyRing, fingerprint

log = logging.getLogger("aiqa.provider")


def _client(timeout: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))


def _raise_for(provider: str, resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        retryable = resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504)
        body = resp.text[:400]
        raise ProviderError(provider, f"HTTP {resp.status_code}: {body}", retryable=retryable, status=resp.status_code)


# --------------------------------------------------------------------------- #
# OpenAI-compatible family (OpenAI, OpenRouter, Ollama's /v1, MLX servers)
# --------------------------------------------------------------------------- #
#: Providers that bill a discounted rate for a re-sent prompt prefix, but only
#: when the request marks it. Everyone else (OpenAI, DeepSeek, Gemini) caches
#: automatically on an exact prefix match and needs no flag — for those, the
#: saving comes from keeping the prefix byte-identical, which the agents do.
_EXPLICIT_CACHE_MARKERS = ("anthropic", "claude")


#: Models that are rate limited right now: model id -> monotonic time when it
#: may be tried again.
#:
#: This is separate from the key ring on purpose. OpenRouter proxies to upstream
#: hosts that each enforce their own limits, so a 429 from one free model says
#: nothing about the next one in the tier. Conflating the two disabled the whole
#: provider whenever a single model was busy, and every call in the run fell
#: through to the offline stub — a pipeline that reported success having spoken
#: to no model at all.
_MODEL_COOLDOWN: dict[str, float] = {}

#: A busy free model usually clears quickly; the tier has other entries to use
#: in the meantime, so this does not need to be long.
MODEL_COOLDOWN_SECONDS = 30.0


def model_cooling(model: str) -> bool:
    until = _MODEL_COOLDOWN.get(model, 0.0)
    if not until:
        return False
    if time.monotonic() >= until:
        _MODEL_COOLDOWN.pop(model, None)
        return False
    return True


def cool_model(model: str, seconds: float = MODEL_COOLDOWN_SECONDS) -> None:
    _MODEL_COOLDOWN[model] = time.monotonic() + seconds


def is_upstream_error(detail: str) -> bool:
    """Did the model's host refuse, rather than OpenRouter itself?

    OpenRouter wraps an upstream failure as "Provider returned error" and names
    the host. Its own account-level limits carry neither marker.
    """
    lowered = (detail or "").lower()
    return "provider returned error" in lowered or "provider_name" in lowered


#: Models observed to reject `response_format`. Populated at runtime rather
#: than hard-coded: which upstream provider serves a given free model changes
#: without notice, and so does whether that provider implements the feature.
_NO_STRUCTURED_OUTPUT: set[str] = set()

#: The upstream complaint, which is not a standard error code.
_STRUCTURED_OUTPUT_REJECTIONS = (
    "does not support feature: structured-outputs",
    "structured outputs are not supported",
    "response_format is not supported",
    "unsupported parameter: 'response_format'",
)


def rejects_structured_output(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _STRUCTURED_OUTPUT_REJECTIONS)


def _wants_cache(req: LLMRequest) -> bool:
    """Did the caller mark a prefix worth caching?

    The router sets this only when the stable prefix clears the configured
    minimum, so a short system prompt never pays the cache-write premium.
    """
    return int((req.metadata or {}).get("cache_prefix_chars", 0)) > 0


def _cache_breakpoint(text: str) -> list[dict[str, Any]]:
    """A single text block marked as the end of the cacheable prefix."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class OpenAICompatibleProvider(BaseProvider):
    """Implements the `/chat/completions` contract shared by many providers."""

    name = "openai"
    supports_embeddings = True
    chat_path = "/chat/completions"
    embed_path = "/embeddings"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    #: Set by subclasses that can forward an explicit cache marker upstream.
    supports_explicit_cache = False

    def _payload(self, req: LLMRequest) -> dict[str, Any]:
        messages = [m.to_dict() for m in req.messages]
        model = req.model or self.default_model

        if (
            self.supports_explicit_cache
            and _wants_cache(req)
            and any(marker in model.lower() for marker in _EXPLICIT_CACHE_MARKERS)
        ):
            # Anthropic models charge a discounted rate for a cached prefix, but
            # only when the request says where that prefix ends. Marking the
            # system message is enough: everything before the breakpoint is
            # cached, and the system prompt is the part that repeats verbatim.
            for message in messages:
                if message.get("role") == "system" and isinstance(message.get("content"), str):
                    message["content"] = _cache_breakpoint(message["content"])
                    break

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.stop:
            payload["stop"] = req.stop
        if req.json_mode and model not in _NO_STRUCTURED_OUTPUT:
            # Asked for, not relied upon: the system prompt already demands bare
            # JSON and `ask_json` parses defensively, so a model that cannot
            # honour this still produces usable output.
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _chat(self, req: LLMRequest) -> LLMResponse:
        async with _client(req.timeout_seconds) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}{self.chat_path}", headers=self._headers(), json=self._payload(req)
                )
            except httpx.HTTPError as exc:
                raise ProviderError(self.name, f"transport error: {exc}", retryable=True) from exc
            _raise_for(self.name, resp)
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(self.name, f"no choices returned: {str(data)[:200]}", retryable=True)
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            total_tokens=int(usage_raw.get("total_tokens", 0)),
            cached_tokens=int((usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
        )
        return LLMResponse(
            text=text,
            model=data.get("model", req.model),
            usage=usage,
            finish_reason=choices[0].get("finish_reason", "stop"),
            raw={"id": data.get("id", "")},
        )

    async def embed(self, texts: Sequence[str], model: str = "") -> list[list[float]]:
        async with _client(120) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}{self.embed_path}",
                    headers=self._headers(),
                    json={"model": model or self.default_model, "input": list(texts)},
                )
            except httpx.HTTPError as exc:
                raise ProviderError(self.name, f"embedding transport error: {exc}") from exc
            _raise_for(self.name, resp)
            data = resp.json()
        return [item["embedding"] for item in data.get("data", [])]

    async def health(self) -> bool:
        if not self.configured:
            return False
        async with _client(8) as client:
            try:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers())
                return resp.status_code < 500
            except httpx.HTTPError:
                return False


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"

    def __init__(self, api_key: str = "", base_url: str = "https://api.openai.com/v1", default_model: str = "gpt-4o-mini") -> None:
        super().__init__(api_key=api_key, base_url=base_url, default_model=default_model)


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter — the cheapest path to many free models."""

    name = "openrouter"
    #: OpenRouter passes `cache_control` straight through to Anthropic models.
    #: For the free models it routes to, caching is either automatic or
    #: irrelevant, so the marker is only added when the model bills for it.
    supports_explicit_cache = True

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://openrouter.ai/api/v1",
        default_model: str = "deepseek/deepseek-chat-v3-0324:free",
        api_keys: list[str] | None = None,
    ) -> None:
        # The free tier is rate-limited per key. With one key a long run stops
        # partway through the day; with several it rotates and carries on.
        self.keys = KeyRing(api_keys or ([api_key] if api_key else []))
        super().__init__(api_key=self.keys.current(), base_url=base_url, default_model=default_model)

    def _headers(self) -> dict[str, str]:
        # Read the live key each time: a rotation between requests must take
        # effect without rebuilding the provider.
        active = self.keys.current() or self.api_key
        headers = {"Content-Type": "application/json"}
        if active:
            headers["Authorization"] = f"Bearer {active}"
        headers["HTTP-Referer"] = "https://github.com/aiqa-engineer"
        headers["X-Title"] = "AI QA Engineer"
        return headers

    @property
    def configured(self) -> bool:  # type: ignore[override]
        return bool(self.keys.current())

    async def _chat(self, req: LLMRequest) -> LLMResponse:
        """Send the request, adapting to what this model and key can do.

        Two things are handled here that a paid provider never needs:

        * **A model that rejects `response_format`.** Common among free models,
          and a hard 400 rather than a graceful degradation. The model is noted
          and the request is retried immediately without the parameter, so one
          wasted call teaches the process for the rest of the session.
        * **Key rotation.** Bounded to one pass through the ring; a
          provider-wide outage must not burn every key's retry budget.
        """
        # One pass through the ring, plus one extra iteration reserved for
        # dropping `response_format`. Without the reservation a single-key
        # install could never perform that retry at all.
        attempts = max(1, len(self.keys)) + 1
        structured_retried = False
        last: ProviderError | None = None

        for _attempt in range(attempts):
            if not self.keys.current():
                break
            try:
                response = await super()._chat(req)
            except ProviderError as exc:
                last = exc
                model = req.model or self.default_model

                if (
                    exc.status == 400
                    and req.json_mode
                    and not structured_retried
                    and model not in _NO_STRUCTURED_OUTPUT
                    and rejects_structured_output(str(exc))
                ):
                    structured_retried = True
                    _NO_STRUCTURED_OUTPUT.add(model)
                    log.info(
                        "%s rejects response_format; retrying without it and "
                        "relying on the prompt for JSON",
                        model,
                    )
                    continue                  # same key, one parameter lighter

                if exc.status == 429 and is_upstream_error(str(exc)):
                    # This model's host is busy. The key is fine, and the tier
                    # has other models: park this one and let the router fall
                    # through rather than retrying the same busy endpoint.
                    cool_model(model)
                    log.info("%s is rate limited upstream; trying the next model", model)
                    raise

                if not self.keys.record_failure(exc.status, str(exc)):
                    raise
                log.warning(
                    "openrouter key %s unusable (HTTP %s); rotating to %s",
                    fingerprint(self.api_key),
                    exc.status or "?",
                    fingerprint(self.keys.current()) or "(none available)",
                )
                self.api_key = self.keys.current()
                continue

            self.keys.record_success()
            return response

        if last is not None:
            raise last
        raise ProviderError(self.name, "no usable API key is available right now", retryable=True)

    def model_available(self, model: str) -> bool:  # type: ignore[override]
        return not model_cooling(model or self.default_model)

    def key_status(self) -> dict[str, Any]:
        return self.keys.snapshot()


class OllamaProvider(BaseProvider):
    """Local models via Ollama — zero cost, fully offline, the default."""

    name = "ollama"
    supports_embeddings = True
    requires_api_key = False

    def __init__(self, base_url: str = "http://localhost:11434", default_model: str = "qwen2.5-coder:7b") -> None:
        super().__init__(api_key="", base_url=base_url, default_model=default_model)

    async def _chat(self, req: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": req.model or self.default_model,
            "messages": [m.to_dict() for m in req.messages],
            "stream": False,
            "options": {"temperature": req.temperature, "num_predict": req.max_tokens},
        }
        if req.json_mode:
            payload["format"] = "json"
        if req.stop:
            payload["options"]["stop"] = req.stop

        async with _client(req.timeout_seconds) as client:
            try:
                resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            except httpx.HTTPError as exc:
                raise ProviderError(self.name, f"Ollama unreachable at {self.base_url}: {exc}", retryable=True) from exc
            _raise_for(self.name, resp)
            data = resp.json()

        usage = TokenUsage(
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            completion_tokens=int(data.get("eval_count", 0)),
            total_tokens=int(data.get("prompt_eval_count", 0)) + int(data.get("eval_count", 0)),
        )
        return LLMResponse(
            text=(data.get("message") or {}).get("content", ""),
            model=data.get("model", req.model),
            usage=usage,
            finish_reason=data.get("done_reason", "stop"),
        )

    async def embed(self, texts: Sequence[str], model: str = "") -> list[list[float]]:
        out: list[list[float]] = []
        async with _client(120) as client:
            for text in texts:
                try:
                    resp = await client.post(
                        f"{self.base_url}/api/embeddings",
                        json={"model": model or "nomic-embed-text", "prompt": text},
                    )
                except httpx.HTTPError as exc:
                    raise ProviderError(self.name, f"embedding error: {exc}") from exc
                _raise_for(self.name, resp)
                out.append(resp.json().get("embedding", []))
        return out

    async def health(self) -> bool:
        async with _client(5) as client:
            try:
                resp = await client.get(f"{self.base_url}/api/tags")
                return resp.status_code == 200
            except httpx.HTTPError:
                return False

    async def list_models(self) -> list[str]:
        async with _client(10) as client:
            try:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                return [m.get("name", "") for m in resp.json().get("models", [])]
            except httpx.HTTPError:
                return []


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.anthropic.com/v1",
        default_model: str = "claude-sonnet-5",
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, default_model=default_model)

    async def _chat(self, req: LLMRequest) -> LLMResponse:
        system_parts = [m.content for m in req.messages if str(m.to_dict()["role"]) == "system"]
        turns = [m.to_dict() for m in req.messages if m.to_dict()["role"] in ("user", "assistant")]
        if not turns:
            turns = [{"role": "user", "content": req.prompt_text}]

        payload: dict[str, Any] = {
            "model": req.model or self.default_model,
            "messages": turns,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        if system_parts:
            system_text = "\n\n".join(system_parts)
            # The system prompt carries the house standards and conventions and
            # is byte-identical between runs, so it is exactly what should be
            # billed once rather than on every call.
            payload["system"] = (
                _cache_breakpoint(system_text) if _wants_cache(req) else system_text
            )
        if req.stop:
            payload["stop_sequences"] = req.stop

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with _client(req.timeout_seconds) as client:
            try:
                resp = await client.post(f"{self.base_url}/messages", headers=headers, json=payload)
            except httpx.HTTPError as exc:
                raise ProviderError(self.name, f"transport error: {exc}", retryable=True) from exc
            _raise_for(self.name, resp)
            data = resp.json()

        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage_raw = data.get("usage") or {}
        pt = int(usage_raw.get("input_tokens", 0))
        ct = int(usage_raw.get("output_tokens", 0))
        return LLMResponse(
            text=text,
            model=data.get("model", req.model),
            usage=TokenUsage(
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=pt + ct,
                cached_tokens=int(usage_raw.get("cache_read_input_tokens", 0)),
            ),
            finish_reason=data.get("stop_reason", "stop"),
        )


class GeminiProvider(BaseProvider):
    name = "gemini"
    supports_embeddings = True

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        default_model: str = "gemini-2.0-flash",
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, default_model=default_model)

    async def _chat(self, req: LLMRequest) -> LLMResponse:
        model = req.model or self.default_model
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []
        for m in req.messages:
            d = m.to_dict()
            if d["role"] == "system":
                system_parts.append(d["content"])
                continue
            role = "model" if d["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": d["content"]}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": req.prompt_text}]}]

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": req.temperature,
                "maxOutputTokens": req.max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if req.json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        url = f"{self.base_url}/models/{model}:generateContent"
        async with _client(req.timeout_seconds) as client:
            try:
                resp = await client.post(url, params={"key": self.api_key}, json=payload)
            except httpx.HTTPError as exc:
                raise ProviderError(self.name, f"transport error: {exc}", retryable=True) from exc
            _raise_for(self.name, resp)
            data = resp.json()

        candidates = data.get("candidates") or []
        text = ""
        if candidates:
            text = "".join(p.get("text", "") for p in (candidates[0].get("content") or {}).get("parts", []))
        usage_raw = data.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            model=model,
            usage=TokenUsage(
                prompt_tokens=int(usage_raw.get("promptTokenCount", 0)),
                completion_tokens=int(usage_raw.get("candidatesTokenCount", 0)),
                total_tokens=int(usage_raw.get("totalTokenCount", 0)),
            ),
            finish_reason=(candidates[0].get("finishReason", "stop") if candidates else "stop"),
        )

    async def embed(self, texts: Sequence[str], model: str = "") -> list[list[float]]:
        model = model or "text-embedding-004"
        out: list[list[float]] = []
        async with _client(120) as client:
            for text in texts:
                resp = await client.post(
                    f"{self.base_url}/models/{model}:embedContent",
                    params={"key": self.api_key},
                    json={"content": {"parts": [{"text": text}]}},
                )
                _raise_for(self.name, resp)
                out.append((resp.json().get("embedding") or {}).get("values", []))
        return out


# --------------------------------------------------------------------------- #
# Deterministic local embeddings — always available, zero cost, no network
# --------------------------------------------------------------------------- #
class HashingEmbeddingProvider(BaseProvider):
    """A hashing-trick embedder.

    Not as semantically rich as a neural embedder, but it is deterministic,
    instant, free and dependency-free — which makes repository retrieval work
    on a laptop with no Ollama and no API key. The router prefers real
    embedders when they are available and silently falls back to this.
    """

    name = "hashing"
    supports_embeddings = True
    requires_api_key = False

    def __init__(self, dim: int = 512) -> None:
        super().__init__(default_model=f"local-hash-{dim}")
        self.dim = dim

    async def _chat(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise ProviderError(self.name, "hashing provider does not generate text", retryable=False)

    @staticmethod
    def _tokens(text: str) -> list[str]:
        buf: list[str] = []
        word: list[str] = []
        for ch in text.lower():
            if ch.isalnum() or ch == "_":
                word.append(ch)
            elif word:
                buf.append("".join(word))
                word = []
        if word:
            buf.append("".join(word))
        return buf

    def embed_sync(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = self._tokens(text)
        if not tokens:
            return vec
        # unigrams + bigrams give a little word-order sensitivity
        grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: Sequence[str], model: str = "") -> list[list[float]]:
        return [self.embed_sync(t) for t in texts]

    async def health(self) -> bool:
        return True
