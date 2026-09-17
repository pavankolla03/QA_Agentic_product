"""A second run of the same request must not re-pay for the same design.

The unit tests in `test_design_reuse.py` cover the decision. This one covers the
plumbing around it, which is where the bug actually was: the offline provider
was handed a one-line summary of the prompt, so it never cited the acceptance
criteria, so nothing was ever recognised as already covered. Only an end-to-end
run showed that.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.aiqa_types.enums import RunMode, RunStatus
from packages.aiqa_types.models import RunRequest

pytestmark = pytest.mark.asyncio

INSTRUCTION = "Automate the Resident Registration functionality"


async def _run(engine, project, org_user) -> dict:
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(
            project_id=project.id,
            instruction=INSTRUCTION,
            mode=RunMode.FULL,
            auto_approve=True,
        ),
        user_id=user_id,
        org_id=org_id,
    )
    result = await engine.run_to_completion(run_id, auto_approve=True)
    # Nothing in this sandbox can verify automation: no npm, no browser, and no
    # reachable application. BLOCKED is the honest outcome, and pinning it here is
    # the point — this assertion read SUCCEEDED for a run that executed zero tests
    # and generated steps with nothing behind them.
    assert result.status is RunStatus.BLOCKED, result.error

    from services.observability.db import session_scope
    from services.observability.models import LLMCallRow

    with session_scope() as session:
        calls = list(
            session.execute(select(LLMCallRow).where(LLMCallRow.run_id == run_id)).scalars()
        )
    return {
        "design_calls": sum(1 for c in calls if (c.agent or "") == "test_design"),
        "input_tokens": sum(c.prompt_tokens for c in calls),
        "output_tokens": sum(c.completion_tokens for c in calls),
    }


async def test_the_second_identical_run_skips_the_design_call(engine, project, org_user) -> None:
    first = await _run(engine, project, org_user)
    assert first["design_calls"] == 1, "the first run has nothing to reuse"

    second = await _run(engine, project, org_user)
    assert second["design_calls"] == 0, "the design was already covered and should not be re-paid for"
    assert second["output_tokens"] < first["output_tokens"]


async def test_a_different_request_is_still_designed(engine, project, org_user) -> None:
    """Reuse must not leak across requirements."""
    await _run(engine, project, org_user)

    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(
            project_id=project.id,
            instruction="Automate the Invoice Approval workflow",
            mode=RunMode.FULL,
            auto_approve=True,
        ),
        user_id=user_id,
        org_id=org_id,
    )
    result = await engine.run_to_completion(run_id, auto_approve=True)
    # Nothing in this sandbox can verify automation: no npm, no browser, and no
    # reachable application. BLOCKED is the honest outcome, and pinning it here is
    # the point — this assertion read SUCCEEDED for a run that executed zero tests
    # and generated steps with nothing behind them.
    assert result.status is RunStatus.BLOCKED, result.error

    from services.observability.db import session_scope
    from services.observability.models import LLMCallRow

    with session_scope() as session:
        calls = list(
            session.execute(select(LLMCallRow).where(LLMCallRow.run_id == run_id)).scalars()
        )
    assert sum(1 for c in calls if (c.agent or "") == "test_design") == 1
