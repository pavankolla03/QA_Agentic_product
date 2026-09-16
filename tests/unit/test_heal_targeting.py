"""Self-healing must not repair the wrong file.

The healer was handed a Cucumber failure whose `file_path` was the `.feature`
-- which is how Cucumber identifies a test -- and appended a TypeScript step
definition to the bottom of it. The proposed code was right; the file was not.
The feature then stopped parsing and took every scenario in the project with
it, which is a considerably worse outcome than the failing test it replaced.

Two defences: the failure is pointed at the step definition Cucumber itself
names, and the healer refuses to write code into Gherkin whatever it is told.
"""

from __future__ import annotations

from agents.self_healing.agent import _is_code
from packages.aiqa_types.models import TestCaseResult


def test_gherkin_prose_is_not_mistaken_for_code() -> None:
    for step in (
        "I am on the login page",
        "the dashboard is visible",
        "I should see the message Invalid username or password",
        "I deleted the record",
    ):
        assert _is_code(step) is False, step


def test_a_step_definition_is_recognised_as_code() -> None:
    for snippet in (
        "await page.reload({ waitUntil: 'networkidle' });",
        "When('I reload the page', async () => {})",
        "const user = getUser('standard');",
        "import { Given } from '@cucumber/cucumber';",
    ):
        assert _is_code(snippet) is True, snippet


def test_a_repair_targets_the_step_definition_not_the_feature() -> None:
    """Cucumber records where the matching step definition lives; use it."""
    from agents.failure_analysis.agent import _repairable_path

    bdd = TestCaseResult(
        name="TC-LOGI-001 session persists",
        file_path="tests/features/login.feature",
        code_path="tests/steps/login.steps.ts",
    )
    assert _repairable_path(bdd) == "tests/steps/login.steps.ts"

    # A Playwright spec is its own code, so nothing changes there.
    spec = TestCaseResult(name="login", file_path="tests/login.spec.ts")
    assert _repairable_path(spec) == "tests/login.spec.ts"


def test_the_step_definition_location_survives_the_report() -> None:
    from tools.playwright.pw_tools import parse_cucumber_report

    report = [
        {
            "uri": "tests/features/login.feature",
            "elements": [
                {
                    "type": "scenario",
                    "name": "TC-LOGI-001 reload",
                    "steps": [
                        {
                            "keyword": "When ",
                            "name": "I reload the page",
                            "match": {"location": "tests/steps/login.steps.ts:10"},
                            "result": {"status": "failed", "error_message": "boom", "duration": 1},
                        }
                    ],
                }
            ],
        }
    ]
    case = parse_cucumber_report(report).results[0]
    assert case.file_path == "tests/features/login.feature", "the test is still identified by its feature"
    assert case.code_path == "tests/steps/login.steps.ts", "but the repair goes to the code"
