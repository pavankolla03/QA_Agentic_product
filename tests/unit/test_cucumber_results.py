"""Reading CucumberJS results without flattering them.

Playwright's runner collects `*.spec.ts`. Point it at a repository whose tests
are feature files and it finds nothing, exits 0, and reports a passing suite
that executed no scenario at all — the same shape of lie as a skipped compile
check reading as a pass.

Cucumber also reports per *step*, and a scenario is only as good as its worst
step. An `undefined` step is not a skip: it means the scenario never ran.
"""

from __future__ import annotations

from typing import Any

from packages.aiqa_types.enums import TestStatus
from tools.playwright.pw_tools import parse_cucumber_report


def _step(name: str, status: str, error: str = "", ns: int = 1_000_000) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "duration": ns}
    if error:
        result["error_message"] = error
    return {"keyword": "Given ", "name": name, "result": result}


def _feature(*elements: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"uri": "tests/features/login.feature", "name": "login", "elements": list(elements)}]


def test_a_scenario_takes_the_status_of_its_worst_step() -> None:
    report = _feature(
        {
            "type": "scenario",
            "name": "TC-LOGI-001 valid sign-in",
            "tags": [{"name": "@smoke"}],
            "steps": [
                _step("I am on the login page", "passed"),
                _step("I sign in", "failed", error="expected visible, got hidden"),
                _step("I see the dashboard", "skipped"),
            ],
        }
    )
    execution = parse_cucumber_report(report, exit_code=1)

    assert execution.total == 1
    assert execution.failed == 1 and execution.passed == 0
    case = execution.results[0]
    assert case.status == TestStatus.FAILED
    assert case.test_id == "TC-LOGI-001"
    assert "expected visible" in case.error_message
    assert case.failed_step == "Given I sign in"
    assert case.tags == ["@smoke"]


def test_an_undefined_step_fails_the_scenario_and_says_which() -> None:
    """The defect that made this matter.

    Cucumber attaches no error message to an undefined step, so treating it as
    a skip produced a scenario that neither passed nor failed and carried no
    explanation — nothing for the failure analyst to work from, and a suite
    that looked mostly fine while implementing nothing.
    """
    report = _feature(
        {
            "type": "scenario",
            "name": "TC-LOGI-002 missing step",
            "steps": [
                _step("I am on the login page", "undefined"),
                _step("I sign in", "skipped"),
            ],
        }
    )
    execution = parse_cucumber_report(report)

    assert execution.failed == 1, "an unimplemented step is a failure, not a skip"
    assert "undefined step" in execution.results[0].error_message
    assert "I am on the login page" in execution.results[0].error_message


def test_background_is_not_counted_as_a_scenario() -> None:
    report = _feature(
        {"type": "background", "name": "", "steps": [_step("I am on the login page", "passed")]},
        {"type": "scenario", "name": "real one", "steps": [_step("something", "passed")]},
    )
    execution = parse_cucumber_report(report)
    assert execution.total == 1
    assert execution.results[0].name == "real one"


def test_durations_come_back_in_milliseconds() -> None:
    report = _feature(
        {
            "type": "scenario",
            "name": "timed",
            "steps": [_step("a", "passed", ns=1_500_000_000), _step("b", "passed", ns=500_000_000)],
        }
    )
    execution = parse_cucumber_report(report)
    assert execution.results[0].duration_ms == 2000
    assert execution.duration_ms == 2000


def test_an_empty_report_is_not_a_passing_suite() -> None:
    execution = parse_cucumber_report([], exit_code=0)
    assert execution.total == 0 and execution.passed == 0
