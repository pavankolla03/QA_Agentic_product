"""Scenarios written from what the crawl saw, not from what a model expects.

Handing discovered features to a language model and asking for Gherkin produces
prose that reads well and tests nothing. A real run against a five-page demo
produced `Then the resident should exist in the database` — there is no database
step, no database connection and no evidence any record is stored — alongside
`Then the reports page should load`, which asserts nothing at all, and a
`Scenario Outline` whose placeholder was `{string}` with no Examples table,
which is not valid Gherkin. Three separate feature files each tested sign-in.

So autopilot does not ask. It renders the plan from the `DiscoveredFeature`
list, under one rule:

    **an assertion may only reference something the crawl observed** — a page
    title, an element's accessible name, or a route.

That rule is what makes the difference. "The record appears in the results list"
is unbounded invention; "I am no longer on /residents/new" is the same claim
reduced to something the browser can actually check, and it is true or false for
an honest reason. Actions are bound to elements that were seen, so every step
has a locator behind it and the step-coverage stage has nothing left to block.

The vocabulary is deliberately tiny and fixed. A handful of step shapes cover
every feature kind, which means the step definitions behind them are the same
handful every time — not a fresh set of near-duplicates per run, each with its
own way of saying "click the button".

**Only real data is quoted.** Cucumber turns a quoted fragment into a `{string}`
parameter, so quoting the field name produced `I fill in {string} with {string}`
— one step matching every field, bindable to no particular element, and duly
reported as blocked. The page and the field belong *in* the sentence; the value
somebody types is the only part that varies.
"""

from __future__ import annotations

import re
from typing import Any

from packages.aiqa_types.enums import Priority, TestLayer
from packages.aiqa_types.models import FeatureSpec, GherkinStep, Scenario, TestPlan
from services.discovery.autopilot import (
    KIND_AUTH,
    KIND_FORM,
    KIND_LISTING,
    KIND_NAVIGATION,
    KIND_PAGE_LOAD,
    KIND_SEARCH,
    DiscoveredFeature,
)

#: Sample values by field name. Only ever used to *fill* a field, never to
#: assert anything, so being wrong costs a re-run and not a false result.
_SAMPLES: tuple[tuple[str, str], ...] = (
    (r"e-?mail", "qa.autopilot@example.com"),
    (r"phone|mobile|tel", "5550100"),
    (r"zip|postcode|postal", "12345"),
    (r"unit|flat|apartment|room", "A-101"),
    (r"date|dob|birth", "2000-01-01"),
    (r"amount|price|cost|qty|quantity|number", "12"),
    (r"url|website|link", "https://example.com"),
    (r"name", "QA Autopilot"),
)
_DEFAULT_SAMPLE = "QA autopilot"


def plan_from_features(
    features: list[DiscoveredFeature],
    *,
    run_id: str = "",
    requirement_id: str = "",
    base_url: str = "",
    signed_in: bool = False,
) -> TestPlan:
    """A complete, bindable test plan built only from observed evidence.

    `signed_in` says the crawler had to authenticate to see these pages, which
    means every test of them has to authenticate too. Leaving that out produced
    a suite that navigated straight to /residents/new, was redirected to the
    login page, and spent thirty seconds per scenario looking for a field that
    was not there — nine timeouts that all said `locator.fill` and none of which
    were about locators.
    """
    sign_in_page = _sign_in_page(features) if signed_in else ""
    specs: list[FeatureSpec] = []
    counter = 0

    for feature in features:
        scenarios: list[Scenario] = []
        for scenario in _scenarios_for(feature):
            counter += 1
            scenario.test_id = f"TC-AUTO-{counter:03d}"
            scenarios.append(scenario)
        if not scenarios:
            continue
        specs.append(
            FeatureSpec(
                name=feature.name,
                file_name=f"{_slug(feature.name)}.feature",
                description=feature.rationale,
                tags=[f"@{feature.kind.replace('_', '-')}"],
                background=_sign_in_background(feature, sign_in_page),
                scenarios=scenarios,
            )
        )

    return TestPlan(
        run_id=run_id,
        requirement_id=requirement_id,
        title=f"Automated coverage for {base_url}" if base_url else "Automated coverage",
        strategy=(
            "Derived from the crawl rather than designed by a model. Every action is bound to an "
            "element that was observed, and every assertion checks a route, a title or an element "
            "that was observed. Nothing here describes behaviour the crawl did not see."
        ),
        features=specs,
    )


# --------------------------------------------------------------------------- #
def _sign_in_page(features: list[DiscoveredFeature]) -> str:
    """The page the suite has to go through before anything else works."""
    for feature in features:
        if feature.kind == KIND_AUTH:
            return _page_name(feature)
    return ""


def _sign_in_background(feature: DiscoveredFeature, sign_in_page: str) -> list[GherkinStep]:
    """Sign in before every scenario on a page that is behind the login.

    Not on the sign-in feature itself, which is *about* signing in: a Background
    that signs in before testing sign-in would leave those scenarios starting
    from the dashboard, testing nothing.
    """
    if not sign_in_page or feature.kind == KIND_AUTH:
        return []
    return [
        GherkinStep(keyword="Given", text=f"I am on the {sign_in_page} page"),
        GherkinStep(keyword="And", text="I sign in with valid credentials"),
    ]


def _scenarios_for(feature: DiscoveredFeature) -> list[Scenario]:
    if feature.kind == KIND_AUTH:
        return _auth(feature)
    if feature.kind == KIND_FORM:
        return _form(feature)
    if feature.kind == KIND_LISTING:
        return _listing(feature)
    if feature.kind == KIND_NAVIGATION:
        return _navigation(feature)
    if feature.kind == KIND_SEARCH:
        return _search(feature)
    if feature.kind == KIND_PAGE_LOAD:
        return _page_load(feature)
    return []


def _auth(feature: DiscoveredFeature) -> list[Scenario]:
    """Sign-in, both ways.

    The happy path is the one thing in the whole plan already proven to work:
    the crawler signed in with these credentials before it could see any of the
    pages this plan covers. Asserting that it leaves the sign-in page is the
    weakest true statement available, and a true weak assertion beats a
    confident invented one.
    """
    page = _page_name(feature)
    scenarios = [
        Scenario(
            name="Sign in with valid credentials",
            description="Verified during exploration: this is how the crawler reached the application.",
            tags=["@smoke", "@P0"],
            priority=Priority.P0,
            layer=TestLayer.UI,
            steps=[
                GherkinStep(keyword="Given", text=f"I am on the {page} page"),
                GherkinStep(keyword="When", text="I sign in with valid credentials"),
                GherkinStep(keyword="Then", text=f"I am taken away from the {page} page"),
            ],
        ),
        Scenario(
            name="Reject an incorrect password",
            description="The rejection path for the same form.",
            tags=["@negative", "@P1"],
            priority=Priority.P1,
            layer=TestLayer.UI,
            negative=True,
            steps=[
                GherkinStep(keyword="Given", text=f"I am on the {page} page"),
                GherkinStep(keyword="When", text="I sign in with an incorrect password"),
                GherkinStep(keyword="Then", text=f"I am still on the {page} page"),
            ],
        ),
    ]
    scenarios.extend(_required_field_scenarios(feature))
    return scenarios


def _form(feature: DiscoveredFeature) -> list[Scenario]:
    """Fill everything and submit, plus one rejection path per required field."""
    page = _page_name(feature)
    fillable = [name for name in feature.fields if name]
    steps: list[GherkinStep] = [GherkinStep(keyword="Given", text=f"I am on the {page} page")]
    for index, field in enumerate(fillable[:8]):
        steps.append(
            GherkinStep(
                keyword="When" if index == 0 else "And",
                # The field is part of the sentence, the value is the parameter.
                # Quoting the field name would collapse every one of these into
                # a single step matching all of them and binding to none.
                text=f'I enter "{_sample(field)}" in the {field} field',
            )
        )
    steps.append(GherkinStep(keyword="And", text="I submit the form"))
    # "Accepted" without having ever submitted during the crawl is not something
    # we know. "We left the page we were on" is the observable consequence, and
    # it is wrong loudly rather than passing quietly.
    steps.append(GherkinStep(keyword="Then", text=f"I am taken away from the {page} page"))

    scenarios = [
        Scenario(
            name="Submit the form with every field completed",
            description=feature.rationale,
            tags=["@smoke", "@P1"],
            priority=Priority.P1,
            layer=TestLayer.UI,
            steps=steps,
        )
    ]
    scenarios.extend(_required_field_scenarios(feature))
    return scenarios


def _required_field_scenarios(feature: DiscoveredFeature) -> list[Scenario]:
    """One rejection scenario per field the application marks required.

    Leaving a field empty is expressed by *not filling it* — every other field
    is filled and this one is skipped. That needs no step the happy path does
    not already have, so there is no `submitTheFormWithTheEmailFieldEmpty`
    method to generate and nothing new for the resolver to fail to bind. The
    scenario name says which field is missing; the steps show it.

    An earlier version made this one Scenario Outline over the field names, but
    the field was the part that varied, so it could only be a `{string}`
    parameter — one step matching every field and bindable to no element.
    """
    required = _required_fields(feature)[:3]
    fillable = [name for name in feature.fields if name]
    if not required or len(fillable) < 2:
        return []

    page = _page_name(feature)
    scenarios: list[Scenario] = []
    for missing in required:
        steps = [GherkinStep(keyword="Given", text=f"I am on the {page} page")]
        first = True
        for field in fillable[:8]:
            if field == missing:
                continue
            steps.append(
                GherkinStep(
                    keyword="When" if first else "And",
                    text=f'I enter "{_sample(field)}" in the {field} field',
                )
            )
            first = False
        steps.append(GherkinStep(keyword="And", text="I submit the form"))
        steps.append(GherkinStep(keyword="Then", text=f"I am still on the {page} page"))
        scenarios.append(
            Scenario(
                name=f"Reject a submission with {missing} left empty",
                description=f"The application marks {missing} required.",
                tags=["@negative", "@P1"],
                priority=Priority.P1,
                layer=TestLayer.UI,
                negative=True,
                steps=steps,
            )
        )
    return scenarios


def _listing(feature: DiscoveredFeature) -> list[Scenario]:
    return [
        Scenario(
            name="The list renders its rows",
            description=feature.rationale,
            tags=["@P2"],
            priority=Priority.P2,
            layer=TestLayer.UI,
            steps=[
                GherkinStep(keyword="Given", text=f"I am on the {_page_name(feature)} page"),
                GherkinStep(keyword="Then", text="I should see at least one row"),
            ],
        )
    ]


def _navigation(feature: DiscoveredFeature) -> list[Scenario]:
    """Nothing, deliberately.

    The crawl recorded which routes it reached by following links, but not the
    link text that got it there — and "I follow the /residents link" is not a
    sentence anybody writes, nor one a locator can be found for. Meanwhile every
    destination already has a page-load scenario that opens it directly, so the
    only thing an invented navigation scenario would add is a step that cannot
    bind.

    Capturing link text during the crawl would make this worth writing. Until
    then, writing it would mean guessing.
    """
    return []


def _search(feature: DiscoveredFeature) -> list[Scenario]:
    box = feature.fields[0] if feature.fields else "search"
    return [
        Scenario(
            name="Searching updates the page",
            description=feature.rationale,
            tags=["@P2"],
            priority=Priority.P2,
            layer=TestLayer.UI,
            steps=[
                GherkinStep(keyword="Given", text=f"I am on the {_page_name(feature)} page"),
                GherkinStep(keyword="When", text=f'I enter "{_DEFAULT_SAMPLE}" in the {box} field'),
                GherkinStep(keyword="And", text="I submit the form"),
                GherkinStep(keyword="Then", text=f"I am still on the {_page_name(feature)} page"),
            ],
        )
    ]


def _page_load(feature: DiscoveredFeature) -> list[Scenario]:
    """The weakest useful test, and completely honest.

    The title was read off the page during the crawl. Asserting it catches the
    two failures that matter for a page with nothing on it: the route stopped
    existing, and the route started serving something else.
    """
    page = _page_name(feature)
    title = _title_of(feature.criteria)
    steps = [GherkinStep(keyword="Given", text=f"I am on the {page} page")]
    if title:
        # The title is data read off the page, so it is genuinely a parameter.
        steps.append(GherkinStep(keyword="Then", text=f'the page title is "{title}"'))
    else:
        steps.append(GherkinStep(keyword="Then", text=f"I am still on the {page} page"))
    return [
        Scenario(
            name=f"The {page} page loads",
            description=feature.rationale,
            tags=["@smoke", "@P2"],
            priority=Priority.P2,
            layer=TestLayer.UI,
            steps=steps,
        )
    ]


# --------------------------------------------------------------------------- #
def _page_name(feature: DiscoveredFeature) -> str:
    """What to call this page inside a step sentence.

    The feature name with its kind suffix removed: "Add resident - form
    submission" is a heading, "Add resident" is how somebody refers to the page
    in a sentence. Falls back to the route, which always exists.
    """
    name = re.split(r"\s+-\s+", feature.name)[0].strip()
    if name and name.lower() not in ("home", ""):
        return name
    return feature.route.strip("/") or "home"


_TITLE_RE = re.compile(r'shows the title "([^"]+)"')
_ROUTE_RE = re.compile(r"^(/\S*)")


def _title_of(criteria: list[str]) -> str:
    for text in criteria:
        match = _TITLE_RE.search(text)
        if match:
            return match.group(1)
    return ""


def _route_of(text: str) -> str:
    match = _ROUTE_RE.match(text.strip())
    return match.group(1) if match else ""


def _required_fields(feature: DiscoveredFeature) -> list[str]:
    """The fields the application itself marked required.

    Read back out of the criteria autopilot wrote, which is where the `required`
    attribute observed on each input ended up.
    """
    out: list[str] = []
    for text in feature.criteria:
        match = re.search(r"with (.+?) left empty", text)
        if match:
            out.append(match.group(1).strip())
    return out


def _sample(field: str) -> str:
    lowered = field.lower()
    for pattern, value in _SAMPLES:
        if re.search(pattern, lowered):
            return value
    return _DEFAULT_SAMPLE


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "feature"


def describe(plan: TestPlan) -> dict[str, Any]:
    """A short summary for the run log."""
    return {
        "features": len(plan.features),
        "scenarios": plan.scenario_count,
        "data_driven": sum(
            1 for spec in plan.features for scenario in spec.scenarios if scenario.data_driven
        ),
    }
