"""Skipping the design call when memory already answers the request.

The design call is the most expensive one in a run: it runs on the reasoning
tier and produces the largest completion. Skipping it on a repeat request is
the single biggest saving available — and the single most dangerous, because a
plan that silently omits a newly added criterion still looks complete.

So the tests here are mostly about *refusing* to skip.
"""

from __future__ import annotations

import pytest

from agents.test_design.agent import TestDesignAgent, _plan_from_knowledge
from packages.aiqa_types.models import AcceptanceCriterion, Requirement
from services.knowledge_service.test_knowledge import TestKnowledge, TestKnowledgeStore

CRITERIA = [
    "A valid resident can be created successfully",
    "Mandatory field validation is enforced on submit",
    "A duplicate resident is rejected with a clear message",
]


def _requirement(texts: list[str]) -> Requirement:
    return Requirement(
        raw_input="Automate resident registration",
        title="Resident Registration",
        summary="Manage residents",
        acceptance_criteria=[AcceptanceCriterion(text=t, testable=True) for t in texts],
    )


def _remember(store: TestKnowledgeStore, texts: list[str]) -> None:
    for index, text in enumerate(texts, start=1):
        store.put(
            TestKnowledge(
                test_id=f"TC-RR-{index:03d}",
                name=text,
                feature="Resident Registration",
                requirement="Resident Registration",
                tags=["@regression", "@P1"],
                page_objects=["ResidentRegistrationPage"],
                fixtures=["authenticatedPage"],
                steps=[
                    "Given I am logged in as a standard user",
                    f"When {text.lower()}",
                    "Then the outcome is confirmed",
                ],
            )
        )


def _prepared(agent_ctx, criteria: list[str], remembered: list[str]):
    store = TestKnowledgeStore(agent_ctx.project.id)
    _remember(store, remembered)
    agent_ctx.test_knowledge = store
    agent_ctx.requirement = _requirement(criteria)
    return TestDesignAgent(), agent_ctx


# --------------------------------------------------------------------------- #
# Refusing to skip
# --------------------------------------------------------------------------- #
def test_partial_cover_still_pays_for_design(agent_ctx) -> None:
    """The uncovered criterion is exactly the one that needs designing."""
    agent, ctx = _prepared(agent_ctx, CRITERIA + ["Only an administrator may delete a resident"], CRITERIA)
    assert agent._plan_from_memory(ctx) is None


def test_an_empty_suite_is_never_a_reason_to_skip(agent_ctx) -> None:
    agent, ctx = _prepared(agent_ctx, CRITERIA, [])
    assert agent._plan_from_memory(ctx) is None


def test_a_requirement_with_no_testable_criteria_is_designed(agent_ctx) -> None:
    agent, ctx = _prepared(agent_ctx, CRITERIA, CRITERIA)
    for criterion in ctx.requirement.acceptance_criteria:
        criterion.testable = False
    assert agent._plan_from_memory(ctx) is None


def test_the_policy_can_be_turned_off(agent_ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, ctx = _prepared(agent_ctx, CRITERIA, CRITERIA)
    monkeypatch.setattr(
        "agents.test_design.agent.load_model_config",
        lambda: {"reuse": {"skip_design": False}},
    )
    assert agent._plan_from_memory(ctx) is None


# --------------------------------------------------------------------------- #
# Skipping, and what the reused plan contains
# --------------------------------------------------------------------------- #
def test_full_cover_skips_the_design_call(agent_ctx) -> None:
    agent, ctx = _prepared(agent_ctx, CRITERIA, CRITERIA)
    plan = agent._plan_from_memory(ctx)

    assert plan is not None, "every criterion was already covered"
    assert ctx.metadata.get("design_reused") is True
    assert plan.scenario_count == len(CRITERIA)
    assert plan.page_objects_needed == [], "nothing new is needed when everything is reused"
    assert "ResidentRegistrationPage" in plan.page_objects_reused


def test_a_reused_plan_keeps_the_original_step_text(agent_ctx) -> None:
    agent, ctx = _prepared(agent_ctx, CRITERIA, CRITERIA)
    plan = agent._plan_from_memory(ctx)
    assert plan is not None

    steps = [step for feature in plan.features for s in feature.scenarios for step in s.steps]
    assert steps, "reconstruction produced no steps"
    assert steps[0].keyword == "Given"
    assert steps[0].text == "I am logged in as a standard user"
    assert {s.keyword for s in steps} <= {"Given", "When", "Then", "And", "But"}


def test_recorded_traceability_beats_text_similarity(agent_ctx) -> None:
    """A scenario that recorded its criterion covers it however it is named.

    Similarity against the name is only a fallback for tests remembered before
    traceability was stored; where the link is recorded there is no threshold
    to get wrong.
    """
    store = TestKnowledgeStore(agent_ctx.project.id)
    for index, text in enumerate(CRITERIA, start=1):
        store.put(
            TestKnowledge(
                test_id=f"TC-RR-{index:03d}",
                # A name that shares almost nothing with the criterion wording.
                name=f"Suite case {index}",
                feature="Resident Registration",
                tags=["@regression", "@P2"],
                steps=["Given a precondition", "When an action", "Then an outcome"],
                covers_criteria=[text],
            )
        )
    agent_ctx.test_knowledge = store
    agent_ctx.requirement = _requirement(CRITERIA)

    plan = TestDesignAgent()._plan_from_memory(agent_ctx)
    assert plan is not None
    assert plan.scenario_count == len(CRITERIA)


def test_reconstruction_skips_scenarios_it_cannot_rebuild() -> None:
    """A remembered entry with no steps cannot become a scenario."""
    class _Ctx:
        run_id = "run_x"
        requirement = None
        project = None

    empty = TestKnowledge(test_id="TC-1", name="Nameless", steps=[])
    assert _plan_from_knowledge([empty], _Ctx()) is None
