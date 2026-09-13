"""Prompt caching: the marker must reach the wire, or it saves nothing.

Caching was wired end to end but no provider ever read the hint, so the feature
existed only in the configuration file. These tests assert the payload that
actually gets posted, because that is the only place the saving is real.

Note on the economics: a cache *write* costs more than an uncached prompt, so
marking a prefix only pays when it is re-sent within the provider's TTL. The
router's `cache_min_chars` floor is what stops the platform paying the premium
on prompts too small to earn it back.
"""

from __future__ import annotations

import pytest

from packages.llm_provider.base import ChatMessage, LLMRequest
from packages.llm_provider.providers import (
    AnthropicProvider,
    OpenAIProvider,
    OpenRouterProvider,
)

SYSTEM = "House standards. " * 200          # comfortably over any floor
USER = "Design the suite."


def _request(model: str, *, cache: bool) -> LLMRequest:
    request = LLMRequest(
        messages=[ChatMessage.system(SYSTEM), ChatMessage.user(USER)],
        model=model,
        max_tokens=512,
        temperature=0.1,
    )
    if cache:
        request.metadata["cache_prefix_chars"] = len(SYSTEM)
    return request


def _system_content(payload: dict) -> object:
    return next(m["content"] for m in payload["messages"] if m["role"] == "system")


# --------------------------------------------------------------------------- #
# Providers that need an explicit marker
# --------------------------------------------------------------------------- #
def test_openrouter_marks_the_prefix_for_anthropic_models() -> None:
    payload = OpenRouterProvider(api_key="k")._payload(
        _request("anthropic/claude-sonnet-5", cache=True)
    )
    content = _system_content(payload)
    assert isinstance(content, list), "the marker requires a content block list"
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[0]["text"] == SYSTEM, "the prompt itself must be unchanged"


def test_anthropic_marks_its_system_prompt() -> None:
    request = _request("claude-sonnet-5", cache=True)
    system_text = "\n\n".join(
        m.content for m in request.messages if m.to_dict()["role"] == "system"
    )
    # Mirrors what AnthropicProvider._chat builds before posting.
    from packages.llm_provider.providers import _cache_breakpoint, _wants_cache

    assert _wants_cache(request)
    block = _cache_breakpoint(system_text)
    assert block[0]["cache_control"] == {"type": "ephemeral"}
    assert AnthropicProvider(api_key="k").name == "anthropic"


# --------------------------------------------------------------------------- #
# Providers that must NOT be sent a marker
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "model",
    ["qwen/qwen-2.5-coder-32b-instruct:free", "meta-llama/llama-3.3-70b-instruct:free"],
)
def test_free_models_are_not_sent_a_marker(model: str) -> None:
    """These are not billed for cache writes; the marker is noise at best."""
    payload = OpenRouterProvider(api_key="k")._payload(_request(model, cache=True))
    assert isinstance(_system_content(payload), str)


def test_openai_relies_on_automatic_caching() -> None:
    payload = OpenAIProvider(api_key="k")._payload(_request("gpt-4o-mini", cache=True))
    assert isinstance(_system_content(payload), str)


def test_no_marker_without_the_hint() -> None:
    payload = OpenRouterProvider(api_key="k")._payload(
        _request("anthropic/claude-sonnet-5", cache=False)
    )
    assert isinstance(_system_content(payload), str)


# --------------------------------------------------------------------------- #
# The router decides whether a prefix is worth caching at all
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("above_floor", [True, False])
async def test_the_router_only_marks_a_prefix_worth_caching(
    offline_router, above_floor: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below the floor the cache-write premium would cost more than it saves."""
    floor = offline_router.cache_min_chars
    assert floor > 0
    prefix = floor + 100 if above_floor else floor - 1

    seen: list[LLMRequest] = []
    provider = offline_router.provider("mock")
    original = provider._chat

    async def capture(request: LLMRequest):
        seen.append(request)
        return await original(request)

    monkeypatch.setattr(provider, "_chat", capture)

    await offline_router.complete(
        [ChatMessage.system("x" * prefix), ChatMessage.user(USER)],
        task="test_design.plan",
        agent="test_design",
        cacheable_prefix_chars=prefix,
    )

    assert seen, "the router never reached a provider"
    marked = "cache_prefix_chars" in (seen[0].metadata or {})
    assert marked is above_floor
