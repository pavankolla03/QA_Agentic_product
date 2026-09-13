"""The cost architecture: tiering, budgets, caching, reuse and permissions.

These tests assert the properties that make the platform cheap. A regression
here does not break a feature — it quietly multiplies the bill — so each one
checks a specific mechanism rather than an end-to-end outcome.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from packages.agent_protocol import permissions_for
from packages.agent_protocol.permissions import AGENT_PERMISSIONS
from packages.agent_protocol.permissions import Capability as AgentCapability
from packages.aiqa_types.budget import BudgetExceeded, ProviderQuota, RunBudget, score_complexity
from services.knowledge_service.application_map import (
    ApplicationMap,
    LocatorKnowledge,
    PageKnowledge,
    detect_components,
    dom_hash,
    route_of,
)
from services.knowledge_service.test_knowledge import TestKnowledge, TestKnowledgeStore, similarity


def _git_init(root: Path) -> None:
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"]):
        subprocess.run(command, cwd=root, check=False, capture_output=True)


# =========================================================================== #
# Tiering: pay only where judgement matters
# =========================================================================== #
def test_expensive_tiers_are_reserved_for_reasoning(offline_router) -> None:
    reasoning = {"requirement", "test_design", "failure_analysis", "self_healing"}
    for agent in reasoning:
        assert offline_router.tier_for(agent=agent) == "reasoning", agent
    for agent in ("repository", "exploration", "standards", "execution", "reporting", "orchestrator"):
        assert offline_router.tier_for(agent=agent) == "cheap", agent


def test_trivial_work_is_downgraded_off_the_paid_tier(offline_router) -> None:
    budget = RunBudget()
    tier, reason = offline_router.adjust_tier("reasoning", complexity=0.02, retry=0, budget=budget)
    assert tier == "cheap"
    assert "below reasoning threshold" in reason


def test_genuinely_hard_work_may_escalate(offline_router) -> None:
    budget = RunBudget()
    tier, reason = offline_router.adjust_tier("cheap", complexity=0.9, retry=0, budget=budget)
    assert tier == "reasoning"
    assert budget.escalations == 1


def test_escalation_is_capped(offline_router) -> None:
    """Cheap work must not be able to escalate itself indefinitely."""
    budget = RunBudget()
    cap = offline_router.max_escalations
    for _ in range(cap + 3):
        offline_router.adjust_tier("cheap", complexity=0.95, retry=0, budget=budget)
    assert budget.escalations == cap


def test_complexity_scoring_is_free_and_ordered() -> None:
    trivial = score_complexity("Summarize these three file names")
    hard = score_complexity(
        "Determine the root cause of this intermittent flaky race condition " * 40, artifacts=9, retry=1
    )
    assert 0.0 <= trivial < hard <= 1.0


# =========================================================================== #
# Budgets: three independent ceilings
# =========================================================================== #
@pytest.mark.parametrize(
    "budget,expected",
    [
        (RunBudget(max_requests=1, max_cost_usd=99), "requests"),
        (RunBudget(max_input_tokens=10, max_cost_usd=99, max_requests=99), "input_tokens"),
        (RunBudget(max_output_tokens=10, max_cost_usd=99, max_requests=99), "output_tokens"),
        (RunBudget(max_cost_usd=0.001, max_requests=99), "cost"),
    ],
)
def test_each_ceiling_stops_the_run(budget: RunBudget, expected: str) -> None:
    budget.record(cost=1.0, input_tokens=100, output_tokens=100, free=False)
    with pytest.raises(BudgetExceeded) as excinfo:
        budget.check()
    assert excinfo.value.kind == expected


def test_budget_survives_a_resume(project, org_user) -> None:
    """A resumed run must not get a fresh budget — that would defeat the ceiling."""
    from packages.aiqa_types.enums import RunMode
    from packages.aiqa_types.models import RunRequest
    from services.agent_engine.engine import AgentEngine
    from services.observability.db import session_scope
    from services.observability.models import RunRow

    org_id, user_id = org_user
    engine = AgentEngine(offline=True)
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    with session_scope() as session:
        row = session.get(RunRow, run_id)
        metadata = dict(row.metadata_json or {})
        metadata["budget_consumed"] = {"requests": 7, "input_tokens": 1234, "spent_usd": 0.5}
        row.metadata_json = metadata

    snapshot = engine._run_snapshot(run_id)
    ctx, _tracker = engine._build_context(run_id, project, snapshot)
    assert ctx.budget.requests == 7
    assert ctx.budget.input_tokens == 1234
    assert ctx.budget.spent_usd == pytest.approx(0.5)


def test_provider_quota_reserves_headroom() -> None:
    """Free tiers are request-limited; we stop using them before they run out."""
    quota = ProviderQuota("openrouter", daily_limit=100, reserve_threshold=10)
    for _ in range(89):
        quota.record()
    assert quota.available()
    quota.record()
    assert not quota.available(), "should stop at the reserve, leaving headroom"


# =========================================================================== #
# Repository map: incremental indexing
# =========================================================================== #
async def test_unchanged_repository_costs_nothing(repo_copy, offline_router) -> None:
    from services.knowledge_service.incremental import IncrementalIndexer

    _git_init(repo_copy)
    indexer = IncrementalIndexer("prj_cache", repo_copy)

    _profile, cold, _map = await indexer.sync(offline_router)
    assert cold.added, "the first pass must index everything"

    budget = RunBudget()
    _profile, warm, _map = await indexer.sync(offline_router, budget=budget)
    assert warm.reused_from_cache
    assert warm.embeddings_computed == 0
    assert budget.tokens_saved > 0


async def test_one_changed_file_reindexes_only_that_file(repo_copy, offline_router) -> None:
    from services.knowledge_service.incremental import IncrementalIndexer

    _git_init(repo_copy)
    indexer = IncrementalIndexer("prj_delta", repo_copy)
    await indexer.sync(offline_router)

    (repo_copy / "tests" / "pages" / "LoginPage.ts").write_text(
        (repo_copy / "tests" / "pages" / "LoginPage.ts").read_text(encoding="utf-8") + "\n// touched\n",
        encoding="utf-8",
    )
    _profile, delta, _map = await indexer.sync(offline_router)
    assert delta.modified == ["tests/pages/LoginPage.ts"]
    assert delta.unchanged >= 8
    assert delta.embeddings_computed <= 2


async def test_removed_files_leave_the_index(repo_copy, offline_router) -> None:
    from services.knowledge_service.incremental import IncrementalIndexer

    _git_init(repo_copy)
    indexer = IncrementalIndexer("prj_rm", repo_copy)
    await indexer.sync(offline_router)

    (repo_copy / "tests" / "pages" / "DashboardPage.ts").unlink()
    _profile, delta, repo_map = await indexer.sync(offline_router)
    assert "tests/pages/DashboardPage.ts" in delta.removed
    assert "tests/pages/DashboardPage.ts" not in repo_map.files


def test_the_map_writing_itself_does_not_dirty_the_index(repo_copy) -> None:
    """Regression: `.aiqa/` churn must not be mistaken for a source change."""
    from services.knowledge_service.repository_map import relevant_dirty

    dirty = {".aiqa/repository_map.json", ".aiqa/application_map.json",
             "node_modules/x/index.js", "tests/pages/LoginPage.ts", "package-lock.json"}
    assert relevant_dirty(dirty) == {"tests/pages/LoginPage.ts"}


# =========================================================================== #
# Application map: explore once
# =========================================================================== #
def _page(route: str, n: int = 3, simulated: bool = False) -> PageKnowledge:
    from dataclasses import asdict

    elements = [
        asdict(LocatorKnowledge(name=f"field{i}", role="textbox",
                                locator=f"getByTestId('f{i}')", strategy="getByTestId", confidence=0.9))
        for i in range(n)
    ]
    return PageKnowledge(route=route, url=f"http://app{route}", elements=elements,
                         dom_hash=dom_hash(elements), simulated=simulated)


def test_a_known_page_is_not_re_explored() -> None:
    app_map = ApplicationMap(project_id="p")
    app_map.put_page(_page("/residents/new"))
    needed, reason = app_map.needs_exploration("/residents/new")
    assert not needed
    assert "cached" in reason


def test_an_unknown_page_is_explored() -> None:
    needed, reason = ApplicationMap(project_id="p").needs_exploration("/unseen")
    assert needed and "never been explored" in reason


def test_failed_locators_force_a_re_crawl() -> None:
    app_map = ApplicationMap(project_id="p")
    app_map.put_page(_page("/residents/new"))
    needed, reason = app_map.needs_exploration(
        "/residents/new", failed_locators={"getByTestId('f1')"}
    )
    assert needed and "failed" in reason


def test_a_version_change_invalidates_the_map() -> None:
    app_map = ApplicationMap(project_id="p", app_version="1.0.0")
    app_map.put_page(_page("/residents/new"))
    needed, reason = app_map.needs_exploration("/residents/new", app_version="2.0.0")
    assert needed and "version changed" in reason


def test_http_captures_are_cached_when_no_browser_exists() -> None:
    """Regression: without Playwright, re-crawling would loop on the same HTML."""
    app_map = ApplicationMap(project_id="p")
    app_map.put_page(_page("/residents/new", simulated=True))

    needed, _ = app_map.needs_exploration("/residents/new", browser_available=False)
    assert not needed, "no browser means the HTTP capture is the best available"

    needed, reason = app_map.needs_exploration("/residents/new", browser_available=True)
    assert needed and "browser is now available" in reason


def test_locator_confidence_reacts_to_outcomes() -> None:
    locator = LocatorKnowledge(locator="getByTestId('x')", confidence=0.6)
    locator.record_success()
    assert locator.confidence > 0.6
    before = locator.confidence
    locator.record_failure()
    assert locator.confidence < before * 0.6, "failure must be punished harder than success rewards"
    for _ in range(3):
        locator.record_failure()
    assert not locator.trustworthy


def test_components_shared_across_pages_are_detected() -> None:
    from dataclasses import asdict

    def nav_page(route: str) -> PageKnowledge:
        elements = [
            asdict(LocatorKnowledge(name="Dashboard", role="link",
                                    locator="getByTestId('nav-dashboard')", confidence=0.9)),
            asdict(LocatorKnowledge(name="Search residents", role="textbox",
                                    locator="getByTestId('resident-search')", confidence=0.9)),
        ]
        return PageKnowledge(route=route, elements=elements)

    components = detect_components([nav_page("/a"), nav_page("/b")])
    assert "NavigationBar" in components
    assert components["NavigationBar"]["shared"] is True
    assert "SearchBox" in components


def test_route_patterns_generalise_ids() -> None:
    assert route_of("http://app/residents/482/edit") == "/residents/:id/edit"
    assert route_of("http://app/residents") == "/residents"


def test_dom_hash_ignores_cosmetic_markup() -> None:
    """A restyle should not throw away good locator knowledge; a new field should."""
    base = [{"role": "textbox", "name": "Email", "input_type": "email"}]
    restyled = [{"role": "textbox", "name": "Email", "input_type": "email"}]
    changed = [*base, {"role": "textbox", "name": "Phone", "input_type": "tel"}]
    assert dom_hash(base) == dom_hash(restyled)
    assert dom_hash(base) != dom_hash(changed)


# =========================================================================== #
# Reuse: do not regenerate what exists
# =========================================================================== #
def test_similarity_favours_containment_over_size(isolated_env) -> None:
    short_query = "resident registration"
    long_match = "Create a new resident with valid details Resident Registration form submit"
    assert similarity(short_query, long_match) > 0.4


def test_duplicate_scenarios_are_recognised(isolated_env) -> None:
    store = TestKnowledgeStore("prj_dup")
    store.put(TestKnowledge(test_id="TC-1", name="Create a new resident with valid details"))
    assert store.duplicate_of("Create a new resident with valid details") is not None
    assert store.duplicate_of("Create a resident with valid details") is not None
    assert store.duplicate_of("Export the monthly financial report") is None


def test_reusable_assets_come_back_from_prior_tests(isolated_env) -> None:
    store = TestKnowledgeStore("prj_reuse")
    store.put(
        TestKnowledge(
            test_id="TC-1", name="Create a resident", feature="Resident Registration",
            page_objects=["ResidentPage"], fixtures=["authenticatedPage"],
            apis=["POST /api/residents"], db_tables=["residents"],
        )
    )
    assets = store.reusable_assets("resident registration")
    assert "ResidentPage" in assets["page_objects"]
    assert "authenticatedPage" in assets["fixtures"]


# =========================================================================== #
# Permissions: least privilege
# =========================================================================== #
def test_no_agent_may_push_by_default() -> None:
    for name, permission in AGENT_PERMISSIONS.items():
        assert AgentCapability.PUSH not in permission.capabilities, name


@pytest.mark.parametrize(
    "agent,tool,allowed",
    [
        ("repository", "fs.read_file", True),
        ("repository", "fs.write_file", False),
        ("code_generation", "fs.write_file", False),   # it proposes; execution applies
        ("execution", "fs.write_file", True),
        ("execution", "git.commit", True),
        ("execution", "git.push", False),
        ("self_healing", "fs.write_file", True),
        ("self_healing", "git.commit", False),
        ("test_design", "playwright.run_tests", False),
        ("reporting", "slack.notify", True),
        ("reporting", "db.query", False),
        ("exploration", "fs.write_file", False),
    ],
)
def test_permission_matrix(agent: str, tool: str, allowed: bool) -> None:
    assert permissions_for(agent).allows_tool(tool) is allowed


def test_unknown_tools_are_denied() -> None:
    assert not permissions_for("execution").allows_tool("evil.exfiltrate")


async def test_an_agent_cannot_use_an_ungranted_tool(agent_ctx) -> None:
    """End-to-end: the refusal happens at the invocation boundary, and is audited."""
    from agents.repository.agent import RepositoryAgent

    result = RepositoryAgent().tool(agent_ctx, "fs.write_file", path="tests/x.ts", content="x")
    assert not result.ok
    assert result.rule == "agent.permission"
    assert not (Path(agent_ctx.project_root) / "tests" / "x.ts").exists()


# =========================================================================== #
# Standards engine
# =========================================================================== #
def test_prose_standards_become_enforceable_rules() -> None:
    from services.knowledge_service.standards_engine import parse_freeform_standards

    rules, unparsed = parse_freeform_standards(
        "Steps cannot contain locators.\n"
        "Assertions must remain outside Page Objects.\n"
        "Never use absolute xpath.\n"
        "We like pizza on Fridays.\n"
    )
    ids = {r["id"] for r in rules}
    assert "STD-004" in ids and "STD-002" in ids
    assert any("ASSERT" in i for i in ids)
    assert unparsed == ["We like pizza on Fridays."], "unrecognised prose must be reported, not dropped"


def test_house_style_is_learned_from_example_files(repo_copy) -> None:
    from services.knowledge_service.standards_engine import learn_house_style

    examples = repo_copy / ".aiqa" / "examples"
    examples.mkdir(parents=True, exist_ok=True)
    shutil.copy(repo_copy / "tests" / "pages" / "LoginPage.ts", examples)
    shutil.copy(repo_copy / "tests" / "features" / "login.feature", examples)

    style = learn_house_style(examples)
    assert style.page_object_base_class == "BasePage"
    assert style.locator_accessor == "getter"
    assert "getByTestId" in style.preferred_locators
    assert "BasePage" in style.briefing()


def test_standards_layer_company_then_project(repo_copy) -> None:
    from services.knowledge_service.standards_engine import StandardsEngine

    resolved = StandardsEngine(repo_copy).resolve()
    ids = {r["id"] for r in resolved.rules}
    assert "STD-001" in ids, "company baseline must survive"
    assert "ACME-001" in ids, "project override must be merged in"
    assert len(resolved.sources) >= 2


# =========================================================================== #
# Static validation
# =========================================================================== #
def test_static_checks_skip_cleanly_without_a_toolchain(repo_copy) -> None:
    from packages.aiqa_types.enums import ArtifactKind
    from packages.aiqa_types.models import FileChange
    from services.execution_service.static_validation import StaticValidationPipeline

    report = StaticValidationPipeline(repo_copy, {"layout": {"features_dir": "tests/features"}}).run(
        [FileChange(path="tests/features/x.feature", kind=ArtifactKind.FEATURE,
                    content="@smoke\nFeature: X\n\n  @smoke\n  Scenario: a\n    Given b\n    Then c\n")]
    )
    assert "gherkin" in report.ran
    assert report.passed
    assert "typescript" in report.skipped


def test_misplaced_files_are_caught(repo_copy) -> None:
    from packages.aiqa_types.enums import ArtifactKind
    from packages.aiqa_types.models import FileChange
    from services.execution_service.static_validation import StaticValidationPipeline

    report = StaticValidationPipeline(
        repo_copy, {"layout": {"pages_dir": "tests/pages", "features_dir": "tests/features"}}
    ).run([FileChange(path="src/WrongPlacePage.ts", kind=ArtifactKind.PAGE_OBJECT, content="export class X {}")])
    assert not report.passed
    assert any(v.rule_id == "STR-001" for v in report.violations)


def test_prose_standards_split_on_sentences_not_just_lines() -> None:
    """Regression: a pasted paragraph lost every rule after the first."""
    from services.knowledge_service.standards_engine import parse_freeform_standards

    rules, unparsed = parse_freeform_standards(
        "Steps cannot contain locators. Never use absolute xpath. "
        "Every scenario must be tagged. Our team likes tabs over spaces."
    )
    ids = {r["id"] for r in rules}
    assert {"STD-004", "STD-002", "STD-005"} <= ids
    assert unparsed == ["Our team likes tabs over spaces."]


def test_duplicate_rule_ids_are_not_emitted_twice() -> None:
    """Two phrasings of the same rule must not produce two conflicting entries."""
    from services.knowledge_service.standards_engine import parse_freeform_standards

    rules, _ = parse_freeform_standards(
        "Steps cannot contain locators.\nThe Page Object must contain all UI interactions.\n"
    )
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids))
