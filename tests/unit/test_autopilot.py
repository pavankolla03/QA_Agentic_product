"""Give it a URL and nothing else, and it works out what to test.

This is the headline capability, and it is also the easiest one to fake. A model
handed "https://shop.example.com" will cheerfully produce forty scenarios for a
site it has never loaded, each bound to a locator that does not exist, and every
later stage will treat that as ground truth. The tests here pin the opposite
behaviour at each step:

  - a bare URL is recognised as work, not as prose to reply to;
  - the requirement it produces has *no* acceptance criteria, because at that
    point nothing has been looked at yet;
  - the criteria come from the crawl, and only from what the crawl saw;
  - and when the crawl found nothing, the run fails saying so.

The last one is the point of the whole file. Everything else is convenience.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.base import AgentContext
from agents.exploration.agent import ExplorationAgent
from agents.requirement.agent import AUTOPILOT_EXPLORE_PAGES, RequirementAgent
from agents.test_design.agent import TestDesignAgent
from packages.aiqa_types.enums import RunMode
from packages.aiqa_types.models import Project, Requirement
from services.agent_engine.intents import Intent, resolve
from services.discovery.autopilot import (
    KIND_AUTH,
    KIND_LISTING,
    KIND_PAGE_LOAD,
    derive_features,
    summarise,
)
from services.knowledge_service.application_map import ApplicationMap


# --------------------------------------------------------------------------- #
# What the message meant
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "https://shop.example.com",
        "automate https://shop.example.com",
        "automate the entire application at https://shop.example.com",
        "test everything on shop.example.com please",
        "can you figure this out for me: https://shop.example.com",
    ],
)
def test_a_url_and_nothing_else_is_autopilot(message: str) -> None:
    resolution = resolve(message)
    assert resolution.intent is Intent.RUN_AUTOPILOT
    assert resolution.target_url == "https://shop.example.com"
    assert resolution.intent.starts_a_run
    assert resolution.mode is RunMode.FULL


def test_naming_a_target_is_an_ordinary_run() -> None:
    """The difference between autopilot and a normal run is whether a scope was given.

    "automate the login page at <url>" already says what to automate. Widening
    that to the whole application would be doing something nobody asked for,
    and on a large site it is an expensive something.
    """
    resolution = resolve("automate the login page at https://shop.example.com")
    assert resolution.intent is Intent.RUN_CREATE
    assert resolution.target_url == "https://shop.example.com"


def test_a_url_on_another_intent_still_carries_the_target() -> None:
    resolution = resolve("run the tests against https://staging.example.com")
    assert resolution.intent is Intent.RUN_TESTS
    assert resolution.target_url == "https://staging.example.com"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("localhost:3000", "http://localhost:3000"),
        ("127.0.0.1:8080/app", "http://127.0.0.1:8080/app"),
        ("192.168.1.10:9000", "http://192.168.1.10:9000"),
        ("10.0.0.5", "http://10.0.0.5"),
        ("shop.example.com", "https://shop.example.com"),
    ],
)
def test_local_addresses_default_to_http(message: str, expected: str) -> None:
    """A dev server has no certificate.

    Defaulting "localhost:3000" to HTTPS means the first thing autopilot does
    against somebody's local app is fail to connect, which reads as the feature
    being broken rather than the scheme being wrong.
    """
    assert resolve(message).target_url == expected


def test_a_greeting_is_still_a_greeting() -> None:
    assert resolve("hi").intent is Intent.CONVERSATION
    assert resolve("how many tests failed?").intent is Intent.RUN_FAILURE_QUERY


# --------------------------------------------------------------------------- #
# What the crawl is evidence for
# --------------------------------------------------------------------------- #
def _map(*pages: dict) -> ApplicationMap:
    """A map keyed by route, the way the crawler writes it."""
    return ApplicationMap(
        base_url="https://app.test", pages={page["route"]: page for page in pages}
    )


LOGIN_PAGE = {
    "route": "/login",
    "title": "Sign in - Acme",
    "forms": [{"action": "/session", "method": "post"}],
    "elements": [
        {"name": "email", "role": "textbox", "locator": "getByLabel('Email')",
         "confidence": 0.95, "required": True},
        {"name": "password", "role": "textbox", "locator": "getByLabel('Password')",
         "confidence": 0.95, "required": True, "input_type": "password"},
    ],
}


def test_a_password_field_makes_a_page_a_sign_in() -> None:
    features = derive_features(_map(LOGIN_PAGE))
    assert [f.kind for f in features] == [KIND_AUTH]

    feature = features[0]
    assert feature.name == "Sign in"          # not "Sign in - sign in"
    assert feature.route == "/login"
    assert any("valid credentials" in c for c in feature.criteria)
    assert any("incorrect password" in c for c in feature.criteria)
    # One rejection path per required field, so a missing field is a named test
    # rather than something the happy path happens to cover.
    assert "Submitting /login with email left empty is rejected" in feature.criteria
    assert "password field (password)" in feature.rationale


def test_a_validation_message_is_evidence_and_never_an_assertion() -> None:
    """We saw the text. We did not see what produced it.

    Turning an observed message into a "then" requires inventing the "when",
    and an invented "when" is how a suite ends up asserting on behaviour the
    application does not have.
    """
    page = dict(LOGIN_PAGE, validation_messages=["Email is required", "That password is wrong"])
    feature = derive_features(_map(page))[0]

    assert "Email is required" in feature.rationale
    assert not any("Email is required" in criterion for criterion in feature.criteria)


def test_rows_seen_during_the_crawl_are_assertable_and_absent_rows_are_not() -> None:
    with_rows = {
        "route": "/residents", "title": "Residents",
        "elements": [
            {"name": "table", "role": "table", "locator": "getByRole('table')", "confidence": 0.9},
            {"name": "row1", "role": "row", "locator": "getByRole('row')", "confidence": 0.9},
        ],
    }
    empty = {
        "route": "/archive", "title": "Archive",
        "elements": [
            {"name": "table", "role": "table", "locator": "getByRole('table')", "confidence": 0.9},
        ],
    }
    features = {f.route: f for f in derive_features(_map(with_rows, empty))}

    assert features["/residents"].kind == KIND_LISTING
    assert "/residents shows at least one row" in features["/residents"].criteria
    # Nothing was seen in this one, so nothing claims there is. A test asserting
    # "at least one row" here would fail the first time it met clean data.
    assert not any("at least one row" in c for c in features["/archive"].criteria)


def test_a_page_with_nothing_on_it_gets_a_load_check_only() -> None:
    page = {"route": "/about", "title": "About us", "elements": []}
    feature = derive_features(_map(page))[0]

    assert feature.kind == KIND_PAGE_LOAD
    assert feature.criteria == ['/about loads without error', '/about shows the title "About us"']


def test_an_aliased_route_is_not_tested_twice() -> None:
    """A catch-all handler answering 200 to a made-up path is one page, not two."""
    app_map = _map(LOGIN_PAGE, dict(LOGIN_PAGE, route="/valid"))
    app_map.aliases = {"/valid": "/login"}

    assert [f.route for f in derive_features(app_map)] == ["/login"]


def test_sign_in_survives_the_budget_and_page_loads_do_not() -> None:
    """Truncation must drop the least-supported checks, not the most important one."""
    pages = [{"route": f"/page-{i}", "title": f"Page {i}", "elements": []} for i in range(20)]
    features = derive_features(_map(LOGIN_PAGE, *pages), budget=3)

    assert len(features) == 3
    assert features[0].kind == KIND_AUTH


def test_a_page_captured_over_http_says_its_behaviour_is_unobserved() -> None:
    feature = derive_features(_map(dict(LOGIN_PAGE, simulated=True)))[0]

    assert feature.simulated
    assert feature.confidence < derive_features(_map(LOGIN_PAGE))[0].confidence
    assert "plain HTTP" in summarise([feature], "https://app.test")


def test_nothing_found_is_said_plainly() -> None:
    assert derive_features(_map()) == []
    assert "Nothing testable was discovered" in summarise([], "https://app.test")


# --------------------------------------------------------------------------- #
# What the pipeline does with it
# --------------------------------------------------------------------------- #
def _ctx(instruction: str) -> AgentContext:
    return AgentContext(
        run_id="r",
        project=Project(org_id="o", name="p", repository_path="."),
        instruction=instruction,
        mode=RunMode.FULL,
    )


def test_the_requirement_stage_writes_no_criteria_it_cannot_support() -> None:
    """At this point nothing has been looked at, so nothing can be claimed.

    The whole design rests on this being empty: it is what forces the criteria
    to come from the crawl rather than from a model's idea of what a shop
    usually has.
    """
    ctx = _ctx("https://shop.example.com")
    assert asyncio.run(_maybe(RequirementAgent(), ctx)) is None

    assert ctx.requirement is not None
    assert ctx.requirement.acceptance_criteria == []
    assert ctx.metadata["autopilot"] is True
    assert ctx.metadata["target_url"] == "https://shop.example.com"
    # A named feature needs a handful of pages; a whole application does not.
    assert ctx.metadata["max_explore_pages"] == AUTOPILOT_EXPLORE_PAGES


async def _maybe(agent, ctx: AgentContext) -> None:
    await agent.run(ctx)


def test_an_ordinary_instruction_still_goes_through_the_model_path() -> None:
    ctx = _ctx("automate the login page: valid sign-in and wrong password")
    assert RequirementAgent()._autopilot(ctx) is False
    assert ctx.requirement is None
    assert "autopilot" not in ctx.metadata


def test_exploration_turns_the_crawl_into_the_requirement() -> None:
    ctx = _ctx("https://app.test")
    ctx.metadata.update({"autopilot": True, "target_url": "https://app.test"})
    ctx.requirement = Requirement(raw_input="https://app.test", title="Automate", acceptance_criteria=[])

    ExplorationAgent()._autopilot_criteria(ctx, _map(LOGIN_PAGE))

    texts = [criterion.text for criterion in ctx.requirement.acceptance_criteria]
    assert any("valid credentials" in text for text in texts)
    # Every criterion carries the evidence it came from, so a reviewer can see
    # why it exists without re-crawling the site themselves.
    assert all(criterion.rationale for criterion in ctx.requirement.acceptance_criteria)
    assert ctx.metadata["discovered_features"][0]["kind"] == KIND_AUTH


def test_exploration_that_found_nothing_writes_nothing() -> None:
    ctx = _ctx("https://app.test")
    ctx.metadata.update({"autopilot": True, "target_url": "https://app.test"})
    ctx.requirement = Requirement(raw_input="https://app.test", title="Automate", acceptance_criteria=[])

    ExplorationAgent()._autopilot_criteria(ctx, _map())

    assert ctx.requirement.acceptance_criteria == []
    assert any("found nothing testable" in warning for warning in ctx.warnings)


def test_test_design_refuses_an_autopilot_run_that_discovered_nothing() -> None:
    """The one that matters.

    Without this the run designs scenarios from an empty requirement, generates
    code for them, compiles it, and reports success — for an application it
    never reached. A failed run naming the URL is the honest outcome.
    """
    ctx = _ctx("https://unreachable.test")
    ctx.metadata.update({"autopilot": True, "target_url": "https://unreachable.test"})
    ctx.requirement = Requirement(raw_input="x", title="Automate", acceptance_criteria=[])

    with pytest.raises(ValueError, match="nothing testable was discovered at https://unreachable.test"):
        asyncio.run(TestDesignAgent().run(ctx))
