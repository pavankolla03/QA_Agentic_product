"""Model routing, cost accounting, and repository understanding."""

from __future__ import annotations

import pytest

from packages.aiqa_types.enums import Capability
from packages.aiqa_types.models import TokenUsage
from packages.llm_provider import ChatMessage, extract_json
from services.model_router.router import BudgetExceeded, ModelCandidate, ModelRouter, RouterBudget


# =========================================================================== #
# Cost arithmetic — wrong here means wrong invoices
# =========================================================================== #
def test_cost_is_priced_per_million_tokens() -> None:
    candidate = ModelCandidate("anthropic", "claude-sonnet-5", price_in=3.0, price_out=15.0)
    cost = candidate.cost(TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000))
    assert cost == pytest.approx(18.0)


def test_free_models_cost_nothing() -> None:
    candidate = ModelCandidate("ollama", "qwen2.5-coder:7b")
    assert candidate.free
    assert candidate.cost(TokenUsage(prompt_tokens=500_000, completion_tokens=500_000)) == 0.0


def test_cached_prompt_tokens_are_discounted() -> None:
    candidate = ModelCandidate("anthropic", "claude-sonnet-5", price_in=3.0, price_out=15.0)
    uncached = candidate.cost(TokenUsage(prompt_tokens=100_000, completion_tokens=0))
    cached = candidate.cost(TokenUsage(prompt_tokens=100_000, completion_tokens=0, cached_tokens=100_000))
    assert cached < uncached


# =========================================================================== #
# Budget enforcement
# =========================================================================== #
def test_budget_blocks_once_the_ceiling_is_reached() -> None:
    budget = RouterBudget(max_cost_usd=1.0, max_tokens=1000)
    budget.check()
    budget.record(0.99, 500)
    budget.check()
    budget.record(0.02, 100)
    with pytest.raises(BudgetExceeded, match="cost limit"):
        budget.check()


def test_budget_blocks_on_tokens_too() -> None:
    budget = RouterBudget(max_cost_usd=100.0, max_tokens=1000)
    budget.record(0.0, 1200)
    with pytest.raises(BudgetExceeded, match="token limit"):
        budget.check()


async def test_router_charges_the_budget(offline_router: ModelRouter) -> None:
    budget = RouterBudget(max_cost_usd=1.0, max_tokens=100_000)
    await offline_router.complete(
        [ChatMessage.user("Automate resident registration")],
        capability=Capability.REASONING, task="requirement.analyze", json_mode=True, budget=budget,
    )
    assert budget.calls == 1
    assert budget.used_tokens > 0


# =========================================================================== #
# Routing and fallback
# =========================================================================== #
async def test_offline_falls_back_to_the_deterministic_provider(offline_router: ModelRouter) -> None:
    response = await offline_router.complete(
        [ChatMessage.user("Automate the Resident Registration functionality")],
        capability=Capability.CODING, task="requirement.analyze", json_mode=True,
    )
    assert response.provider == "mock"
    assert response.cost_usd == 0.0


async def test_every_capability_resolves_even_with_no_credentials(offline_router: ModelRouter) -> None:
    for capability in ("fast", "reasoning", "coding", "embedding"):
        assert await offline_router.resolve(capability) is not None


async def test_embeddings_always_work(offline_router: ModelRouter) -> None:
    vectors = await offline_router.embed(["login page", "resident registration form"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 512
    assert vectors[0] != vectors[1]


async def test_traces_are_emitted_for_every_call(offline_router: ModelRouter) -> None:
    traces: list = []
    offline_router.trace_sink = traces.append
    await offline_router.complete([ChatMessage.user("hi")], capability=Capability.FAST, task="x")
    assert len(traces) == 1
    assert traces[0].provider == "mock"
    assert traces[0].status == "succeeded"


def test_agent_capability_mapping(offline_router: ModelRouter) -> None:
    assert offline_router.capability_for_agent("code_generation") == Capability.CODING
    assert offline_router.capability_for_agent("failure_analysis") == Capability.REASONING
    assert offline_router.capability_for_agent("unknown-agent") == Capability.FAST


# =========================================================================== #
# JSON extraction — models rarely return clean JSON
# =========================================================================== #
@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Here you go:\n```\n{"a": 1}\n```\nHope that helps!',
        'Sure! {"a": 1}',
        '{"a": 1,}',                                  # trailing comma
        'Preamble\n{"a": 1}\nPostamble',
    ],
)
def test_json_is_recovered_from_messy_output(text: str) -> None:
    assert extract_json(text) == {"a": 1}


def test_json_extraction_returns_the_fallback_when_hopeless() -> None:
    assert extract_json("no json here at all", default={"ok": False}) == {"ok": False}


# =========================================================================== #
# Repository understanding
# =========================================================================== #
def test_framework_and_layout_are_detected(repo_copy) -> None:
    from services.knowledge_service import RepositoryIndexer

    profile, _ = RepositoryIndexer("prj_test", str(repo_copy)).scan()
    assert profile.language == "typescript"
    assert profile.test_runner == "playwright"
    assert profile.bdd is True
    assert profile.detected_layout["pages_dir"] == "tests/pages"
    assert profile.detected_layout["steps_dir"] == "tests/steps"
    assert profile.detected_layout["features_dir"] == "tests/features"


def test_page_objects_and_members_are_extracted(repo_copy) -> None:
    from services.knowledge_service import RepositoryIndexer

    profile, _ = RepositoryIndexer("prj_test", str(repo_copy)).scan()
    names = {s.name for s in profile.symbols_of("page_object")}
    assert {"BasePage", "LoginPage", "DashboardPage"} <= names

    login = profile.find_symbol("LoginPage", kind="page_object")
    assert login is not None
    assert "extends BasePage" in login.signature
    assert "login" in login.members


def test_exact_case_wins_over_a_similarly_named_fixture(repo_copy) -> None:
    """`LoginPage` the class must not resolve to `loginPage` the fixture."""
    from services.knowledge_service import RepositoryIndexer

    profile, _ = RepositoryIndexer("prj_test", str(repo_copy)).scan()
    assert profile.find_symbol("LoginPage").kind == "page_object"
    assert profile.find_symbol("loginPage").kind == "fixture"


def test_naming_conventions_are_learned(repo_copy) -> None:
    from services.knowledge_service import RepositoryIndexer

    profile, _ = RepositoryIndexer("prj_test", str(repo_copy)).scan()
    assert profile.naming_conventions["page_object_base_class"] == "BasePage"
    assert profile.naming_conventions["test_id_prefix"] == "TC-"
    assert profile.naming_conventions["page_object_file"] == "PascalCase.ts"


def test_conventions_summary_names_reusable_assets(repo_copy) -> None:
    from services.knowledge_service import RepositoryIndexer

    profile, _ = RepositoryIndexer("prj_test", str(repo_copy)).scan()
    summary = profile.conventions_summary
    assert "LoginPage" in summary
    assert "fixture" in summary.lower()


async def test_indexing_then_retrieval_finds_relevant_code(repo_copy, offline_router) -> None:
    from services.knowledge_service import KnowledgeRetriever, RepositoryIndexer

    profile = await RepositoryIndexer("prj_test", str(repo_copy)).index(router=offline_router)
    assert profile.indexed_chunks > 0

    hits = await KnowledgeRetriever("prj_test").search(
        "how do page objects handle the login form", router=offline_router, limit=3
    )
    assert hits
    assert any("LoginPage" in hit["file_path"] for hit in hits)
    assert all(hit["score"] > 0 for hit in hits)


async def test_retrieval_can_be_scoped_by_kind(repo_copy, offline_router) -> None:
    from services.knowledge_service import KnowledgeRetriever, RepositoryIndexer

    await RepositoryIndexer("prj_test", str(repo_copy)).index(router=offline_router)
    hits = await KnowledgeRetriever("prj_test").search(
        "reusable fixtures", router=offline_router, limit=5, kinds=["fixture"]
    )
    assert hits
    assert all("fixture" in hit["file_path"].lower() for hit in hits)


def test_gherkin_is_parsed(repo_copy) -> None:
    from services.knowledge_service import parse_file

    text = (repo_copy / "tests" / "features" / "login.feature").read_text(encoding="utf-8")
    parsed = parse_file("tests/features/login.feature", text)
    assert parsed.feature_names == ["User login"]
    assert len(parsed.scenario_names) == 2
    assert any("TC-AUTH-001" in name for name in parsed.scenario_names)
