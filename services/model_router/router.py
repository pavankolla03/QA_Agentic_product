"""Model router: capability → concrete model, with health-aware fallback.

Agents ask for a *capability* ("coding", "reasoning", "fast", "embedding").
The router resolves it against ``configs/models.yaml``, walks the candidate
list until one succeeds, prices the call, emits a trace, and enforces the
budget. Swapping models is therefore a config change, never a code change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from configs.settings import Settings, get_settings, load_model_config
from packages.aiqa_types.enums import AgentName, Capability
from packages.aiqa_types.models import LLMCallTrace, TokenUsage
from packages.llm_provider.base import BaseProvider, LLMRequest, LLMResponse, ProviderError
from packages.llm_provider.mock import MockProvider
from packages.llm_provider.providers import (
    AnthropicProvider,
    GeminiProvider,
    HashingEmbeddingProvider,
    OllamaProvider,
    OpenAIProvider,
    OpenRouterProvider,
)
from packages.security.redaction import redact

log = logging.getLogger("aiqa.router")


class BudgetExceeded(RuntimeError):
    """Raised when a call would push a run past its cost or token ceiling."""


@dataclass
class ModelCandidate:
    provider: str
    model: str
    price_in: float = 0.0     # USD per 1M prompt tokens
    price_out: float = 0.0    # USD per 1M completion tokens
    dim: int = 0

    @property
    def free(self) -> bool:
        return self.price_in == 0.0 and self.price_out == 0.0

    def cost(self, usage: TokenUsage) -> float:
        billable_prompt = max(0, usage.prompt_tokens - usage.cached_tokens)
        cached = usage.cached_tokens * self.price_in * 0.1  # cached reads ~10% of list price
        return round(
            (billable_prompt * self.price_in + usage.completion_tokens * self.price_out) / 1_000_000
            + cached / 1_000_000,
            8,
        )


@dataclass
class RouterBudget:
    """Per-run spend guard. Checked before and after every call."""

    max_cost_usd: float = 2.0
    max_tokens: int = 400_000
    spent_usd: float = 0.0
    used_tokens: int = 0
    calls: int = 0

    def check(self) -> None:
        if self.max_cost_usd and self.spent_usd >= self.max_cost_usd:
            raise BudgetExceeded(
                f"Run cost limit reached: ${self.spent_usd:.4f} of ${self.max_cost_usd:.2f}."
            )
        if self.max_tokens and self.used_tokens >= self.max_tokens:
            raise BudgetExceeded(
                f"Run token limit reached: {self.used_tokens:,} of {self.max_tokens:,}."
            )

    def record(self, cost: float, tokens: int) -> None:
        self.spent_usd = round(self.spent_usd + cost, 8)
        self.used_tokens += tokens
        self.calls += 1

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_cost_usd - self.spent_usd)


@dataclass
class RouterStats:
    calls: int = 0
    failures: int = 0
    fallbacks: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)


TraceSink = Callable[[LLMCallTrace], None]


class ModelRouter:
    """Central access point for every LLM call in the platform."""

    def __init__(
        self,
        settings: Settings | None = None,
        config: dict[str, Any] | None = None,
        trace_sink: TraceSink | None = None,
        offline: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.config = config or load_model_config()
        self.trace_sink = trace_sink
        self.stats = RouterStats()
        self._providers: dict[str, BaseProvider] = {}
        self._health: dict[str, tuple[bool, float]] = {}
        self._health_ttl = 60.0
        self.offline = offline if offline is not None else False
        defaults = self.config.get("defaults", {}) or {}
        self.default_temperature = float(defaults.get("temperature", 0.1))
        self.default_max_tokens = int(defaults.get("max_tokens", 4096))
        self.default_timeout = int(defaults.get("timeout_seconds", 180))
        self.retries = int(defaults.get("retries", 2))

    # ------------------------------------------------------------------ #
    # Provider registry
    # ------------------------------------------------------------------ #
    def provider(self, name: str) -> BaseProvider | None:
        if name in self._providers:
            return self._providers[name]
        s = self.settings
        built: BaseProvider | None
        if name == "ollama":
            built = OllamaProvider(base_url=s.ollama_base_url, default_model=s.ollama_model)
        elif name == "openrouter":
            built = OpenRouterProvider(api_key=s.openrouter_api_key, default_model=s.openrouter_model)
        elif name == "openai":
            built = OpenAIProvider(api_key=s.openai_api_key, default_model=s.openai_model)
        elif name == "anthropic":
            built = AnthropicProvider(api_key=s.anthropic_api_key, default_model=s.anthropic_model)
        elif name == "gemini":
            built = GeminiProvider(api_key=s.gemini_api_key, default_model=s.gemini_model)
        elif name == "hashing":
            built = HashingEmbeddingProvider()
        elif name == "mock":
            built = MockProvider()
        else:
            built = None
        if built is not None:
            self._providers[name] = built
        return built

    async def _is_healthy(self, name: str) -> bool:
        if self.offline and name not in ("mock", "hashing"):
            return False
        cached = self._health.get(name)
        now = time.monotonic()
        if cached and (now - cached[1]) < self._health_ttl:
            return cached[0]
        prov = self.provider(name)
        if prov is None or not prov.configured:
            self._health[name] = (False, now)
            return False
        try:
            ok = await asyncio.wait_for(prov.health(), timeout=8)
        except (TimeoutError, asyncio.TimeoutError, Exception):  # noqa: BLE001 - health must never raise
            ok = False
        self._health[name] = (bool(ok), now)
        return bool(ok)

    def invalidate_health(self) -> None:
        self._health.clear()

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def candidates(self, capability: Capability | str) -> list[ModelCandidate]:
        cap = capability.value if isinstance(capability, Capability) else str(capability)
        raw = (self.config.get("routes", {}) or {}).get(cap, []) or []
        out = [
            ModelCandidate(
                provider=str(item.get("provider", "")),
                model=str(item.get("model", "")),
                price_in=float(item.get("in", 0) or 0),
                price_out=float(item.get("out", 0) or 0),
                dim=int(item.get("dim", 0) or 0),
            )
            for item in raw
            if item.get("provider")
        ]
        # The offline provider is always the last resort for text capabilities.
        if cap != "embedding" and not any(c.provider == "mock" for c in out):
            out.append(ModelCandidate(provider="mock", model=f"mock-{cap}"))
        if cap == "embedding" and not any(c.provider == "hashing" for c in out):
            out.append(ModelCandidate(provider="hashing", model="local-hash-512", dim=512))
        return out

    def capability_for_agent(self, agent: AgentName | str) -> Capability:
        name = agent.value if isinstance(agent, AgentName) else str(agent)
        mapping = self.config.get("agent_capability", {}) or {}
        try:
            return Capability(mapping.get(name, "fast"))
        except ValueError:
            return Capability.FAST

    async def resolve(self, capability: Capability | str) -> ModelCandidate | None:
        """First healthy candidate for a capability."""
        for cand in self.candidates(capability):
            if await self._is_healthy(cand.provider):
                return cand
        return None

    # ------------------------------------------------------------------ #
    # The call
    # ------------------------------------------------------------------ #
    async def complete(
        self,
        messages: Sequence[Any],
        *,
        capability: Capability | str = Capability.FAST,
        task: str = "",
        agent: AgentName | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        budget: RouterBudget | None = None,
        run_id: str = "",
        trace_id: str = "",
        model_override: str | None = None,
    ) -> LLMResponse:
        """Run one completion, falling back through the candidate chain."""
        if budget is not None:
            budget.check()

        req = LLMRequest(
            messages=list(messages),
            temperature=self.default_temperature if temperature is None else temperature,
            max_tokens=self.default_max_tokens if max_tokens is None else max_tokens,
            json_mode=json_mode,
            timeout_seconds=self.default_timeout,
            task=task,
        )

        chain = self.candidates(capability)
        last_error: Exception | None = None
        first_choice = chain[0].provider if chain else ""

        for index, cand in enumerate(chain):
            prov = self.provider(cand.provider)
            if prov is None or not prov.configured:
                continue
            if not await self._is_healthy(cand.provider):
                continue

            req.model = model_override or cand.model
            attempt_error: Exception | None = None

            for attempt in range(self.retries + 1):
                started = time.perf_counter()
                try:
                    resp = await prov.chat(req)
                except ProviderError as exc:
                    attempt_error = exc
                    self.stats.failures += 1
                    self._emit_trace(
                        LLMCallTrace(
                            run_id=run_id, trace_id=trace_id, agent=agent,
                            provider=cand.provider, model=req.model,
                            capability=Capability(capability) if isinstance(capability, str) else capability,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            status="failed", error=str(exc)[:500],
                        )
                    )
                    if not exc.retryable or attempt >= self.retries:
                        break
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                except Exception as exc:  # noqa: BLE001 - never let a provider crash a run
                    attempt_error = exc
                    self.stats.failures += 1
                    break
                else:
                    cost = cand.cost(resp.usage)
                    resp.cost_usd = cost
                    self.stats.calls += 1
                    self.stats.total_cost_usd = round(self.stats.total_cost_usd + cost, 8)
                    self.stats.total_tokens += resp.usage.total_tokens
                    self.stats.by_provider[cand.provider] = self.stats.by_provider.get(cand.provider, 0) + 1
                    if index > 0:
                        self.stats.fallbacks += 1
                    if budget is not None:
                        budget.record(cost, resp.usage.total_tokens)

                    self._emit_trace(
                        LLMCallTrace(
                            run_id=run_id, trace_id=trace_id, agent=agent,
                            provider=cand.provider, model=resp.model,
                            capability=Capability(capability) if isinstance(capability, str) else capability,
                            usage=resp.usage, cost_usd=cost, latency_ms=resp.latency_ms,
                            status="succeeded",
                            prompt_chars=len(req.prompt_text), completion_chars=len(resp.text),
                            prompt_preview=redact(req.prompt_text)[:400],
                            fallback_from=first_choice if index > 0 else None,
                        )
                    )
                    return resp

            last_error = attempt_error or last_error
            log.warning("provider %s failed for %s, falling back: %s", cand.provider, task or capability, last_error)

        raise ProviderError(
            "router",
            f"No provider could serve capability '{capability}'. Last error: {last_error}",
            retryable=False,
        )

    # ------------------------------------------------------------------ #
    async def embed(self, texts: Sequence[str], budget: RouterBudget | None = None, run_id: str = "") -> list[list[float]]:
        """Embed texts with the best available embedder (never fails: hashing is terminal)."""
        for cand in self.candidates(Capability.EMBEDDING):
            prov = self.provider(cand.provider)
            if prov is None or not prov.supports_embeddings or not prov.configured:
                continue
            if cand.provider != "hashing" and not await self._is_healthy(cand.provider):
                continue
            try:
                started = time.perf_counter()
                vectors = await prov.embed(texts, model=cand.model)
                if not vectors or not vectors[0]:
                    continue
                approx = sum(max(1, len(t) // 4) for t in texts)
                cost = cand.cost(TokenUsage(prompt_tokens=approx, total_tokens=approx))
                if budget is not None:
                    budget.record(cost, approx)
                self._emit_trace(
                    LLMCallTrace(
                        run_id=run_id, provider=cand.provider, model=cand.model,
                        capability=Capability.EMBEDDING,
                        usage=TokenUsage(prompt_tokens=approx, total_tokens=approx),
                        cost_usd=cost, latency_ms=int((time.perf_counter() - started) * 1000),
                        status="succeeded",
                    )
                )
                return vectors
            except Exception as exc:  # noqa: BLE001
                log.warning("embedding provider %s failed: %s", cand.provider, exc)
                continue
        # Terminal fallback — always works.
        return await HashingEmbeddingProvider().embed(texts)

    # ------------------------------------------------------------------ #
    def _emit_trace(self, trace: LLMCallTrace) -> None:
        if self.trace_sink:
            try:
                self.trace_sink(trace)
            except Exception:  # noqa: BLE001 - tracing must never break a run
                log.exception("trace sink failed")

    async def status(self) -> dict[str, Any]:
        """Health + routing snapshot for the control plane UI."""
        providers: dict[str, Any] = {}
        for name in ("ollama", "openrouter", "openai", "anthropic", "gemini", "hashing", "mock"):
            prov = self.provider(name)
            providers[name] = {
                "configured": bool(prov and prov.configured),
                "healthy": await self._is_healthy(name) if prov else False,
                "default_model": prov.default_model if prov else "",
            }
        routes: dict[str, Any] = {}
        for cap in ("fast", "reasoning", "coding", "embedding"):
            chosen = await self.resolve(cap)
            routes[cap] = (
                {"provider": chosen.provider, "model": chosen.model, "free": chosen.free} if chosen else None
            )
        return {
            "providers": providers,
            "active_routes": routes,
            "stats": {
                "calls": self.stats.calls,
                "failures": self.stats.failures,
                "fallbacks": self.stats.fallbacks,
                "total_cost_usd": round(self.stats.total_cost_usd, 6),
                "total_tokens": self.stats.total_tokens,
                "by_provider": dict(self.stats.by_provider),
            },
        }

    async def close(self) -> None:
        for prov in self._providers.values():
            await prov.close()


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def reset_router() -> None:
    global _router
    _router = None
