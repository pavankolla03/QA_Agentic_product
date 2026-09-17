"""What a finished run is allowed to call itself.

`status` is the one word everything downstream reads: the dashboard colours it,
CI gates on it, the chat answers "did it work?" from it, and none of them open
the report. So it has to be the honest word.

It was not. The rule was "SUCCEEDED if a report was written, else FAILED",
which made *producing a report* the definition of success. A real autopilot run
against the demo application ended as `succeeded` having written a report whose
own headline was AUTOMATION_BLOCKED, with four compile errors in the generated
TypeScript, eight steps that did nothing, and zero tests executed. Writing a
document about not finishing is not finishing.

BLOCKED is the missing word. FAILED still means the platform itself broke;
SUCCEEDED still covers a suite that ran and reported genuine failures, because
going red for a real defect is the job.
"""

from __future__ import annotations

import pytest

from agents.base import AgentContext
from agents.orchestrator.graph import terminal_status
from packages.aiqa_types.enums import RunMode, RunStatus
from packages.aiqa_types.models import ExecutionResult, Project, TestCaseResult


def _ctx(**metadata: object) -> AgentContext:
    ctx = AgentContext(
        run_id="r",
        project=Project(org_id="o", name="p", repository_path="."),
        instruction="automate the login page",
        mode=RunMode.FULL,
    )
    ctx.metadata.update(metadata)
    return ctx


def _execution(total: int = 0, passed: int = 0, failed: int = 0) -> ExecutionResult:
    return ExecutionResult(
        run_id="r",
        total=total,
        passed=passed,
        failed=failed,
        results=[
            TestCaseResult(name=f"t{i}", status="failed" if i < failed else "passed")
            for i in range(total)
        ],
    )


# --------------------------------------------------------------------------- #
def test_a_suite_that_ran_and_passed_succeeded() -> None:
    ctx = _ctx()
    ctx.execution = _execution(total=6, passed=6)
    assert terminal_status(ctx) is RunStatus.SUCCEEDED


def test_a_suite_that_ran_and_found_real_failures_still_succeeded() -> None:
    """Going red for a genuine defect is the job, not a failure of the platform."""
    ctx = _ctx()
    ctx.execution = _execution(total=6, passed=4, failed=2)
    assert terminal_status(ctx) is RunStatus.SUCCEEDED


def test_execution_that_could_not_run_is_blocked() -> None:
    ctx = _ctx(execution_blocked="Playwright is not installed in this project.")
    ctx.execution = _execution()
    assert terminal_status(ctx) is RunStatus.BLOCKED


def test_steps_with_nothing_behind_them_are_blocked() -> None:
    """A step that compiles and does nothing is worse than a missing step.

    The suite goes green while verifying nothing, which is the single outcome
    this platform exists to prevent.
    """
    ctx = _ctx(automation_blocked=['"I should see a success message" - no verified element'])
    ctx.execution = _execution(total=3, passed=3)
    assert terminal_status(ctx) is RunStatus.BLOCKED


def test_code_that_does_not_compile_is_blocked() -> None:
    ctx = _ctx(compile_check={"verdict": "failed", "ran": True, "errors": 4, "skipped": {}})
    ctx.execution = _execution(total=2, passed=2)
    assert terminal_status(ctx) is RunStatus.BLOCKED


def test_an_execution_that_produced_no_test_at_all_is_blocked() -> None:
    """The runner exiting cleanly with an empty report is not a pass.

    Nothing else catches this: there are no failures to count, no block was
    recorded, and the compile check may well have been fine.
    """
    ctx = _ctx()
    ctx.execution = _execution(total=0)
    assert terminal_status(ctx) is RunStatus.BLOCKED


def test_a_run_that_never_reaches_execution_is_judged_on_what_it_did() -> None:
    """plan_only writes no code and runs nothing, and succeeds at planning.

    Blocking it would make the mode permanently incapable of a good outcome.
    """
    ctx = _ctx()
    assert ctx.execution is None
    assert terminal_status(ctx) is RunStatus.SUCCEEDED


@pytest.mark.parametrize(
    "status",
    [RunStatus.SUCCEEDED, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.CANCELLED,
     RunStatus.BUDGET_EXCEEDED],
)
def test_every_ending_is_terminal(status: RunStatus) -> None:
    """A run left off this list is a run the reconciler never cleans up."""
    assert status.terminal


def test_only_success_counts_as_verified_work() -> None:
    assert RunStatus.SUCCEEDED.verified_work
    assert not RunStatus.BLOCKED.verified_work
    assert not RunStatus.FAILED.verified_work


def test_both_orchestrators_decide_this_the_same_way() -> None:
    """The two graphs must not drift, so there is exactly one rule.

    They are meant to be behaviourally identical. A verdict computed in two
    places is a verdict that will eventually be computed two ways — which is
    how it got wrong in the first place: the built-in graph and the LangGraph
    one each had their own copy, and they had already diverged (one demanded a
    report to avoid FAILED, the other only when something had failed).
    """
    from agents.orchestrator import graph, langgraph_graph

    assert langgraph_graph.terminal_status is graph.terminal_status


# --------------------------------------------------------------------------- #
# The word that reaches a Slack channel
# --------------------------------------------------------------------------- #
def test_a_blocked_run_does_not_announce_itself_as_succeeded() -> None:
    """Nobody scanning a channel opens the report to check a green tick.

    The notification read "failed if any test failed, else partial if there were
    product defects, else succeeded" — so a run that compiled nothing, bound no
    steps and executed no tests announced **succeeded**, because zero tests
    failed. The outbound message is the one most people ever see.
    """
    from agents.reporting.agent import _notification_status

    ctx = _ctx(execution_blocked="Playwright is not installed in this project.")
    ctx.execution = _execution()
    facts = {"failed": 0, "product_defects": []}

    assert _notification_status(ctx, facts) == "blocked"


def test_a_suite_with_real_failures_still_announces_failed() -> None:
    from agents.reporting.agent import _notification_status

    ctx = _ctx()
    ctx.execution = _execution(total=5, passed=3, failed=2)

    assert _notification_status(ctx, {"failed": 2, "product_defects": []}) == "failed"


def test_a_green_run_announces_succeeded() -> None:
    from agents.reporting.agent import _notification_status

    ctx = _ctx()
    ctx.execution = _execution(total=5, passed=5)

    assert _notification_status(ctx, {"failed": 0, "product_defects": []}) == "succeeded"

