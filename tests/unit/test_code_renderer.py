"""The plan-then-render code generator.

Code generation is the one place where a mistake ships into the customer's
repository as a file that compiles, reads plausibly and fails at runtime. These
tests pin the properties that keep the renderer honest:

* a Page Object models exactly one route, so its locators cannot be a mixture
  of the login form and the page under test;
* every element is driven by the interaction its ARIA role supports;
* a step never references an identifier it does not declare.

Each of these was a real defect caught by inspecting generated output rather
than by a failing test, which is why they are pinned here.
"""

from __future__ import annotations

import re

from agents.code_generation.renderer import (
    GenerationPlan,
    MethodPlan,
    PagePlan,
    StepPlan,
    deterministic_plan,
    enrich_plan,
    parameterise,
    plan_from_response,
    render_page_object,
    render_steps,
    resolve_keywords,
)


# --------------------------------------------------------------------------- #
# A catalogue shaped exactly like ApplicationMap.catalog() output.
# --------------------------------------------------------------------------- #
def _entry(page: str, name: str, role: str, locator: str, required: bool = False) -> dict:
    return {
        "page": page,
        "name": name,
        "role": role,
        "required": required,
        "locator": locator,
        "strategy": "getByTestId",
        "confidence": 0.98,
    }


CATALOG = [
    _entry("/", "Username", "textbox", "getByTestId('login-username')", True),
    _entry("/", "Password", "textbox", "getByTestId('login-password')", True),
    _entry("/", "Sign in", "button", "getByTestId('login-submit')"),
    _entry("/login", "Username", "textbox", "getByTestId('login-username')", True),
    _entry("/login", "Sign in", "button", "getByTestId('login-submit')"),
    _entry("/residents", "Search residents", "textbox", "getByTestId('resident-search')"),
    _entry("/residents", "Search", "button", "getByTestId('resident-search-submit')"),
    _entry("/residents/new", "Full name", "textbox", "getByTestId('resident-name')", True),
    _entry("/residents/new", "Email", "textbox", "getByTestId('resident-email')", True),
    _entry("/residents/new", "Resident type", "combobox", "getByTestId('resident-type')"),
    _entry("/residents/new", "Send updates", "checkbox", "getByTestId('resident-updates')"),
    _entry("/residents/new", "Create resident", "button", "getByTestId('resident-submit')"),
]

SCENARIOS = [
    {
        "name": "Register a new resident with valid details",
        "steps": [
            {"keyword": "Given", "text": "I am on the registration page"},
            {"keyword": "When", "text": "I complete the resident registration form with valid details"},
            {"keyword": "And", "text": "I submit the form"},
            {"keyword": "Then", "text": "a success confirmation is displayed"},
            {"keyword": "And", "text": "the record appears in the results list"},
        ],
    }
]


def _bare_plan() -> GenerationPlan:
    """What a model returns when it names methods but binds nothing."""
    return GenerationPlan(
        pages=[
            PagePlan(
                class_name="ResidentRegistrationPage",
                methods=[
                    MethodPlan(name="fillForm", kind="action", params=["value"],
                               intent="Complete the form."),
                    MethodPlan(name="submit", kind="action", intent="Submit the form."),
                    MethodPlan(name="expectSuccess", kind="assertion", params=["message"],
                               expect="toContainText"),
                ],
            )
        ]
    )


# --------------------------------------------------------------------------- #
# Route scoping
# --------------------------------------------------------------------------- #
def test_enrichment_binds_only_the_pages_own_route() -> None:
    """A registration page must not pick up the login form's fields."""
    plan = enrich_plan(_bare_plan(), CATALOG, SCENARIOS, base_class="BasePage")
    page = plan.pages[0]

    assert page.route == "/residents/new"
    assert page.locators, "the catalogue was non-empty, so locators must be bound"
    assert all("resident-" in expression for expression in page.locators.values()), page.locators


def test_route_follows_the_locators_not_the_declared_route() -> None:
    """A route asserted without any element on it is not evidence."""
    raw = {
        "pages": [
            {
                "class": "ResidentRegistrationPage",
                "route": "/",                      # wrong, and contradicted below
                "locators": [
                    {"prop": "fullName", "locator": "getByTestId('resident-name')"},
                    {"prop": "email", "locator": "getByTestId('resident-email')"},
                ],
                "methods": [{"name": "fillForm", "kind": "action",
                             "params": ["fullName", "email"],
                             "locators": ["fullName", "email"]}],
            }
        ]
    }
    plan = plan_from_response(raw, catalog=CATALOG, default_route="/")
    plan = enrich_plan(plan, CATALOG, SCENARIOS)
    assert plan.pages[0].route == "/residents/new"


def test_duplicate_catalogue_entries_render_once() -> None:
    """The same element captured on two routes is one getter, not two."""
    plan = GenerationPlan(pages=[PagePlan(class_name="LoginPage")])
    # Force the login route, where Username/Sign in appear under both "/" and "/login".
    plan.pages[0].route = "/"
    enriched = enrich_plan(plan, CATALOG, [], base_class="BasePage")
    expressions = list(enriched.pages[0].locators.values())
    assert len(expressions) == len(set(expressions)), expressions
    assert not any(re.search(r"\d$", prop) for prop in enriched.pages[0].locators)


# --------------------------------------------------------------------------- #
# Role-aware interactions
# --------------------------------------------------------------------------- #
def test_each_role_gets_the_interaction_it_supports() -> None:
    plan = enrich_plan(_bare_plan(), CATALOG, SCENARIOS, base_class="BasePage")
    source = render_page_object(plan.pages[0])

    assert ".selectOption(residentType)" in source, "a <select> cannot be filled"
    assert ".setChecked(" in source, "a checkbox does not take a string"
    assert ".fill(fullName)" in source
    assert ".click();" in source, "the submit button is clicked"
    assert ".fill(residentType)" not in source


def test_a_fill_method_never_silently_becomes_a_click() -> None:
    """Params and locators are paired positionally, so counts must match."""
    plan = enrich_plan(_bare_plan(), CATALOG, SCENARIOS, base_class="BasePage")
    fill = next(m for m in plan.pages[0].methods if m.name == "fillForm")
    assert len(fill.params) == len(fill.locators) > 1


# --------------------------------------------------------------------------- #
# Gherkin binding
# --------------------------------------------------------------------------- #
def test_and_inherits_the_keyword_above_it() -> None:
    steps = resolve_keywords(
        [
            StepPlan(text="a precondition", keyword="Given"),
            StepPlan(text="an action", keyword="When"),
            StepPlan(text="another action", keyword="And"),
            StepPlan(text="an outcome", keyword="Then"),
            StepPlan(text="another outcome", keyword="And"),
        ]
    )
    assert [s.keyword for s in steps] == ["Given", "When", "When", "Then", "Then"]


def test_a_quoted_placeholder_is_one_parameter() -> None:
    pattern, params = parameterise('I enter "<value>" into the "<field>" field')
    assert pattern.count("{string}") == len(params) == 2
    assert params == ["value", "field"]


def test_steps_never_reference_an_undeclared_identifier() -> None:
    """The invariant that makes generated steps compile.

    Binding a method that needs a value the step cannot supply used to fall
    back to the *planned* argument name, producing a reference to something
    that does not exist in the callback's scope.
    """
    plan = enrich_plan(_bare_plan(), CATALOG, SCENARIOS, base_class="BasePage")
    source = render_steps(plan.steps, [p.class_name for p in plan.pages])

    signature_re = re.compile(r"async function \(([^)]*)\)")
    declared: set[str] = set()
    for line in source.splitlines():
        match = signature_re.search(line)
        if match:
            declared = {
                part.split(":")[0].strip() for part in match.group(1).split(",") if part.strip()
            }
            continue
        call = re.match(r"\s*await \w+\.\w+\(([^)]*)\);", line)
        if call and call.group(1).strip():
            for argument in (a.strip() for a in call.group(1).split(",")):
                if re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", argument):
                    assert argument in declared, f"{argument!r} is not declared: {line}"


def test_an_unbindable_step_is_a_comment_not_broken_code() -> None:
    steps = [StepPlan(text="a success confirmation is displayed", keyword="Then",
                      page="ResidentRegistrationPage", call="expectSuccess(message)")]
    source = render_steps(steps, ["ResidentRegistrationPage"])
    assert "TODO(aiqa): supply message" in source
    executable = [line for line in source.splitlines() if not line.strip().startswith("//")]
    assert not any("expectSuccess" in line for line in executable), executable


# --------------------------------------------------------------------------- #
# Hallucination resistance
# --------------------------------------------------------------------------- #
def test_locators_outside_the_catalogue_are_dropped() -> None:
    raw = {
        "pages": [
            {
                "class": "ResidentRegistrationPage",
                "route": "/residents/new",
                "locators": [
                    {"prop": "fullName", "locator": "getByTestId('resident-name')"},
                    {"prop": "invented", "locator": "getByTestId('does-not-exist')"},
                ],
                "methods": [{"name": "fillForm", "kind": "action",
                             "params": ["fullName", "invented"],
                             "locators": ["fullName", "invented"]}],
            }
        ]
    }
    plan = plan_from_response(raw, catalog=CATALOG, default_route="/")
    page = plan.pages[0]
    assert "invented" not in page.locators
    assert "does-not-exist" not in render_page_object(page)


def test_deterministic_plan_needs_no_model() -> None:
    """The whole catalogue goes in, because that is what the caller passes.

    An earlier version of this test hand-filtered the catalogue to one route and
    so assumed away the bug: when a model's plan was truncated and this fallback
    ran for real, it emitted a registration page carrying the login form's
    fields, twice each.
    """
    plan = deterministic_plan(
        "ResidentRegistrationPage",
        CATALOG,
        SCENARIOS,
        base_class="BasePage",
        route="/residents/new",
    )
    page = plan.pages[0]
    source = render_page_object(page)

    assert "export class ResidentRegistrationPage extends BasePage" in source
    assert ".selectOption(" in source
    assert plan.steps and all(s.page == "ResidentRegistrationPage" for s in plan.steps)

    assert all("resident-" in expr for expr in page.locators.values()), page.locators
    assert "login-username" not in source, "login fields belong to the login page"
    expressions = list(page.locators.values())
    assert len(expressions) == len(set(expressions)), "the same element was rendered twice"


def test_the_fallback_finds_the_route_when_given_a_wrong_one() -> None:
    """A defaulted route must not drag in another page's elements."""
    plan = deterministic_plan("ResidentRegistrationPage", CATALOG, SCENARIOS, route="/")
    assert plan.pages[0].route == "/residents/new"
    assert all("resident-" in expr for expr in plan.pages[0].locators.values())


def test_a_fill_step_never_binds_to_submit() -> None:
    """Code that compiles, runs, and tests the wrong thing is the worst outcome.

    The model-free fallback used to map every `When` to `submit()`, so
    "I fill in valid resident details" clicked the submit button and filled
    nothing. Caught by reading a live run's output, not by a failing test.
    """
    scenarios = [
        {
            "name": "Happy path",
            "steps": [
                {"keyword": "Given", "text": "I am on the Resident Registration page"},
                {"keyword": "When", "text": "I fill in valid resident details"},
                {"keyword": "When", "text": "I submit the form"},
            ],
        }
    ]
    plan = deterministic_plan("ResidentRegistrationPage", CATALOG, scenarios, route="/residents/new")
    by_text = {s.text: s.call for s in plan.steps}

    assert by_text["I fill in valid resident details"].startswith("fillForm(")
    assert by_text["I submit the form"] == "submit()"


def test_a_step_binds_to_the_methods_real_signature() -> None:
    """Generated code that does not compile is the worst possible output.

    A plan can write `submitEmail()` for a method declared
    `submitEmail(email: string)`. Binding from the planned call string alone
    emitted the zero-argument version — a TypeScript error, inside a step that
    declared the argument and never passed it.
    """
    page = PagePlan(
        class_name="ResidentRegistrationPage",
        route="/residents/new",
        locators={"email": "getByTestId('resident-email')"},
        methods=[MethodPlan(name="submitEmail", kind="action", params=["email"], locators=["email"])],
    )
    steps = [
        StepPlan(
            text='I enter "<value>" in the email field and submit the form',
            keyword="When",
            page="ResidentRegistrationPage",
            call="submitEmail()",          # the plan omitted the argument
        )
    ]
    source = render_steps(steps, [page])

    assert "await residentRegistrationPage.submitEmail(value);" in source
    assert "submitEmail();" not in source


def test_a_zero_argument_method_is_not_handed_arguments() -> None:
    page = PagePlan(
        class_name="ResidentRegistrationPage",
        locators={"createResident": "getByTestId('resident-submit')"},
        methods=[MethodPlan(name="submit", kind="action", locators=["createResident"])],
    )
    steps = [
        StepPlan(text='I submit "<thing>"', keyword="When",
                 page="ResidentRegistrationPage", call="submit(thing)")
    ]
    source = render_steps(steps, [page])
    assert "await residentRegistrationPage.submit();" in source


def test_a_step_short_of_arguments_is_left_for_a_human() -> None:
    page = PagePlan(
        class_name="ResidentRegistrationPage",
        locators={"a": "x", "b": "y"},
        methods=[MethodPlan(name="fillForm", kind="action", params=["a", "b"], locators=["a", "b"])],
    )
    steps = [
        StepPlan(text='I enter "<only-one>"', keyword="When",
                 page="ResidentRegistrationPage", call="fillForm()")
    ]
    source = render_steps(steps, [page])
    assert "TODO(aiqa): supply a, b" in source
    executable = [line for line in source.splitlines() if not line.strip().startswith("//")]
    assert not any("fillForm" in line for line in executable)


def test_declared_parameters_match_the_patterns_placeholders() -> None:
    """The pattern is the contract Cucumber calls against.

    A model often writes a Cucumber expression directly — "with dateOfBirth
    {string}" — rather than a quoted value. Nothing is substituted, so no
    parameter was recorded, and Cucumber passed an argument to a callback
    declared with none.
    """
    pattern, params = parameterise("I submit resident details with dateOfBirth {string}")
    assert pattern.count("{string}") == len(params) == 1


def test_a_step_written_as_an_expression_declares_its_argument() -> None:
    steps = [
        StepPlan(text="I submit details with dateOfBirth {string}", keyword="When",
                 page="ResidentRegistrationPage", call="submitDob(dob)")
    ]
    page = PagePlan(
        class_name="ResidentRegistrationPage",
        locators={"dob": "getByTestId('dob')"},
        methods=[MethodPlan(name="submitDob", kind="action", params=["dob"], locators=["dob"])],
    )
    source = render_steps(steps, [page])
    assert "async function (value1: string)" in source
    assert "submitDob(value1)" in source


def test_an_unbindable_call_is_shown_as_something_callable() -> None:
    steps = [
        StepPlan(text="I do a thing", keyword="When",
                 page="P", call="needsTwo")     # no parentheses at all
    ]
    page = PagePlan(
        class_name="P",
        locators={"a": "x", "b": "y"},
        methods=[MethodPlan(name="needsTwo", kind="action", params=["a", "b"], locators=["a", "b"])],
    )
    source = render_steps(steps, [page])
    assert "// await p.needsTwo(a, b);" in source


def test_a_page_is_constructed_before_it_is_used() -> None:
    """A scenario can start on a `When`, so setup may never have run.

    Without constructing it here, the first step to touch a page dereferences
    an undeclared variable — code that compiles and dies on the first run.

    Assigned rather than `??=`. The variable lives at module scope and outlives
    the scenario that set it, so `??=` keeps a Page Object pointing at a browser
    context the previous scenario's teardown already closed. That surfaced as
    `locator.fill: Target page, context or browser has been closed` in a
    scenario that had never touched the page it was complaining about.
    """
    page = PagePlan(
        class_name="DashboardPage",
        locators={"link": "getByTestId('go')"},
        methods=[MethodPlan(name="openRegistration", kind="action", locators=["link"])],
    )
    steps = [
        StepPlan(text="I navigate to the registration page", keyword="When",
                 page="DashboardPage", call="openRegistration()")
    ]
    source = render_steps(steps, [page])

    assert "dashboardPage = new DashboardPage(this.page);" in source
    assert "??=" not in source
    assert source.index("new DashboardPage") < source.index("openRegistration()")


def test_a_setup_step_still_constructs_eagerly_and_navigates() -> None:
    page = PagePlan(class_name="LoginPage", methods=[])
    steps = [StepPlan(text="I am on the login page", keyword="Given",
                      page="LoginPage", setup=True)]
    source = render_steps(steps, [page])
    assert "loginPage = new LoginPage(this.page);" in source
    assert "await loginPage.goto();" in source
    assert "??=" not in source


def test_a_page_without_a_base_class_provides_its_own_goto() -> None:
    """The step renderer calls `goto()` on every page it sets up.

    With a BasePage that is inherited. In a repository that has none — the
    from-scratch case — nothing supplied it, so the generated step called a
    method that did not exist.
    """
    page = PagePlan(
        class_name="LoginPage",
        route="/login",
        base_class="",
        locators={"username": "getByTestId('u')"},
        methods=[MethodPlan(name="login", kind="action", params=["username"], locators=["username"])],
    )
    source = render_page_object(page)

    assert "async goto(): Promise<void> {" in source
    assert "await this.page.goto(this.path);" in source

    steps = render_steps(
        [StepPlan(text="I am on the login page", keyword="Given", page="LoginPage", setup=True)],
        [page],
    )
    called = "await loginPage.goto();" in steps
    assert called and "async goto()" in source, "the step calls it, so the class must declare it"


def test_a_page_with_a_base_class_does_not_redeclare_goto() -> None:
    page = PagePlan(class_name="LoginPage", base_class="BasePage", methods=[])
    assert "async goto()" not in render_page_object(page)


def test_a_method_invented_on_a_reused_page_is_not_called() -> None:
    """The class is real; the method is not. Only `tsc` ever caught this.

    A plan may reuse an existing `DashboardPage` and invent
    `goToResidentRegistrationPage()` on it. The import resolves, the class
    exists, and the call fails to compile.
    """
    steps = [
        StepPlan(
            text="I navigate to the registration page",
            keyword="When",
            page="DashboardPage",
            call="goToResidentRegistrationPage()",
        )
    ]
    source = render_steps(
        steps,
        ["DashboardPage"],
        existing_members={"DashboardPage": {"expectLoaded", "openSection"}},
    )

    assert "DashboardPage has no goToResidentRegistrationPage()" in source
    assert "Its methods are: expectLoaded, openSection" in source
    executable = [line for line in source.splitlines() if not line.strip().startswith("//")]
    assert not any("goToResidentRegistrationPage" in line for line in executable)


def test_a_real_method_on_a_reused_page_is_still_called() -> None:
    steps = [
        StepPlan(text="the dashboard is loaded", keyword="Then",
                 page="DashboardPage", call="expectLoaded()")
    ]
    source = render_steps(
        steps, ["DashboardPage"], existing_members={"DashboardPage": {"expectLoaded"}}
    )
    assert "await dashboardPage.expectLoaded();" in source


def test_page_variables_are_definitely_assigned() -> None:
    """Under `strict`, a plain `let x: T;` fails TS2454 in every step file."""
    page = PagePlan(class_name="LoginPage", methods=[])
    source = render_steps(
        [StepPlan(text="I am on login", keyword="Given", page="LoginPage", setup=True)], [page]
    )
    assert "let loginPage!: LoginPage;" in source


# --------------------------------------------------------------------------- #
# Found by running the pipeline against a real application
# --------------------------------------------------------------------------- #
def test_an_unrecognised_when_step_binds_to_nothing() -> None:
    """A wrong call is worse than no call.

    The fallback ended `if fill ... elif submit: call = submit()`, so every
    `When` it did not recognise clicked the submit button. A live run turned
    "I set the email/username field to ''" into `submit()` — four times in one
    generated file. It compiled, and the standards agent passed it, because
    nothing about it is malformed; it simply tests something nobody asked for.
    """
    scenarios = [
        {
            "id": "TC-1",
            "name": "field validation",
            "steps": [
                {"keyword": "Given", "text": "I am on the registration page"},
                {"keyword": "When", "text": "I wait for the spinner to disappear"},
                {"keyword": "When", "text": "I click the Save button"},
            ],
        }
    ]
    plan = deterministic_plan("ResidentRegistrationPage", CATALOG, scenarios, route="/residents/new")
    by_text = {s.text: s for s in plan.steps}

    assert by_text["I wait for the spinner to disappear"].call == "", (
        "an unrecognised step must not be bound to whatever action happens to exist"
    )
    assert by_text["I click the Save button"].call.startswith("submit("), (
        "a step that really is a submit should still bind"
    )

    source = render_steps(plan.steps, plan.pages)
    assert "TODO(aiqa)" in source, "the unbound step must be visible as a gap"


def test_background_steps_get_definitions() -> None:
    """Otherwise the suite cannot run at all.

    `Background:` lives on the feature, not the scenario, and the step
    generator only ever saw `scenario.steps`. A feature whose Background said
    "Given I am on the login page" therefore generated no `Given` anywhere.
    Cucumber reports that step undefined and every scenario fails before its
    first assertion — past the compile gate, past standards, all the way to the
    first real execution.
    """
    scenarios = [
        {
            "id": "TC-1",
            "name": "valid registration",
            # As the agent now assembles them: background first, then the body.
            "steps": [
                {"keyword": "Given", "text": "I am on the registration page"},
                {"keyword": "When", "text": "I complete the resident details"},
            ],
        }
    ]
    plan = deterministic_plan("ResidentRegistrationPage", CATALOG, scenarios, route="/residents/new")
    source = render_steps(plan.steps, plan.pages)

    assert "Given('I am on the registration page'" in source, source
    assert "await residentRegistrationPage.goto();" in source, (
        "the background step has to actually navigate somewhere"
    )


def test_prose_is_not_read_as_cucumber_syntax() -> None:
    """A parse error here stops the whole suite, not one test.

    Cucumber Expressions give `(...)` and `/` their own meaning: an optional
    and an alternation. A generated step read "... all required fields (Full
    Name, Date of Birth, ID/Document Number) are valid" and CucumberJS refused
    to load *any* step file — "an alternation can not be used inside an
    optional" — so every feature in the project failed before its first
    scenario.

    Nothing upstream could see it. The TypeScript compiles, the Gherkin parses,
    the standards pass. It only appears when a runner reads the pattern, which
    is why it survived until the generated tests were executed for the first
    time.
    """
    pattern, params = parameterise(
        "all required fields (Full Name, Date of Birth, ID/Document Number) are valid"
    )
    assert r"\(" in pattern and r"\/" in pattern and r"\)" in pattern, pattern
    assert not params

    # Our own placeholders must survive the escaping.
    pattern, params = parameterise('I set the "email/username" field to "<value>"')
    assert pattern.count("{string}") == 2, pattern
    assert len(params) == 2

    # A backslash in prose is itself an escape character to Cucumber.
    pattern, _ = parameterise(r"the export lands in C:\reports")
    assert r"C:\\reports" in pattern, pattern


def test_an_unfinished_step_is_pending_not_passing() -> None:
    """A step body that is only a comment reports as a pass.

    It does nothing, and doing nothing is indistinguishable from succeeding:
    Cucumber marks it passed, and the assertions after it pass too because the
    browser is still wherever the previous step left it. A generated suite
    reported "3 scenarios (3 passed)" with one step that had never been
    written — green, from a file that openly said TODO.
    """
    steps = [
        StepPlan(
            text="I reload the page",
            keyword="When",
            page="DashboardPage",
            call="reloadPage()",
        )
    ]
    source = render_steps(
        steps, ["DashboardPage"], existing_members={"DashboardPage": {"expectLoaded"}}
    )
    body = source[source.index("I reload the page") :]
    assert "return 'pending';" in body, "an unimplemented step must not report as a pass"


def test_an_unbound_step_is_pending_too() -> None:
    steps = [StepPlan(text="something happens", keyword="When", page="LoginPage", call="")]
    source = render_steps(steps, ["LoginPage"])
    assert "return 'pending';" in source


def test_a_bound_step_is_not_pending() -> None:
    page = PagePlan(
        class_name="LoginPage",
        base_class="BasePage",
        locators={"username": "getByTestId('login-username')"},
        methods=[MethodPlan(name="submit", kind="action", locators=["username"])],
    )
    steps = [StepPlan(text="I submit the form", keyword="When", page="LoginPage", call="submit()")]
    source = render_steps(steps, [page])
    assert "await loginPage.submit();" in source
    assert "return 'pending';" not in source


def test_a_step_file_imports_only_what_it_uses() -> None:
    """Dead code in a file the platform tells people to trust.

    Every step file imported `Given, When, Then` whatever it contained, and
    declared a variable for every page the *plan* mentioned rather than every
    page its own steps touch. One generated file imported `LoginPage` and
    declared `loginPage` without referring to either.
    """
    steps = [StepPlan(text="I reload the page", keyword="When", page="DashboardPage", call="reloadPage()")]
    source = render_steps(
        steps,
        ["DashboardPage", "LoginPage"],
        existing_members={"DashboardPage": {"expectLoaded"}},
    )

    assert "import { When } from" in source, source
    assert "Given" not in source and "Then" not in source
    assert "LoginPage" not in source, "no step in this file mentions it"
    assert "let dashboardPage!: DashboardPage;" in source


def test_every_declared_page_is_referenced() -> None:
    """The general property, not the one instance of it."""
    steps = [
        StepPlan(text="I am on the login page", keyword="Given", page="LoginPage", call="", setup=True),
        StepPlan(text="I submit", keyword="When", page="LoginPage", call="submit()"),
    ]
    page = PagePlan(
        class_name="LoginPage",
        base_class="BasePage",
        locators={"submit": "getByTestId('login-submit')"},
        methods=[MethodPlan(name="submit", kind="action", locators=["submit"])],
    )
    source = render_steps(steps, [page, "DashboardPage"])
    for line in source.splitlines():
        if line.startswith("let "):
            name = line[4:].split("!")[0]
            body = source[source.index("(", source.index("Given(")) :]
            assert name in body, f"{name} is declared and never used"


def test_a_locator_and_a_method_cannot_share_a_name() -> None:
    """TS2300, and a method body that calls itself.

    `private get searchResidents()` beside `async searchResidents(...)` is a
    duplicate identifier — they share one namespace in a TypeScript class — and
    `this.searchResidents.fill(...)` inside the method resolves to the method.
    Both names are perfectly reasonable in isolation, which is exactly why a
    plan produced them.
    """
    plan = PagePlan(
        class_name="ResidentsPage",
        base_class="BasePage",
        route="/residents",
        locators={
            "searchResidents": "getByTestId('resident-search')",
            "searchSubmit": "getByTestId('resident-search-submit')",
        },
        roles={"searchResidents": "textbox", "searchSubmit": "button"},
        methods=[
            MethodPlan(
                name="searchResidents", kind="action", params=["term"],
                locators=["searchResidents", "searchSubmit"],
            )
        ],
    )
    source = render_page_object(plan)

    assert "private get searchResidentsField()" in source, source
    assert "async searchResidents(term: string)" in source, "the method keeps the name"
    assert "this.searchResidentsField.fill(term);" in source, "and the body follows the rename"
    assert source.count("searchResidents(") == 1, "only the method declares that name"


def test_a_plan_without_collisions_is_left_alone() -> None:
    plan = PagePlan(
        class_name="LoginPage",
        base_class="BasePage",
        locators={"username": "getByTestId('login-username')"},
        roles={"username": "textbox"},
        methods=[MethodPlan(name="signIn", kind="action", params=["user"], locators=["username"])],
    )
    source = render_page_object(plan)
    assert "private get username()" in source
    assert "usernameField" not in source


def test_an_unregistered_parameter_type_becomes_a_string() -> None:
    """Cucumber knows four parameter types; `{field}` is not one of them.

    A model writing `{field}` means "a value goes here", not "look up the
    parameter type named field". Cucumber answers by simply not matching the
    step, so a whole Scenario Outline came back `undefined` with nothing said
    about why.
    """
    pattern, params = parameterise("I submit the form with an empty {field}")
    assert pattern == "I submit the form with an empty {string}"
    assert params == ["field"], "the name is worth keeping — it reads better than value1"

    pattern, params = parameterise("I set {field} to {value}")
    assert pattern == "I set {string} to {string}"
    assert params == ["field", "value"]


def test_the_built_in_parameter_types_are_left_alone() -> None:
    for text, expected in (
        ("I see {int} results", "I see {int} results"),
        ("I wait {float} seconds", "I wait {float} seconds"),
        ("I type {word} here", "I type {word} here"),
        ("I type {string} here", "I type {string} here"),
    ):
        pattern, params = parameterise(text)
        assert pattern == expected
        assert len(params) == 1, "a placeholder still declares an argument"


# --------------------------------------------------------------------------- #
# One definition per expression
# --------------------------------------------------------------------------- #
def test_a_step_shared_by_two_scenarios_is_defined_once() -> None:
    """Cucumber matches by expression across the whole suite.

    Defining "I am on the residents form" twice is an ambiguity error that
    fails every scenario in the run, not only the two that share the step — and
    two scenarios opening on the same page is the normal case. A real run wrote
    it twice and the suite could not execute at all.
    """
    steps = [
        StepPlan(text="I am on the residents form", keyword="Given", page="ResidentsPage", setup=True),
        StepPlan(text="I submit the form", keyword="When", page="ResidentsPage", call="submitForm()"),
        StepPlan(text="I am on the residents form", keyword="Given", page="ResidentsPage", setup=True),
        StepPlan(text="I leave a field empty", keyword="When", page="ResidentsPage", call="clearField()"),
    ]
    source = render_steps(steps, ["ResidentsPage"])

    assert source.count("Given('I am on the residents form'") == 1
    assert "When('I submit the form'" in source
    assert "When('I leave a field empty'" in source


def test_the_duplicate_that_survives_is_the_one_that_does_something() -> None:
    """Keeping the first would be a coin toss.

    A plan routinely yields the same step twice: once bound to a Page Object
    method and once as an unresolved placeholder. Taking the placeholder throws
    away a working binding for nothing.
    """
    steps = [
        StepPlan(text="I submit the form", keyword="When"),
        StepPlan(text="I submit the form", keyword="When", page="ResidentsPage", call="submitForm()"),
    ]
    source = render_steps(steps, ["ResidentsPage"])

    assert source.count("When('I submit the form'") == 1
    assert "residentsPage.submitForm()" in source


def test_steps_differing_only_in_their_argument_are_one_expression() -> None:
    """`{string}` makes them the same Cucumber expression, whatever the example."""
    steps = [
        StepPlan(text='I submit with "name" left empty', keyword="When",
                 page="ResidentsPage", call="clear(value1)"),
        StepPlan(text='I submit with "email" left empty', keyword="When",
                 page="ResidentsPage", call="clear(value1)"),
    ]
    source = render_steps(steps, ["ResidentsPage"])

    assert source.count("When('I submit with {string} left empty'") == 1


# --------------------------------------------------------------------------- #
# Parameter names that compile
# --------------------------------------------------------------------------- #
def test_a_parameter_is_never_called_string() -> None:
    """A plan from "fill the form with {string}" names every parameter "string".

    Rendered literally that is `fillForm(string: string, string: string, ...)`:
    the parameter shadows the type in its own annotation and then shadows
    itself, which TypeScript rejects once per placeholder. A real run produced
    exactly that, and a page object that does not compile takes the whole suite
    with it.
    """
    plan = PagePlan(
        class_name="ResidentsPage",
        route="/residents/new",
        locators={"fullName": "getByTestId('name')", "email": "getByTestId('email')"},
        methods=[
            MethodPlan(name="fillForm", kind="action",
                       params=["string", "string", "string"], locators=["fullName", "email"])
        ],
    )
    source = render_page_object(plan)

    assert "async fillForm(value1: string, value2: string, value3: string)" in source
    assert "string: string" not in source


def test_two_parameters_never_share_a_name() -> None:
    plan = PagePlan(
        class_name="ResidentsPage",
        route="/r",
        locators={"a": "getByTestId('a')"},
        methods=[MethodPlan(name="check", kind="assertion", params=["value", "value", "value"])],
    )
    source = render_page_object(plan)

    signature = next(line for line in source.splitlines() if "async check(" in line)
    names = [part.split(":")[0].strip() for part in signature.split("(")[1].split(")")[0].split(",")]
    assert len(set(names)) == len(names), signature


def test_a_meaningful_parameter_name_is_kept() -> None:
    """Only the unusable ones are renamed. `email` reads better than `value1`."""
    plan = PagePlan(
        class_name="ResidentsPage",
        route="/r",
        locators={"email": "getByTestId('email')"},
        methods=[MethodPlan(name="fillEmail", kind="action", params=["email"], locators=["email"])],
    )
    assert "async fillEmail(email: string)" in render_page_object(plan)

