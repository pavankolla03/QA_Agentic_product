"""The conversation plane: fast answers, from facts, with no model.

"How many tests failed?" has an exact answer sitting in a database row. Asking
a language model for it is slower, spends a request, and can be wrong — the one
question type where a model is strictly worse than a query. It used to go to a
model anyway, because the classifier could only decide *reply or run*, and
"reply" meant "ask the model something".

Three properties matter here and each has a test:

* a named intent is resolved without a model;
* a question the platform holds the answer to is answered from the database;
* a chat call can never inherit the automation policy — minutes of timeout and
  a retry budget — which is what made a greeting hang in the first place.
"""

from __future__ import annotations

import pytest

from packages.aiqa_types.enums import Capability, RunMode
from services.agent_engine.conversation import ConversationService
from services.agent_engine.intents import Intent, resolve


# --------------------------------------------------------------------------- #
# Intent resolution — deterministic, no model
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("Hi", Intent.CONVERSATION),
        ("thanks", Intent.CONVERSATION),
        ("what can you do", Intent.HELP),
        ("automate the login page", Intent.RUN_CREATE),
        ("run the tests", Intent.RUN_TESTS),
        ("run smoke tests", Intent.RUN_TESTS),
        ("heal the failing tests", Intent.RUN_HEAL),
        ("explore the application", Intent.RUN_EXPLORE),
        ("how many tests failed?", Intent.RUN_FAILURE_QUERY),
        ("why did the last run fail?", Intent.RUN_FAILURE_QUERY),
        ("is my test still running?", Intent.RUN_STATUS),
        ("what did today cost?", Intent.COST_QUERY),
        ("how much did I spend today", Intent.COST_QUERY),
        ("summarise the last run", Intent.RUN_REPORT),
        ("what project is this?", Intent.PROJECT_QUERY),
        ("which model are you using?", Intent.CONFIGURATION_QUERY),
        ("approve", Intent.APPROVAL_ACCEPT),
        ("reject", Intent.APPROVAL_REJECT),
    ],
)
def test_intents_resolve_without_a_model(message: str, intent: Intent) -> None:
    resolution = resolve(message)
    assert resolution.intent is intent, message
    assert resolution.confident, "a named intent must not need a model to confirm it"


def test_work_beats_a_question_when_a_message_is_both() -> None:
    """"Run the tests and tell me what failed" is work, not a question.

    Both patterns match. Resolving it as a query would answer about a previous
    run and never start the one being asked for.
    """
    assert resolve("run the tests and tell me what failed").intent is Intent.RUN_TESTS


@pytest.mark.parametrize(
    ("intent", "mode"),
    [
        (Intent.RUN_CREATE, RunMode.FULL),
        (Intent.RUN_TESTS, RunMode.EXECUTE_ONLY),
        (Intent.RUN_HEAL, RunMode.HEAL_ONLY),
        (Intent.RUN_EXPLORE, RunMode.PLAN_ONLY),
    ],
)
def test_each_work_intent_knows_its_mode(intent: Intent, mode: RunMode) -> None:
    from services.agent_engine.intents import Resolution

    assert Resolution(intent).mode is mode


def test_only_work_intents_start_runs() -> None:
    """The invariant that keeps "Hi" cheap."""
    for intent in Intent:
        if intent.starts_a_run:
            assert intent.name.startswith("RUN_"), intent
    assert not Intent.CONVERSATION.starts_a_run
    assert not Intent.HELP.starts_a_run
    assert not Intent.RUN_STATUS.starts_a_run, "asking about a run is not starting one"
    assert not Intent.UNKNOWN.starts_a_run, "when unsure, answer — never run"


def test_ambiguous_prose_defers_to_a_model_but_never_to_a_run() -> None:
    resolution = resolve("the dashboard")
    assert resolution.intent is Intent.UNKNOWN
    assert not resolution.confident
    assert not resolution.intent.starts_a_run


# --------------------------------------------------------------------------- #
# Deterministic answers
# --------------------------------------------------------------------------- #
def test_conversation_and_help_need_no_database(tmp_path) -> None:  # noqa: ANN001
    service = ConversationService()
    assert service.answer(resolve("hi")).text
    assert service.answer(resolve("hi")).suggestions, "offer somewhere to go next"
    assert "automate" in service.answer(resolve("what can you do")).text.lower()


def test_a_question_about_runs_is_answered_from_the_database(project) -> None:  # noqa: ANN001
    """No model is constructed anywhere in this path."""
    service = ConversationService(project_id=project.id)
    answer = service.answer(resolve("how many tests failed?"))
    assert answer is not None
    assert answer.text, "a question we hold the facts for must produce a sentence"


def test_a_project_with_no_runs_says_so_rather_than_inventing(project) -> None:  # noqa: ANN001
    service = ConversationService(project_id=project.id)
    assert "No runs yet" in service.answer(resolve("is it still running?")).text


def test_an_unknown_intent_has_no_deterministic_answer() -> None:
    """Returning None is how the caller knows to escalate to a model."""
    assert ConversationService().answer(resolve("the dashboard")) is None


def test_a_run_request_has_no_deterministic_answer() -> None:
    """Work is not answered; it is done."""
    assert ConversationService().answer(resolve("automate the login page")) is None


# --------------------------------------------------------------------------- #
# The policy that stopped the hanging
# --------------------------------------------------------------------------- #
def test_interactive_chat_has_its_own_policy() -> None:
    """A chat call must never inherit the automation settings.

    `defaults` are tuned for a background job — minutes of timeout, a retry
    budget, room to think at length. Applied to a message somebody is waiting
    on, those same settings are the reason a greeting could hang.
    """
    from services.model_router.router import ModelRouter

    policy = ModelRouter().policy_for(Capability.INTERACTIVE_CHAT)
    assert policy["timeout_seconds"] <= 5, "a person is waiting"
    assert policy["retries"] == 0, "a retry doubles the wait for someone already waiting"
    assert policy["max_output_tokens"] <= 500, "a chat reply is a few sentences"


def test_the_automation_tiers_keep_their_generous_policy() -> None:
    """The fix must not make code generation fail in five seconds."""
    from services.model_router.router import ModelRouter

    router = ModelRouter()
    for tier in (Capability.REASONING, Capability.CODING):
        policy = router.policy_for(tier)
        assert policy.get("timeout_seconds", router.default_timeout) > 30, tier


def test_one_router_is_reused_across_chat_messages() -> None:
    """The state that makes the *second* call fast lives on the router.

    Which providers answered, which are rate limited, which models are cooling
    off, and the HTTP connections themselves. Building one per message threw all
    of it away and re-probed provider health — up to eight seconds — before the
    model was asked anything.
    """
    from services.agent_engine.engine import AgentEngine

    engine = AgentEngine(offline=True)
    assert engine.chat_router is engine.chat_router


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
def test_every_chat_outcome_uses_the_same_frame_sequence() -> None:
    """One code path in the client, not three.

    A deterministic answer, a model-backed answer and a run request are three
    very different things; making the client branch on which it got would mean
    three places for the panel to get stuck.
    """
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "services" / "api_gateway" / "app.py"
    ).read_text(encoding="utf-8")

    stream = source[source.index('@api.post("/chat/stream"'):]
    stream = stream[: stream.index("\ndef _chat_context(")]

    for event in ("chat_started", "token", "chat_finished", "run_suggested"):
        assert f'"{event}"' in stream, f"{event} is never emitted"

    assert "media_type=\"text/event-stream\"" in stream
    assert "X-Accel-Buffering" in stream, (
        "a proxy that buffers the whole response defeats the point of streaming"
    )


def test_a_deterministic_answer_is_not_dripped_out() -> None:
    """It is already complete when produced; withholding it would be theatre."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "services" / "api_gateway" / "app.py"
    ).read_text(encoding="utf-8")
    stream = source[source.index('@api.post("/chat/stream"'):]
    stream = stream[: stream.index("\ndef _chat_context(")]

    deterministic = stream[stream.index("if answer is not None:"):]
    deterministic = deterministic[: deterministic.index("context = _chat_context(")]
    assert deterministic.count('frame("token"') == 1, "the whole answer, in one frame"


def test_streaming_never_switches_model_mid_answer() -> None:
    """Half a sentence from one model and half from another is worse than a
    short answer: the words already on screen would not match what follows."""
    router = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "services" / "model_router" / "router.py"
    ).read_text(encoding="utf-8")

    stream = router[router.index("    async def stream("):]
    stream = stream[: stream.index("\n    async def status(")]
    assert "if produced:" in stream and "return" in stream, (
        "a failure after the first token must stop, not restart elsewhere"
    )


def test_a_run_at_an_approval_gate_has_not_finished(project) -> None:  # noqa: ANN001
    """It said "It finished as waiting_approval", which is a contradiction.

    The person asked what had failed and was told the work was over, of a run
    that was sitting still waiting for them.
    """
    from packages.aiqa_types.enums import RunStatus
    from services.agent_engine.intents import Intent, Resolution
    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        session.add(
            RunRow(
                id="run_gate", project_id=project.id, instruction="http://app.test",
                mode="full", status=RunStatus.WAITING_APPROVAL.value,
                current_agent="test_design", tests_total=0,
            )
        )

    answer = ConversationService(project_id=project.id).answer(
        Resolution(Intent.RUN_FAILURE_QUERY, run_id="run_gate")
    )

    assert answer is not None
    assert "finished" not in answer.text
    assert "waiting for your approval" in answer.text


def test_a_running_run_says_what_stage_it_is_at(project) -> None:  # noqa: ANN001
    from packages.aiqa_types.enums import RunStatus
    from services.agent_engine.intents import Intent, Resolution
    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        session.add(
            RunRow(
                id="run_midway", project_id=project.id, instruction="http://app.test",
                mode="full", status=RunStatus.RUNNING.value,
                current_agent="exploration", tests_total=0,
            )
        )

    answer = ConversationService(project_id=project.id).answer(
        Resolution(Intent.RUN_FAILURE_QUERY, run_id="run_midway")
    )

    assert answer is not None
    assert "finished" not in answer.text
    assert "still exploration" in answer.text

