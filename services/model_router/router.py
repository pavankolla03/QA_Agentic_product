"""Model router — the single place any LLM call is priced, budgeted and chosen.

Agents ask for a *tier* (or just name their task); the router decides which
concrete model serves it. That indirection is what makes cost a configuration
concern rather than a code concern, and it is where every cost optimization
lands:

* **Tiering** — paid reasoning only where judgement changes the outcome.
* **Task overrides** — a single agent can mix a free call and a paid one.
* **Deterministic complexity scoring** — trivial work is downgraded, genuinely
  hard work may escalate, and the decision itself costs nothing.
* **Request accounting** — free tiers are request-limited; we reroute before
  hitting the wall instead of failing mid-run.
* **Pre-flight budget checks** — a call that cannot fit is refused before it is
  paid for, not after.
* **Prompt caching** — the static prefix (standards, conventions, app map) is
  marked cacheable so repeat runs pay for it once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from configs.settings import Settings, get_settings, load_model_config
from packages.aiqa_types.budget import (
    BudgetExceeded,
    ProviderQuota,
    RunBudget,
    score_complexity,
)
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

#: Ordered from cheapest to most expensive; used when escalating or downgrading.
TIER_ORDER = ["cheap", "coding", "reasoning"]

# Re-exported for backwards compatibility with v1 imports.
__all__ = [
    "BudgetExceeded",
    "ModelCandidate",
    "ModelRouter",
    "RouterBudget",
    "RunBudget",
    "get_router",
    "reset_router",
]

#: v1 name. `RunBudget` is the real type; this alias keeps older call sites working.
RouterBudget = RunBudget


@dataclass
class ModelCandidate:
    provider: str
    model: str
    price_in: float = 0.0     # USD per 1M prompt tokens
    price_out: float = 0.0    # USD per 1M completion tokens
    dim: int = 0
    caching: bool = False
    tier: str = ""

    @property
    def free(self) -> bool:
        return self.price_in == 0.0 and self.price_out == 0.0

    def cost(self, usage: TokenUsage) -> float:
        billable_prompt = max(0, usage.prompt_tokens - usage.cached_tokens)
        # Cached prompt reads are billed at roughly a tenth of list price.
        cached = usage.cached_tokens * self.price_in * 0.1
        return round(
            (billable_prompt * self.price_in + usage.completion_tokens * self.price_out + cached)
            / 1_000_000,
            8,
        )

    def estimate(self, prompt_chars: int, expected_output_tokens: int = 800) -> float:
        prompt_tokens = max(1, prompt_chars // 4)
        return self.cost(
            TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=expected_output_tokens)
        )


@dataclass
class RouterStats:
    calls: int = 0
    failures: int = 0
    fallbacks: int = 0
    escalations: int = 0
    downgrades: int = 0
    refused: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    cached_tokens: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    by_tier: dict[str, int] = field(default_factory=dict)


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
        self.offline = bool(offline)

        self._providers: dict[str, BaseProvider] = {}
        self._health: dict[str, tuple[bool, float]] = {}
        self._health_ttl = 60.0

        defaults = self.config.get("defaults", {}) or {}
        self.default_temperature = float(defaults.get("temperature", 0.1))
        self.default_max_tokens = int(defaults.get("max_tokens", 4096))
        self.default_timeout = int(defaults.get("timeout_seconds", 180))
        self.retries = int(defaults.get("retries", 2))
        self.prompt_caching = bool(defaults.get("prompt_caching", True))
        self.cache_min_chars = int(defaults.get("cache_min_chars", 2000))

        self._aliases: dict[str, str] = dict(self.config.get("aliases", {}) or {})
        self._task_capability: dict[str, str] = dict(self.config.get("task_capability", {}) or {})
        self._agent_capability: dict[str, str] = dict(self.config.get("agent_capability", {}) or {})

        escalation = self.config.get("escalation", {}) or {}
        self.escalation_enabled = bool(escalation.get("enabled", True))
        self.escalation_threshold = float(escalation.get("cheap_to_reasoning_threshold", 0.75))
        self.escalate_on_retry = bool(escalation.get("escalate_on_retry", True))
        self.max_escalations = int(escalation.get("max_escalations_per_run", 3))

        self.quotas: dict[str, ProviderQuota] = {}
        for name, limits in (self.config.get("provider_limits", {}) or {}).items():
            if isinstance(limits, dict) and limits.get("free_requests_daily"):
                self.quotas[name] = ProviderQuota(
                    provider=name,
                    daily_limit=int(limits["free_requests_daily"]),
                    reserve_threshold=int(limits.get("reserve_threshold", 0)),
                )

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
        except Exception:  # noqa: BLE001 - a health probe must never raise
            ok = False
        self._health[name] = (bool(ok), now)
        return bool(ok)

    def invalidate_health(self) -> None:
        self._health.clear()

    # ------------------------------------------------------------------ #
    # Tier resolution
    # ------------------------------------------------------------------ #
    def _normalise_tier(self, tier: Capability | str) -> str:
        name = tier.value if isinstance(tier, Capability) else str(tier)
        return self._aliases.get(name, name)

    def tier_for(
        self,
        *,
        task: str = "",
        agent: AgentName | str | None = None,
        explicit: Capability | str | None = None,
    ) -> str:
        """Most specific wins: explicit → task → agent → cheap."""
        if explicit is not None:
            return self._normalise_tier(explicit)
        if task and task in self._task_capability:
            return self._normalise_tier(self._task_capability[task])
        if agent is not None:
            key = agent.value if isinstance(agent, AgentName) else str(agent)
            if key in self._agent_capability:
                return self._normalise_tier(self._agent_capability[key])
        return "cheap"

    def adjust_tier(
        self, tier: str, complexity: float, retry: int, budget: RunBudget | None
    ) -> tuple[str, str]:
        """Apply escalation/downgrade. Returns ``(tier, reason)``."""
        if not self.escalation_enabled or tier == "embedding":
            return tier, ""

        # Escalate cheap work that is genuinely hard.
        if tier == "cheap" and complexity >= self.escalation_threshold:
            if budget is None or budget.escalations < self.max_escalations:
                if budget is not None:
                    budget.escalations += 1
                self.stats.escalations += 1
                return "reasoning", f"complexity {complexity:.2f} >= {self.escalation_threshold}"

        # Escalate one tier after a failed cheap attempt.
        if retry > 0 and self.escalate_on_retry:
            index = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
            if index < len(TIER_ORDER) - 1:
                if budget is None or budget.escalations < self.max_escalations:
                    if budget is not None:
                        budget.escalations += 1
                    self.stats.escalations += 1
                    return TIER_ORDER[index + 1], f"retry {retry}"

        # Downgrade trivial reasoning work — the common case, and the big saving.
        if tier == "reasoning" and complexity < 0.15:
            self.stats.downgrades += 1
            return "cheap", f"complexity {complexity:.2f} below reasoning threshold"

        return tier, ""

    def candidates(self, tier: Capability | str) -> list[ModelCandidate]:
        name = self._normalise_tier(tier)
        raw = (self.config.get("routes", {}) or {}).get(name, []) or []
        out = [
            ModelCandidate(
                provider=str(item.get("provider", "")),
                model=str(item.get("model", "")),
                price_in=float(item.get("in", 0) or 0),
                price_out=float(item.get("out", 0) or 0),
                dim=int(item.get("dim", 0) or 0),
                caching=bool(item.get("caching", False)),
                tier=name,
            )
            for item in raw
            if item.get("provider")
        ]
        # Terminal fallbacks so the platform always has somewhere to go.
        if name != "embedding" and not any(c.provider == "mock" for c in out):
            out.append(ModelCandidate(provider="mock", model=f"mock-{name}", tier=name))
        if name == "embedding" and not any(c.provider == "hashing" for c in out):
            out.append(ModelCandidate(provider="hashing", model="local-hash-512", dim=512, tier=name))
        return out

    def capability_for_agent(self, agent: AgentName | str) -> Capability:
        """v1 compatibility shim."""
        tier = self.tier_for(agent=agent)
        try:
            return Capability(tier)
        except ValueError:
            return Capability.FAST

    async def resolve(self, tier: Capability | str) -> ModelCandidate | None:
        for candidate in self.candidates(tier):
            if await self._usable(candidate):
                return candidate
        return None

    async def _usable(self, candidate: ModelCandidate) -> bool:
        provider = self.provider(candidate.provider)
        if provider is None or not provider.configured:
            return False
        quota = self.quotas.get(candidate.provider)
        # Free models on a request-limited provider stop being an option once we
        # are into the reserve; paid models on the same provider still work.
        if quota is not None and candidate.free and not quota.available():
            return False
        return await self._is_healthy(candidate.provider)

    # ------------------------------------------------------------------ #
    # The call
    # ------------------------------------------------------------------ #
    async def complete(
        self,
        messages: Sequence[Any],
        *,
        capability: Capability | str | None = None,
        tier: Capability | str | None = None,
        task: str = "",
        agent: AgentName | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        budget: RunBudget | None = None,
        run_id: str = "",
        trace_id: str = "",
        model_override: str | None = None,
        complexity: float | None = None,
        retry: int = 0,
        cacheable_prefix_chars: int = 0,
    ) -> LLMResponse:
        """Run one completion: choose a tier, respect the budget, fall back on failure."""
        if budget is not None:
            budget.check()

        chosen_tier = self.tier_for(task=task, agent=agent, explicit=tier or capability)

        request = LLMRequest(
            messages=list(messages),
            temperature=self.default_temperature if temperature is None else temperature,
            max_tokens=self.default_max_tokens if max_tokens is None else max_tokens,
            json_mode=json_mode,
            timeout_seconds=self.default_timeout,
            task=task,
        )
        prompt_text = request.prompt_text

        if complexity is None:
            complexity = score_complexity(prompt_text, retry=retry)
        chosen_tier, adjust_reason = self.adjust_tier(chosen_tier, complexity, retry, budget)
        if adjust_reason:
            log.info("tier for %s adjusted to %s (%s)", task or agent, chosen_tier, adjust_reason)

        # Mark the static prefix as cacheable so repeat runs pay for it once.
        if self.prompt_caching and cacheable_prefix_chars >= self.cache_min_chars:
            request.metadata["cache_prefix_chars"] = cacheable_prefix_chars

        chain = self.candidates(chosen_tier)
        last_error: Exception | None = None
        first_choice = chain[0].provider if chain else ""

        for index, candidate in enumerate(chain):
            if not await self._usable(candidate):
                continue

            # Pre-flight: refuse a call we already know will not fit.
            if budget is not None:
                estimated = candidate.estimate(len(prompt_text), request.max_tokens // 4)
                breach = budget.would_exceed(max(1, len(prompt_text) // 4), estimated)
                if breach:
                    if candidate.free:
                        # A free model cannot breach the cost ceiling; only
                        # request/token ceilings apply to it.
                        if breach == "cost":
                            pass
                        else:
                            self.stats.refused += 1
                            raise BudgetExceeded(breach, f"run budget would be exceeded ({breach})")
                    else:
                        log.info("skipping paid %s: would exceed %s", candidate.model, breach)
                        continue

            request.model = model_override or candidate.model
            request.metadata["supports_caching"] = candidate.caching
            provider = self.provider(candidate.provider)
            assert provider is not None
            attempt_error: Exception | None = None

            for attempt in range(self.retries + 1):
                started = time.perf_counter()
                try:
                    response = await provider.chat(request)
                except ProviderError as exc:
                    attempt_error = exc
                    self.stats.failures += 1
                    self._record_quota(candidate)
                    self._emit_trace(
                        LLMCallTrace(
                            run_id=run_id, trace_id=trace_id, agent=agent,
                            provider=candidate.provider, model=request.model,
                            capability=_as_capability(chosen_tier),
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            status="failed", error=str(exc)[:500],
                        )
                    )
                    if not exc.retryable or attempt >= self.retries:
                        break
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                except Exception as exc:  # noqa: BLE001 - never let a provider crash a run
                    attempt_error = exc
                    self.stats.failures += 1
                    break
                else:
                    cost = candidate.cost(response.usage)
                    response.cost_usd = cost
                    self._record_quota(candidate)

                    self.stats.calls += 1
                    self.stats.total_cost_usd = round(self.stats.total_cost_usd + cost, 8)
                    self.stats.total_tokens += response.usage.total_tokens
                    self.stats.cached_tokens += response.usage.cached_tokens
                    self.stats.by_provider[candidate.provider] = (
                        self.stats.by_provider.get(candidate.provider, 0) + 1
                    )
                    self.stats.by_tier[chosen_tier] = self.stats.by_tier.get(chosen_tier, 0) + 1
                    if index > 0:
                        self.stats.fallbacks += 1

                    if budget is not None:
                        budget.record(
                            cost=cost,
                            input_tokens=response.usage.prompt_tokens,
                            output_tokens=response.usage.completion_tokens,
                            cached_tokens=response.usage.cached_tokens,
                            free=candidate.free,
                        )

                    self._emit_trace(
                        LLMCallTrace(
                            run_id=run_id, trace_id=trace_id, agent=agent,
                            provider=candidate.provider, model=response.model,
                            capability=_as_capability(chosen_tier),
                            usage=response.usage, cost_usd=cost, latency_ms=response.latency_ms,
                            status="succeeded",
                            prompt_chars=len(prompt_text), completion_chars=len(response.text),
                            prompt_preview=redact(prompt_text)[:400],
                            fallback_from=first_choice if index > 0 else None,
                        )
                    )
                    return response

            last_error = attempt_error or last_error
            log.warning(
                "provider %s failed for %s, falling back: %s", candidate.provider, task or chosen_tier, last_error
            )

        raise ProviderError(
            "router",
            f"No provider could serve tier '{chosen_tier}'. Last error: {last_error}",
            retryable=False,
        )

    def _record_quota(self, candidate: ModelCandidate) -> None:
        quota = self.quotas.get(candidate.provider)
        if quota is not None and candidate.free:
            quota.record()

    # ------------------------------------------------------------------ #
    async def embed(
        self, texts: Sequence[str], budget: RunBudget | None = None, run_id: str = ""
    ) -> list[list[float]]:
        """Embed texts with the best available embedder. Never fails: hashing is terminal."""
        for candidate in self.candidates("embedding"):
            provider = self.provider(candidate.provider)
            if provider is None or not provider.supports_embeddings or not provider.configured:
                continue
            if candidate.provider != "hashing" and not await self._is_healthy(candidate.provider):
                continue
            try:
                started = time.perf_counter()
                vectors = await provider.embed(texts, model=candidate.model)
                if not vectors or not vectors[0]:
                    continue
                approx = sum(max(1, len(t) // 4) for t in texts)
                cost = candidate.cost(TokenUsage(prompt_tokens=approx, total_tokens=approx))
                if budget is not None:
                    budget.record(cost=cost, input_tokens=approx, free=candidate.free)
                self._emit_trace(
                    LLMCallTrace(
                        run_id=run_id, provider=candidate.provider, model=candidate.model,
                        capability=Capability.EMBEDDING,
                        usage=TokenUsage(prompt_tokens=approx, total_tokens=approx),
                        cost_usd=cost, latency_ms=int((time.perf_counter() - started) * 1000),
                        status="succeeded",
                    )
                )
                return vectors
            except Exception as exc:  # noqa: BLE001
                log.warning("embedding provider %s failed: %s", candidate.provider, exc)
                continue
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
        for name in ("openrouter", "anthropic", "ollama", "openai", "gemini", "hashing", "mock"):
            prov = self.provider(name)
            providers[name] = {
                "configured": bool(prov and prov.configured),
                "healthy": await self._is_healthy(name) if prov else False,
                "default_model": prov.default_model if prov else "",
                "quota": self.quotas[name].snapshot() if name in self.quotas else None,
            }
        routes: dict[str, Any] = {}
        for tier in ("cheap", "coding", "reasoning", "embedding"):
            chosen = await self.resolve(tier)
            routes[tier] = (
                {
                    "provider": chosen.provider,
                    "model": chosen.model,
                    "free": chosen.free,
                    "price_in": chosen.price_in,
                    "price_out": chosen.price_out,
                }
                if chosen
                else None
            )
        # v1 compatibility for existing clients.
        routes["fast"] = routes["cheap"]
        return {
            "providers": providers,
            "active_routes": routes,
            "tiers": {
                tier: [f"{c.provider}/{c.model}" for c in self.candidates(tier)]
                for tier in ("cheap", "coding", "reasoning", "embedding")
            },
            "stats": {
                "calls": self.stats.calls,
                "failures": self.stats.failures,
                "fallbacks": self.stats.fallbacks,
                "escalations": self.stats.escalations,
                "downgrades": self.stats.downgrades,
                "refused": self.stats.refused,
                "total_cost_usd": round(self.stats.total_cost_usd, 6),
                "total_tokens": self.stats.total_tokens,
                "cached_tokens": self.stats.cached_tokens,
                "by_provider": dict(self.stats.by_provider),
                "by_tier": dict(self.stats.by_tier),
            },
        }

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()


def _as_capability(tier: str) -> Capability:
    try:
        return Capability(tier)
    except ValueError:
        return Capability.FAST


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def reset_router() -> None:
    global _router
    _router = None
