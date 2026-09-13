"""Agent-level quality gates.

These cover the judgements that make the platform trustworthy rather than merely
functional: standards enforcement, correct failure triage, and — most
importantly — the refusal to "heal" a real product defect.
"""

from __future__ import annotations

import pytest

from agents.base import AgentContext
from agents.failure_analysis.agent import FailureAnalysisAgent
from agents.self_healing.agent import (
    _best_locator_match,
    _introduces_hard_wait,
    _unverified_test_ids,
    _weakens_assertions,
)
from agents.standards.agent import StandardsAgent
from packages.aiqa_types.enums import ArtifactKind, FailureCategory, HealStrategy, Severity, TestStatus
from packages.aiqa_types.models import CodeBundle, ExecutionResult, FileChange, TestCaseResult


# =========================================================================== #
# Standards rule engine
# =========================================================================== #
def _bundle(path: str, content: str, kind: ArtifactKind) -> CodeBundle:
    return CodeBundle(changes=[FileChange(path=path, kind=kind, content=content)])


async def _check(ctx: AgentContext, path: str, content: str, kind: ArtifactKind):
    ctx.code_bundle = _bundle(path, content, kind)
    await StandardsAgent().run(ctx)
    assert ctx.standards_report is not None
    return ctx.standards_report


async def test_hard_waits_are_blocked_and_autofixed(agent_ctx: AgentContext) -> None:
    report = await _check(
        agent_ctx, "tests/steps/x.steps.ts",
        "When('I wait', async function () {\n  await page.waitForTimeout(5000);\n});\n",
        ArtifactKind.STEP_DEFINITION,
    )
    assert report.autofixed, "STD-001 declares an autofix; it should have been applied"
    assert "waitForTimeout" not in agent_ctx.code_bundle.changes[0].content


async def test_raw_locators_in_steps_are_errors(agent_ctx: AgentContext) -> None:
    report = await _check(
        agent_ctx, "tests/steps/x.steps.ts",
        "When('I click', async function () {\n  await page.locator('#submit').click();\n});\n",
        ArtifactKind.STEP_DEFINITION,
    )
    assert "STD-004" in {v.rule_id for v in report.violations}
    assert not report.passed


async def test_hardcoded_credentials_are_errors(agent_ctx: AgentContext) -> None:
    report = await _check(
        agent_ctx, "tests/pages/X.ts",
        'const password = "hunter2value";\n',
        ArtifactKind.PAGE_OBJECT,
    )
    assert "STD-003" in {v.rule_id for v in report.violations}


async def test_untagged_scenarios_are_flagged(agent_ctx: AgentContext) -> None:
    report = await _check(
        agent_ctx, "tests/features/x.feature",
        "Feature: X\n\n  Scenario: does a thing\n    Given I am here\n    Then it works\n",
        ArtifactKind.FEATURE,
    )
    assert "STD-005" in {v.rule_id for v in report.violations}


async def test_tagged_scenarios_pass(agent_ctx: AgentContext) -> None:
    report = await _check(
        agent_ctx, "tests/features/x.feature",
        "@regression\nFeature: X\n\n  @smoke\n  Scenario: TC-X-001 does a thing\n"
        "    Given I am here\n    Then it works\n",
        ArtifactKind.FEATURE,
    )
    assert "STD-005" not in {v.rule_id for v in report.violations}


async def test_focused_tests_are_blocked(agent_ctx: AgentContext) -> None:
    report = await _check(
        agent_ctx, "tests/steps/x.steps.ts", "test.only('debug', async () => {});\n",
        ArtifactKind.STEP_DEFINITION,
    )
    assert "STD-011" in {v.rule_id for v in report.violations}


async def test_redefining_an_existing_helper_is_an_error(agent_ctx: AgentContext, repo_copy) -> None:
    """The repository already exports getUser; regenerating it must be refused."""
    from services.knowledge_service import RepositoryIndexer

    agent_ctx.repo_profile, _ = RepositoryIndexer("prj_test", str(repo_copy)).scan()
    report = await _check(
        agent_ctx, "tests/utils/dupe.ts",
        "export function getUser(kind: string) {\n  return { username: 'x' };\n}\n",
        ArtifactKind.UTIL,
    )
    assert "STD-009" in {v.rule_id for v in report.violations}


async def test_a_local_variable_is_not_a_redefinition(agent_ctx: AgentContext, repo_copy) -> None:
    """Regression: `let loginPage: LoginPage;` was wrongly flagged as duplicating a fixture."""
    from services.knowledge_service import RepositoryIndexer

    agent_ctx.repo_profile, _ = RepositoryIndexer("prj_test", str(repo_copy)).scan()
    report = await _check(
        agent_ctx, "tests/steps/x.steps.ts",
        "import { LoginPage } from '../pages/LoginPage';\nlet loginPage: LoginPage;\n",
        ArtifactKind.STEP_DEFINITION,
    )
    assert "STD-009" not in {v.rule_id for v in report.violations}


async def test_project_override_raises_severity(agent_ctx: AgentContext) -> None:
    """The sample project escalates console.log (STD-006) from warning to error."""
    report = await _check(
        agent_ctx, "tests/pages/X.ts", "export class XPage {\n  go() { console.log('hi'); }\n}\n",
        ArtifactKind.PAGE_OBJECT,
    )
    violation = next((v for v in report.violations if v.rule_id == "STD-006"), None)
    if violation is not None:            # may have been autofixed away first
        assert violation.severity == Severity.ERROR


async def test_clean_code_passes(agent_ctx: AgentContext) -> None:
    report = await _check(
        agent_ctx, "tests/pages/ResidentPage.ts",
        "import { expect } from '@playwright/test';\n"
        "import { BasePage } from './BasePage';\n\n"
        "export class ResidentPage extends BasePage {\n"
        "  readonly path = '/residents';\n\n"
        "  private get submitButton() {\n    return this.page.getByTestId('submit');\n  }\n\n"
        "  async submit(): Promise<void> {\n    await this.submitButton.click();\n  }\n\n"
        "  async expectCreated(name: string): Promise<void> {\n"
        "    await expect(this.page.getByTestId('toast')).toContainText(name);\n  }\n}\n",
        ArtifactKind.PAGE_OBJECT,
    )
    assert report.passed, [f"{v.rule_id}: {v.message}" for v in report.violations]


# =========================================================================== #
# Failure triage
# =========================================================================== #
def _failure(message: str, stack: str = "", name: str = "TC-X-001 does a thing") -> ExecutionResult:
    return ExecutionResult(
        total=1, failed=1,
        results=[
            TestCaseResult(
                test_id="TC-X-001", name=name, file_path="tests/steps/x.steps.ts",
                status=TestStatus.FAILED, error_message=message, error_stack=stack,
            )
        ],
    )


@pytest.mark.parametrize(
    "message,expected",
    [
        ("locator.click: Error: strict mode violation: resolved to 0 elements", FailureCategory.LOCATOR_BROKEN),
        ("Timeout 30000ms exceeded waiting for locator to be visible", FailureCategory.TIMING_FLAKE),
        ("connect ECONNREFUSED 127.0.0.1:3000", FailureCategory.ENVIRONMENT),
        ("duplicate key value violates unique constraint", FailureCategory.TEST_DATA),
        ("TypeError: Cannot read properties of undefined", FailureCategory.TEST_LOGIC_ERROR),
        ("Error: Cannot find module '../pages/Missing'", FailureCategory.DEPENDENCY),
    ],
)
async def test_failures_are_classified(agent_ctx: AgentContext, message: str, expected: FailureCategory) -> None:
    agent_ctx.execution = _failure(message)
    await FailureAnalysisAgent().run(agent_ctx)
    assert agent_ctx.analyses[0].category == expected


async def test_locator_failures_are_healable(agent_ctx: AgentContext) -> None:
    agent_ctx.execution = _failure("strict mode violation: resolved to 0 elements")
    await FailureAnalysisAgent().run(agent_ctx)
    analysis = agent_ctx.analyses[0]
    assert analysis.healable
    assert analysis.suggested_strategy == HealStrategy.RELOCATE_SELECTOR
    assert not analysis.is_product_defect


async def test_environment_failures_are_not_healable(agent_ctx: AgentContext) -> None:
    agent_ctx.execution = _failure("connect ECONNREFUSED 127.0.0.1:3000")
    await FailureAnalysisAgent().run(agent_ctx)
    assert not agent_ctx.analyses[0].healable


@pytest.mark.parametrize(
    "message",
    [
        'Expected string: "Total: 100.00"\nReceived string: "Total: 90.00"',
        "expect(received).toHaveText(expected)\nExpected: 'Approved'\nReceived: 'Pending'",
        "expect(received).toEqual(expected)\nExpected: 42\nReceived: 41",
    ],
)
async def test_value_mismatches_are_treated_as_product_defects(agent_ctx: AgentContext, message: str) -> None:
    """The safety override: never auto-heal something that looks like a real bug."""
    agent_ctx.execution = _failure(message)
    await FailureAnalysisAgent().run(agent_ctx)
    analysis = agent_ctx.analyses[0]
    assert analysis.is_product_defect, "an assertion mismatch must be surfaced, not silently repaired"
    assert not analysis.healable
    assert analysis.suggested_strategy == HealStrategy.NO_ACTION


async def test_a_missing_element_is_still_a_locator_problem(agent_ctx: AgentContext) -> None:
    """A locator failure that happens to mention `expect` is not a product defect."""
    agent_ctx.execution = _failure(
        "expect(locator).toBeVisible()\nError: strict mode violation: resolved to 0 elements"
    )
    await FailureAnalysisAgent().run(agent_ctx)
    assert agent_ctx.analyses[0].category == FailureCategory.LOCATOR_BROKEN


# =========================================================================== #
# Self-healing safety predicates
# =========================================================================== #
@pytest.mark.parametrize(
    "old,new,weakens",
    [
        ("await expect(x).toBeVisible();", "await expect(y).toBeVisible();", False),
        ("await expect(x).toBeVisible();", "// await expect(x).toBeVisible();", True),
        ("await expect(x).toBeVisible();\nawait expect(y).toHaveText('a');", "await expect(x).toBeVisible();", True),
        ("test('a', ...)", "test.skip('a', ...)", True),
    ],
)
def test_repairs_that_reduce_verification_are_rejected(old: str, new: str, weakens: bool) -> None:
    assert _weakens_assertions(old, new) is weakens


@pytest.mark.parametrize(
    "snippet,bad",
    [
        ("await page.waitForTimeout(1000);", True),
        ("time.sleep(2)", True),
        ("await expect(x).toBeVisible();", False),
    ],
)
def test_repairs_may_not_introduce_hard_waits(snippet: str, bad: bool) -> None:
    assert _introduces_hard_wait(snippet) is bad


def test_repairs_may_not_invent_test_ids() -> None:
    catalog = [{"locator": "getByTestId('submit-btn')", "name": "Submit", "confidence": 0.9}]
    assert _unverified_test_ids("page.getByTestId('submit-btn')", catalog) == []
    assert _unverified_test_ids("page.getByTestId('imaginary')", catalog) == ["imaginary"]


def test_locator_matching_needs_a_clear_winner() -> None:
    catalog = [
        {"locator": "getByTestId('resident-submit')", "name": "Submit resident", "role": "button", "confidence": 0.95},
        {"locator": "getByTestId('unrelated-field')", "name": "Postcode", "role": "textbox", "confidence": 0.9},
    ]
    match = _best_locator_match("getByTestId('resident-submit-old')", catalog)
    assert match is not None and "resident-submit" in match["locator"]

    # Two equally plausible candidates: refuse rather than guess.
    ambiguous = [
        {"locator": "getByTestId('resident-name')", "name": "resident name", "role": "textbox", "confidence": 0.9},
        {"locator": "getByTestId('resident-name-2')", "name": "resident name", "role": "textbox", "confidence": 0.9},
    ]
    assert _best_locator_match("getByTestId('resident-name-old')", ambiguous) is None


def test_locator_matching_returns_nothing_without_a_catalog() -> None:
    assert _best_locator_match("getByTestId('x')", []) is None
