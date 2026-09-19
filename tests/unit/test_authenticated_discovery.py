"""A URL and a password, and it tests the application rather than its front door.

Everything worth testing in a real application is behind a sign-in, and the
crawler could not sign in. Pointed at an application that enforces login it
followed every navigation link, was redirected each time, and recorded five
"pages" that were five copies of the login screen. Autopilot then derived
features from that, and the suite it produced described an application nobody
had ever seen. That is where "it just generated some random code" comes from:
there was nothing real to generate from.

Two halves, and both matter:

  - sign in, *verify* the sign-in worked, and never click Log out mid-crawl;
  - build the scenarios from what was seen instead of asking a model to imagine
    them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.aiqa_types.enums import RunStatus
from services.discovery.autopilot import KIND_AUTH, KIND_FORM, KIND_LISTING, derive_features
from services.discovery.credentials import AppCredentials, load, parse, save
from services.discovery.scenarios import plan_from_features
from services.knowledge_service.application_map import ApplicationMap


# --------------------------------------------------------------------------- #
# Reading credentials out of a sentence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "http://localhost:3000 user: std.user pass: Passw0rd!",
        "automate http://localhost:3000 username=std.user password=Passw0rd!",
        "http://localhost:3000 login: std.user, password: Passw0rd!",
    ],
)
def test_credentials_are_read_and_removed(message: str) -> None:
    """The cleaned text is what gets stored as the run's instruction.

    Which makes this the only thing standing between a typed password and a
    permanent record of it in a database every dashboard query reads.
    """
    cleaned, credentials = parse(message)

    assert credentials is not None
    assert credentials.username == "std.user"
    assert credentials.password == "Passw0rd!"
    assert "Passw0rd!" not in cleaned
    assert "http://localhost:3000" in cleaned


def test_a_slash_pair_is_read_only_after_a_credential_word() -> None:
    cleaned, credentials = parse("https://app.test sign in with alice / hunter2")
    assert credentials is not None and credentials.username == "alice"
    assert "hunter2" not in cleaned


@pytest.mark.parametrize(
    "message",
    [
        "automate https://shop.example.com",
        "automate the login page and the checkout flow",
        "cover the sign-in form and the dashboard",
    ],
)
def test_nothing_is_mistaken_for_credentials(message: str) -> None:
    """Labelled forms only.

    Two bare tokens near a URL are far more often a stray word than a password,
    and guessing wrong means typing somebody's sentence into a login form.
    """
    cleaned, credentials = parse(message)
    assert credentials is None
    assert cleaned == message


def test_a_password_never_appears_in_a_repr() -> None:
    """A dataclass repr in a traceback is how secrets escape.

    It happens at exactly the moment nobody is thinking about secrets.
    """
    text = repr(AppCredentials(username="alice", password="hunter2"))
    assert "hunter2" not in text
    assert "alice" in text


def test_credentials_are_stored_beside_the_project_and_git_ignored(tmp_path: Path) -> None:
    """Not in the control-plane database: it is shared, backed up, and has no
    encryption to offer a password."""
    saved = save(tmp_path, AppCredentials(username="alice", password="hunter2"))

    assert saved.is_file()
    assert json.loads(saved.read_text(encoding="utf-8"))["username"] == "alice"

    # One `git add -A` away from a public repository otherwise, and the person
    # who typed it into a chat panel has no reason to expect a file was created.
    ignore = (tmp_path / ".aiqa" / ".gitignore").read_text(encoding="utf-8")
    assert "credentials.json" in ignore

    loaded = load(tmp_path)
    assert loaded is not None and loaded.password == "hunter2"


def test_the_environment_wins_over_the_file(tmp_path: Path, monkeypatch) -> None:
    """A CI job sets variables rather than writing secrets into a checkout."""
    save(tmp_path, AppCredentials(username="from-file", password="file"))
    monkeypatch.setenv("AIQA_APP_USERNAME", "from-env")
    monkeypatch.setenv("AIQA_APP_PASSWORD", "env")

    loaded = load(tmp_path)
    assert loaded is not None and loaded.username == "from-env"


def test_no_credentials_is_not_an_error(tmp_path: Path) -> None:
    assert load(tmp_path) is None


# --------------------------------------------------------------------------- #
# What the crawler must refuse to do
# --------------------------------------------------------------------------- #
def test_the_crawler_never_follows_a_link_that_ends_its_session() -> None:
    """Clicking Log out mid-crawl turns every later page into the login screen.

    Which looks exactly like an application with one page, rather than like an
    error — so nothing downstream would have questioned it.
    """
    from tools.playwright.explorer_script import EXPLORER_MJS

    assert "SESSION_ENDING" in EXPLORER_MJS
    assert "if (SESSION_ENDING.test(next.pathname)) continue;" in EXPLORER_MJS


def test_a_sign_in_is_verified_rather_than_assumed() -> None:
    """Submitting a form is not the same as being signed in.

    A crawler that assumes success walks away with a session it does not have
    and reports one page five times as a five-page application.
    """
    from tools.playwright.explorer_script import EXPLORER_MJS

    assert "sign-in failed: still on a page with a password field" in EXPLORER_MJS
    # And the sign-in page itself is captured before we leave it, or the most
    # important flow in the product ends up with no coverage and no page.
    assert "Record the sign-in page before leaving it" in EXPLORER_MJS


def test_a_failed_sign_in_blocks_the_run() -> None:
    """Credentials were given and refused, so the application is unexplored.

    Whatever was produced describes the login page, not the thing somebody
    asked to have automated.
    """
    from agents.base import AgentContext
    from agents.orchestrator.graph import terminal_status
    from packages.aiqa_types.enums import RunMode
    from packages.aiqa_types.models import Project

    ctx = AgentContext(
        run_id="r",
        project=Project(org_id="o", name="p", repository_path="."),
        instruction="http://app.test",
        mode=RunMode.FULL,
    )
    ctx.metadata["sign_in_failed"] = "still on a page with a password field"

    assert terminal_status(ctx) is RunStatus.BLOCKED


# --------------------------------------------------------------------------- #
# Scenarios from evidence
# --------------------------------------------------------------------------- #
LOGIN = {
    "route": "/login",
    "title": "Sign in - Acme",
    "forms": [{"action": "/login", "method": "post"}],
    "elements": [
        {"name": "Username", "role": "textbox", "locator": "getByTestId('u')",
         "confidence": 0.98, "required": True},
        {"name": "Password", "role": "textbox", "locator": "getByTestId('p')",
         "confidence": 0.98, "required": True, "input_type": "password"},
        # Every real form has a control that submits it, and without one there
        # is no `submitForm` to bind "I submit the form" to.
        {"name": "Sign in", "role": "button", "locator": "getByTestId('login-submit')",
         "confidence": 0.98},
    ],
}
NEW_RESIDENT = {
    "route": "/residents/new",
    "title": "Add resident - Acme",
    "forms": [{"action": "/residents/new", "method": "post"}],
    "elements": [
        {"name": "Full name", "role": "textbox", "locator": "getByTestId('n')",
         "confidence": 0.98, "required": True},
        {"name": "Email", "role": "textbox", "locator": "getByTestId('e')",
         "confidence": 0.98, "required": True},
        {"name": "Notes", "role": "textbox", "locator": "getByTestId('no')", "confidence": 0.98},
        {"name": "Create resident", "role": "button", "locator": "getByTestId('r-submit')",
         "confidence": 0.98},
    ],
}
DASHBOARD = {"route": "/dashboard", "title": "Dashboard - Acme", "elements": []}


def _plan(*pages: dict):
    app_map = ApplicationMap(
        base_url="http://app.test", pages={page["route"]: page for page in pages}
    )
    return plan_from_features(derive_features(app_map), base_url="http://app.test")


def _all_steps(plan) -> list[str]:
    return [
        step.render()
        for spec in plan.features
        for scenario in spec.scenarios
        for step in scenario.steps
    ]


def test_nothing_is_asserted_that_the_crawl_did_not_see() -> None:
    """The rule the whole design rests on.

    A model handed the same features produced "the resident should exist in the
    database" for an application with no database step and no connection, and
    "the reports page should load", which asserts nothing whatsoever.
    """
    steps = " | ".join(_all_steps(_plan(LOGIN, NEW_RESIDENT, DASHBOARD))).lower()

    for invention in ("database", "should exist", "success message", "email is sent"):
        assert invention not in steps, invention


def test_every_assertion_names_a_page_or_a_title_that_was_observed() -> None:
    plan = _plan(LOGIN, NEW_RESIDENT, DASHBOARD)
    pages = {"Sign in", "Add resident", "Dashboard"}

    for spec in plan.features:
        for scenario in spec.scenarios:
            for step in scenario.steps:
                if step.keyword != "Then":
                    continue
                text = step.text
                assert any(page in text for page in pages) or "title is" in text or "row" in text, text


def test_only_real_data_is_quoted() -> None:
    """Cucumber turns a quoted fragment into a `{string}` parameter.

    Quoting the field name produced `I fill in {string} with {string}` — one
    step matching every field, bindable to no particular element, and duly
    reported as blocked. The page and the field belong in the sentence; the
    value somebody types is the only part that varies.
    """
    plan = _plan(NEW_RESIDENT)
    steps = _all_steps(plan)

    assert 'When I enter "QA Autopilot" in the Full name field on the Add resident page' in steps
    assert not any('in "Full name"' in step for step in steps)
    # Every step shape used here is one the happy path already needs.
    assert {step.split(" ", 1)[1] for step in steps} == {
        "I am on the Add resident page",
        'I enter "QA Autopilot" in the Full name field on the Add resident page',
        'I enter "qa.autopilot+{unique}@example.com" in the Email field on the Add resident page',
        'I enter "QA autopilot" in the Notes field on the Add resident page',
        "I submit the Add resident form",
        "I am taken away from the Add resident page",
        "I am still on the Add resident page",
    }


def test_a_field_is_left_empty_by_not_filling_it() -> None:
    """Which needs no step the happy path does not already have.

    There is no `submitTheFormWithTheEmailFieldEmpty` method to generate, so
    nothing new for the resolver to fail to bind. The scenario name says which
    field is missing; the steps show it.
    """
    plan = _plan(NEW_RESIDENT)
    rejection = next(
        scenario
        for spec in plan.features
        for scenario in spec.scenarios
        if "Email left empty" in scenario.name
    )
    filled = [step.text for step in rejection.steps if step.text.startswith("I enter")]

    assert any("Full name field" in step for step in filled)
    assert not any("Email field" in step for step in filled)
    assert rejection.steps[-1].text == "I am still on the Add resident page"


def test_the_sign_in_flow_is_covered_both_ways() -> None:
    plan = _plan(LOGIN)
    names = [s.name for spec in plan.features for s in spec.scenarios]

    assert "Sign in with valid credentials" in names
    assert "Reject an incorrect password" in names


def test_actions_use_the_field_names_the_application_showed_us() -> None:
    """A step naming a real field binds to a real locator.

    Steps written from imagination bind to nothing, which is how a suite ends up
    full of blocked steps that do not run.
    """
    steps = _all_steps(_plan(NEW_RESIDENT))

    joined = " | ".join(steps)
    assert '"QA Autopilot" in the Full name field on the Add resident page' in joined
    # `{unique}` is expanded per execution by the generated step definition: a
    # created record carries a uniqueness constraint, and a fixed address makes
    # the happy path pass exactly once.
    assert '"qa.autopilot+{unique}@example.com" in the Email field on the' in joined


def test_a_list_with_a_filter_box_is_a_list_not_a_form() -> None:
    """Checking for a form first made every index page a "form submission"
    whose one field was the search box, and the rows went untested."""
    listing = {
        "route": "/residents",
        "title": "Residents",
        "forms": [{"action": "/residents", "method": "get"}],
        "elements": [
            {"name": "Search residents", "role": "textbox", "locator": "getByTestId('q')",
             "confidence": 0.95},
            {"name": "row-1", "role": "row", "locator": "getByRole('row')", "confidence": 0.95},
        ],
    }
    features = derive_features(
        ApplicationMap(base_url="http://app.test", pages={"/residents": listing})
    )
    assert [f.kind for f in features] == [KIND_LISTING]


def test_a_create_form_is_still_a_form() -> None:
    features = derive_features(
        ApplicationMap(base_url="http://app.test", pages={"/residents/new": NEW_RESIDENT})
    )
    assert [f.kind for f in features] == [KIND_FORM]


def test_each_feature_is_planned_once() -> None:
    """Three separate feature files each testing sign-in is what the model did."""
    plan = _plan(LOGIN, NEW_RESIDENT, DASHBOARD)
    auth_files = [
        spec for spec in plan.features
        if any("sign in" in s.name.lower() for s in spec.scenarios)
    ]
    assert len(auth_files) == 1

    ids = [s.test_id for spec in plan.features for s in spec.scenarios]
    assert len(ids) == len(set(ids)), ids
    assert all(ids)


def test_the_plan_says_where_it_came_from() -> None:
    plan = _plan(LOGIN)
    assert "crawl" in plan.strategy.lower()
    assert derive_features(
        ApplicationMap(base_url="http://app.test", pages={"/login": LOGIN})
    )[0].kind == KIND_AUTH


# --------------------------------------------------------------------------- #
# The suite has to sign in too
# --------------------------------------------------------------------------- #
def test_pages_behind_a_login_sign_in_before_every_scenario() -> None:
    """The crawler authenticated; the generated tests did not.

    So the suite navigated straight to /residents/new, was redirected to the
    login page, and spent thirty seconds per scenario looking for a field that
    was not there — nine timeouts that all read `locator.fill` and none of which
    were about locators.
    """
    plan = plan_from_features(
        derive_features(
            ApplicationMap(base_url="http://app.test",
                           pages={p["route"]: p for p in (LOGIN, NEW_RESIDENT)})
        ),
        base_url="http://app.test",
        signed_in=True,
    )
    form = next(spec for spec in plan.features if "Add resident" in spec.name)

    assert [step.text for step in form.background] == [
        "I am on the Sign in page",
        "I sign in on the Sign in page with valid credentials",
    ]


def test_the_sign_in_feature_does_not_sign_in_first() -> None:
    """A Background that signs in before testing sign-in leaves those scenarios
    starting from the dashboard, testing nothing."""
    plan = plan_from_features(
        derive_features(ApplicationMap(base_url="http://app.test", pages={"/login": LOGIN})),
        base_url="http://app.test",
        signed_in=True,
    )
    assert plan.features[0].background == []


def test_an_application_with_no_login_gets_no_background() -> None:
    plan = plan_from_features(
        derive_features(ApplicationMap(base_url="http://app.test",
                                       pages={"/residents/new": NEW_RESIDENT})),
        base_url="http://app.test",
        signed_in=False,
    )
    assert all(spec.background == [] for spec in plan.features)


def test_every_step_including_the_background_is_bound() -> None:
    """A Background whose steps are undefined fails every scenario in the file
    before its first assertion."""
    from services.discovery.codegen import generation_plan

    app_map = ApplicationMap(
        base_url="http://app.test",
        pages={p["route"]: p for p in (LOGIN, NEW_RESIDENT)},
    )
    features = derive_features(app_map)
    plan = plan_from_features(features, base_url="http://app.test", signed_in=True)
    generation = generation_plan(features, plan, app_map.catalog(), base_class="BasePage")

    planned = {step.text for spec in plan.features for step in spec.background}
    planned |= {
        step.text for spec in plan.features
        for scenario in spec.scenarios for step in scenario.steps
    }
    assert planned - {step.text for step in generation.steps} == set()


def test_the_runner_is_given_the_account_the_suite_signs_in_with() -> None:
    """`sanitized_env` strips anything that looks like a password, correctly.

    So they have to be handed back deliberately. Without them `signIn()` fills
    two empty strings, the application stays on the login page, and every
    scenario behind it times out.
    """
    from agents.base import AgentContext
    from agents.execution.agent import ExecutionAgent
    from packages.aiqa_types.enums import RunMode
    from packages.aiqa_types.models import Project

    ctx = AgentContext(
        run_id="r",
        project=Project(org_id="o", name="p", repository_path="."),
        instruction="x",
        mode=RunMode.FULL,
    )
    ctx.credentials = AppCredentials(username="qa.bot", password="hunter2")

    assert ExecutionAgent._app_env(ctx) == {
        "AIQA_APP_USERNAME": "qa.bot",
        "AIQA_APP_PASSWORD": "hunter2",
    }


# --------------------------------------------------------------------------- #
# Dropdowns accept only what they list
# --------------------------------------------------------------------------- #
WITH_DROPDOWN = {
    "route": "/residents/new",
    "title": "Add resident - Acme",
    "forms": [{"action": "/residents/new", "method": "post"}],
    "elements": [
        {"name": "Full name", "role": "textbox", "locator": "getByTestId('n')",
         "confidence": 0.98, "required": True},
        {"name": "Resident type", "role": "combobox", "locator": "getByTestId('t')",
         "confidence": 0.98, "options": ["owner", "tenant"]},
        {"name": "Create resident", "role": "button", "locator": "getByTestId('c')",
         "confidence": 0.98},
    ],
}


def test_a_dropdown_is_filled_with_one_of_its_own_options() -> None:
    """`selectOption` on a value a select does not list waits for the timeout.

    A real run spent thirty seconds per scenario choosing "QA autopilot" from a
    dropdown whose options were "owner" and "tenant" — five failures that all
    said `locator.selectOption: Timeout` and none of which were about timing.
    """
    plan = _plan(WITH_DROPDOWN)
    steps = _all_steps(plan)

    assert 'I enter "owner" in the Resident type field on the' in " | ".join(steps)
    assert "QA autopilot" not in " | ".join(
        step for step in steps if "Resident type" in step
    )


def test_a_dropdown_whose_options_were_never_seen_is_left_alone() -> None:
    """Leaving it at its default is what a person filling the form would do.

    Guessing costs a timeout and explains nothing.
    """
    unseen = {
        **WITH_DROPDOWN,
        "elements": [
            dict(element, options=[]) if element["name"] == "Resident type" else element
            for element in WITH_DROPDOWN["elements"]
        ],
    }
    steps = _all_steps(_plan(unseen))

    assert not any("Resident type" in step for step in steps)
    assert any("Full name field" in step for step in steps)


# --------------------------------------------------------------------------- #
# One step text, one meaning
# --------------------------------------------------------------------------- #
def test_the_submit_step_names_its_page() -> None:
    """Cucumber matches a step by its text across the whole suite.

    A bare "I submit the form" is therefore one definition bound to one page
    object — the first feature that used it. Every other page's submit then
    clicked a button that was not on screen and waited out the full timeout:
    five failures reading `locator.click: Timeout`, all of them pressing the
    sign-in button while standing on the resident form.
    """
    plan = _plan(LOGIN, NEW_RESIDENT)
    submits = {step for step in _all_steps(plan) if "submit the" in step}

    assert "And I submit the Add resident form" in submits
    assert not any(step.endswith("I submit the form") for step in submits)


def test_a_step_that_means_two_pages_is_refused() -> None:
    """The guard for the whole class, not just the one instance.

    A step bound to the wrong page fails as a timeout on a control that is not
    there, which reads as a slow application rather than as a wiring mistake —
    so it has to be caught where the wiring happens.
    """
    import pytest as _pytest

    from packages.aiqa_types.models import FeatureSpec, GherkinStep, Scenario, TestPlan
    from services.discovery.codegen import generation_plan

    app_map = ApplicationMap(
        base_url="http://app.test",
        pages={p["route"]: p for p in (LOGIN, NEW_RESIDENT)},
    )
    features = derive_features(app_map)

    def _spec(name: str, page: str) -> FeatureSpec:
        return FeatureSpec(
            name=name,
            scenarios=[
                Scenario(
                    name=name,
                    steps=[
                        GherkinStep(keyword="Given", text=f"I am on the {page} page"),
                        # The same words on two different pages. It binds on
                        # both — that is what makes it dangerous rather than
                        # merely undefined.
                        GherkinStep(keyword="When", text="I submit the Sign in form"),
                    ],
                )
            ],
        )

    hand_made = TestPlan(features=[_spec("a", "Sign in"), _spec("b", "Add resident")])

    with _pytest.raises(ValueError, match="different things on different pages"):
        generation_plan(features, hand_made, app_map.catalog(), base_class="BasePage")


def test_a_filter_box_is_a_search_not_a_form_submission() -> None:
    """An index page's search field is a form with one input, and is not data entry.

    Treating it as one produced "submitting the Residents form takes you away
    from the Residents page" — the opposite of what a search does, and it failed
    for exactly that reason.
    """
    from services.discovery.autopilot import KIND_SEARCH

    listing = {
        "route": "/residents",
        "title": "Residents - Acme",
        "forms": [{"action": "/residents", "method": "get"}],
        "elements": [
            {"name": "Search residents", "role": "textbox", "locator": "getByTestId('q')",
             "confidence": 0.95},
            {"name": "Search", "role": "button", "locator": "getByTestId('s')",
             "confidence": 0.95},
        ],
    }
    feature = derive_features(
        ApplicationMap(base_url="http://app.test", pages={"/residents": listing})
    )[0]
    assert feature.kind == KIND_SEARCH

    steps = _all_steps(plan_from_features([feature], base_url="http://app.test"))
    assert "Then I am still on the Residents page" in steps
    assert not any("taken away" in step for step in steps)

