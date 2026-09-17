"""Steps that will not verify anything, found before anything claims to be done.

A run that writes a feature, a page object and a step file, compiles them all
and reports success is still not finished if three of its steps are
`return 'pending'`. The suite is green in the sense that nothing errored and red
in the sense that nothing was checked — the exact failure mode this platform
exists to refuse, and one it shipped with for weeks.

Two gaps, two instruments. Cucumber's own dry run knows which steps are
undefined; reading the generated source finds the ones that are defined and
empty. Neither asks a model anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.execution_service.gap_resolver import GapResolver, action_for, method_name_for
from services.execution_service.step_coverage import StepCoverage


# --------------------------------------------------------------------------- #
# Finding the gaps
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data: Any, error: str = "") -> None:
        self.data = data
        self.ok = not error
        self.error = error


class _Runner:
    """Writes the dry-run report the real cucumber would write."""

    def __init__(self, root: Path, report: list[dict] | None, exit_code: int = 0) -> None:
        self.root = root
        self.report = report
        self.exit_code = exit_code

    def run(self, command: list[str], **_: Any) -> _Result:
        if self.report is not None:
            path = self.root / ".aiqa" / "dryrun.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.report), encoding="utf-8")
        return _Result({"stdout": "", "stderr": "", "exit_code": self.exit_code})


class _DeadRunner:
    """What a failed spawn looks like: no data, just an error."""

    def run(self, **_: Any) -> _Result:
        return _Result(None, error="executable not found on PATH: npx")


def _project(tmp_path: Path, steps: str = "", feature: str = "") -> Path:
    root = tmp_path / "repo"
    (root / "tests" / "steps").mkdir(parents=True)
    (root / "tests" / "features").mkdir(parents=True)
    (root / "package.json").write_text("{}", encoding="utf-8")
    # A cucumber config, or coverage correctly declines to judge the features.
    (root / "cucumber.cjs").write_text("module.exports = {};", encoding="utf-8")
    if steps:
        (root / "tests" / "steps" / "login.steps.ts").write_text(steps, encoding="utf-8")
    if feature:
        (root / "tests" / "features" / "login.feature").write_text(feature, encoding="utf-8")
    return root


BOUND = """import { Given } from '@cucumber/cucumber';

Given('I am on the login page', async function () {
  await loginPage.goto();
});
"""

PENDING = """import { Given, When } from '@cucumber/cucumber';

Given('I am on the login page', async function () {
  await loginPage.goto();
});

When('I confirm the deletion', async function () {
  // TODO(aiqa): LoginPage has no confirmDeletion().
  return 'pending';
});
"""

UNDEFINED_REPORT = [
    {
        "uri": "tests/features/login.feature",
        "elements": [
            {
                "type": "scenario",
                "steps": [
                    {"keyword": "Given ", "name": "I am on the login page", "line": 3,
                     "result": {"status": "skipped"}},
                    {"keyword": "When ", "name": "I do something nobody implemented", "line": 4,
                     "result": {"status": "undefined"}},
                ],
            }
        ],
    }
]


def test_a_pending_body_is_a_gap(tmp_path: Path) -> None:
    """The renderer emits `return 'pending'` so this is findable before a run."""
    root = _project(tmp_path, steps=PENDING)
    report = StepCoverage(root, runner=_Runner(root, [])).scan()

    assert not report.clean
    assert report.verdict == "gaps"
    assert [gap.text for gap in report.pending] == ["I confirm the deletion"]
    assert report.pending[0].file_path.endswith("login.steps.ts")
    assert report.pending[0].line > 0, "a gap without a location is hard to act on"


def test_an_undefined_step_is_a_gap(tmp_path: Path) -> None:
    """Cucumber is the only thing that knows this for certain."""
    root = _project(tmp_path, steps=BOUND)
    report = StepCoverage(root, runner=_Runner(root, UNDEFINED_REPORT)).scan()

    assert not report.clean
    assert [gap.text for gap in report.undefined] == ["I do something nobody implemented"]


def test_a_fully_bound_suite_is_clean(tmp_path: Path) -> None:
    root = _project(tmp_path, steps=BOUND)
    report = StepCoverage(root, runner=_Runner(root, [])).scan()

    assert report.clean
    assert report.verdict == "covered"
    assert "every step is bound" in report.summary()


def test_a_dry_run_that_could_not_start_is_unverified(tmp_path: Path) -> None:
    """The lesson this codebase has had to learn in four separate places.

    Finding no gaps and being unable to look are not the same answer, and only
    one of them means the work is done.
    """
    root = _project(tmp_path, steps=BOUND)
    report = StepCoverage(root, runner=_DeadRunner()).scan()

    assert report.ran is False
    assert report.clean is False, "unverified is not covered"
    assert report.verdict == "unverified"
    assert "NOT checked" in report.summary()


def test_a_pending_body_counts_even_when_the_dry_run_fails(tmp_path: Path) -> None:
    """Evidence of a gap is still evidence, whatever else went wrong."""
    root = _project(tmp_path, steps=PENDING)
    report = StepCoverage(root, runner=_DeadRunner()).scan()

    assert report.ran is True
    assert report.clean is False
    assert report.pending, "the source scan costs nothing and answered the question"


def test_a_repository_with_no_cucumber_config_is_not_judged(tmp_path: Path) -> None:
    """Its features are not executed at all, which is a different problem."""
    root = _project(tmp_path, steps=BOUND)
    (root / "cucumber.cjs").unlink()
    report = StepCoverage(root, runner=_Runner(root, UNDEFINED_REPORT)).scan()

    assert report.ran is False
    assert "no cucumber configuration" in report.skipped_reason


# --------------------------------------------------------------------------- #
# Closing them — with evidence, or not at all
# --------------------------------------------------------------------------- #
CATALOG = [
    {"page": "/residents/new", "name": "Full name", "role": "textbox",
     "locator": "getByTestId('resident-name')", "confidence": 0.98},
    {"page": "/residents/new", "name": "Email", "role": "textbox",
     "locator": "getByTestId('resident-email')", "confidence": 0.98},
    {"page": "/residents/new", "name": "Create resident", "role": "button",
     "locator": "getByTestId('resident-submit')", "confidence": 0.98},
    {"page": "/residents/new", "name": "Send updates", "role": "checkbox",
     "locator": "getByTestId('resident-updates')", "confidence": 0.9},
]


def _resolver(**kwargs: Any) -> GapResolver:
    defaults: dict[str, Any] = {
        "catalog": CATALOG,
        "existing_steps": ["I am on the login page", "I sign in as a standard user"],
        "existing_methods": {"LoginPage": {"login", "expectLoginError"}},
    }
    return GapResolver(**{**defaults, **kwargs})


def test_an_already_implemented_step_is_reused_not_regenerated() -> None:
    resolution = _resolver().resolve("I am on the login page")
    assert resolution.strategy == "reuse_step"
    assert not resolution.method, "nothing needs generating"


def test_an_existing_method_is_preferred_over_a_new_one() -> None:
    """The capability exists; only the binding was missing."""
    resolution = _resolver().resolve("I expect a login error")
    assert resolution.strategy == "reuse_method"
    assert resolution.page_class == "LoginPage"
    assert resolution.method == "expectLoginError"


@pytest.mark.parametrize(
    ("step", "action", "locator"),
    [
        ("I enter the resident's email address", "fill", "getByTestId('resident-email')"),
        ("I fill in the full name", "fill", "getByTestId('resident-name')"),
        ("I click the Create resident button", "click", "getByTestId('resident-submit')"),
        ("I tick send updates", "check", "getByTestId('resident-updates')"),
    ],
)
def test_a_new_method_comes_from_an_observed_element(step: str, action: str, locator: str) -> None:
    """Never from the model's imagination — that is how a suite clicks a button
    that does not exist."""
    resolution = _resolver().resolve(step)
    assert resolution.strategy == "generate_method"
    assert resolution.action == action
    assert resolution.locator == locator
    assert resolution.confidence >= 0.6


def test_a_step_with_no_matching_element_is_blocked() -> None:
    """Declining to guess is the only useful thing available here."""
    resolution = _resolver().resolve("the accountant reconciles the quarterly ledger")
    assert resolution.strategy == "blocked"
    assert not resolution.resolved
    assert resolution.reason, "a block with no reason gives a human nothing to act on"


def test_a_role_that_cannot_do_the_action_is_not_matched() -> None:
    """You cannot fill a button, however similar the words are."""
    resolver = GapResolver(
        catalog=[{"page": "/x", "name": "Create resident", "role": "button",
                  "locator": "getByTestId('resident-submit')", "confidence": 0.98}]
    )
    assert resolver.resolve("I enter the create resident value").strategy == "blocked"


def test_matching_is_scoped_to_the_route_when_one_is_known() -> None:
    """The defect that put the login form's fields on a registration page."""
    resolver = GapResolver(
        catalog=CATALOG + [
            {"page": "/login", "name": "Full name", "role": "textbox",
             "locator": "getByTestId('login-username')", "confidence": 0.99}
        ],
        route="/residents/new",
    )
    assert resolver.resolve("I fill in the full name").locator == "getByTestId('resident-name')"


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ("I enter the resident's email address", "fillResidentEmailAddress"),
        ("I fill in the full name", "fillFullName"),
        ("I click the Create resident button", "clickCreateResident"),
        ("I should see the success message", "expectSuccessMessage"),
        ("I go to the dashboard", "goToDashboard"),
    ],
)
def test_generated_names_do_not_repeat_the_verb(step: str, expected: str) -> None:
    """`fillFillFullName` and `fillEnterResidentSEmail` are what happens when
    the prefix and the tail both carry the action."""
    assert method_name_for(step, action_for(step)) == expected
