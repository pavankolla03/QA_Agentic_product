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

    Without a lazy construction the first step to touch a page dereferences an
    undeclared variable — code that compiles and dies on the first run.
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

    assert "dashboardPage ??= new DashboardPage(this.page);" in source
    assert source.index("??=") < source.index("openRegistration()")


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
