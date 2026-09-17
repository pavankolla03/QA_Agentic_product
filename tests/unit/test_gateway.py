"""One brain, many channels.

Slack, Teams, WhatsApp, a voice call, a CI job and the VS Code panel all reach
the same understanding of what a message means. That is the point of the
gateway: six approximations of "is this a greeting or a request to automate an
application?" would agree in week one and disagree by week four, and the
disagreements would be found by someone whose run did not start.

An adapter's whole job is the two translations at the edge. It never decides
anything, and these tests are mostly about holding that line — plus the two
places where the gateway refuses to be helpful, because being helpful there
means guessing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from packages.aiqa_types.enums import RunMode
from services.gateway.adapters import (
    from_ci,
    from_slack,
    from_teams,
    from_voice,
    from_whatsapp,
    to_ci,
    to_slack,
    to_voice,
)
from services.gateway.envelope import Channel, CommandEnvelope, Reply
from services.gateway.gateway import Gateway
from services.gateway.sessions import Session


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class _Engine:
    def __init__(self, answer: str = "") -> None:
        self.requests: list[Any] = []
        self.started: list[str] = []
        self._answer = answer

    def create_run(self, request: Any, user_id: str = "", org_id: str = "") -> str:
        self.requests.append(request)
        return "run_abc123"

    async def start(self, run_id: str) -> None:
        self.started.append(run_id)

    async def answer(self, text: str, context: str = "") -> str:
        return self._answer


class _Sessions:
    """A session store with no database behind it."""

    def __init__(self, project_id: str = "prj_1", only: str = "") -> None:
        self.project_id = project_id
        self.only = only
        self.bound: list[tuple[str, str]] = []
        self.runs: list[tuple[str, str]] = []

    def resolve(self, envelope: CommandEnvelope) -> Session:
        return Session(
            id="chs_1",
            key=envelope.session_key,
            channel=envelope.channel,
            external_user_id=envelope.external_user_id,
            project_id=envelope.project_id or self.project_id,
            org_id=envelope.org_id,
        )

    def only_project(self, org_id: str = "") -> str:
        return self.only

    def bind_project(self, key: str, project_id: str) -> None:
        self.bound.append((key, project_id))

    def remember_run(self, key: str, run_id: str) -> None:
        self.runs.append((key, run_id))


def _send(text: str, channel: Channel = Channel.SLACK, **kwargs: Any) -> tuple[Reply, _Engine]:
    engine = _Engine(**{k: v for k, v in kwargs.items() if k == "answer"})
    sessions = _Sessions(**{k: v for k, v in kwargs.items() if k in ("project_id", "only")})
    envelope = CommandEnvelope(
        channel=channel, text=text, external_user_id="U1", conversation_id="C1"
    )
    reply = asyncio.run(Gateway(engine, sessions=sessions).handle(envelope))
    return reply, engine


# --------------------------------------------------------------------------- #
# The same message means the same thing everywhere
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "channel", [Channel.SLACK, Channel.TEAMS, Channel.WHATSAPP, Channel.VOICE, Channel.CI]
)
def test_a_url_starts_an_autopilot_run_on_every_channel(channel: Channel) -> None:
    reply, engine = _send("https://shop.example.com", channel)

    assert reply.kind == "run"
    assert reply.run_id == "run_abc123"
    assert reply.autopilot
    assert engine.requests[0].target_url == "https://shop.example.com"
    assert engine.requests[0].mode is RunMode.FULL


@pytest.mark.parametrize("channel", [Channel.SLACK, Channel.WHATSAPP, Channel.VOICE])
def test_a_greeting_never_starts_a_run_on_any_channel(channel: Channel) -> None:
    reply, engine = _send("hi", channel)

    assert reply.kind == "reply"
    assert engine.requests == []
    assert reply.text


def test_a_question_is_answered_from_records_not_from_a_model() -> None:
    """The deterministic path has to work channel-side too.

    An engine that would raise if asked proves no model was consulted.
    """

    class _Refuses(_Engine):
        async def answer(self, text: str, context: str = "") -> str:
            raise AssertionError("a model was consulted for a question with a factual answer")

    envelope = CommandEnvelope(channel=Channel.SLACK, text="what did today cost?", conversation_id="C1")
    reply = asyncio.run(Gateway(_Refuses(), sessions=_Sessions()).handle(envelope))

    assert reply.kind == "reply"
    assert reply.text


# --------------------------------------------------------------------------- #
# Where the gateway refuses to guess
# --------------------------------------------------------------------------- #
def test_an_unbound_conversation_is_asked_rather_than_guessed_at() -> None:
    """Starting the wrong run is not a smaller error than starting none.

    With several projects registered and none named, picking the most recent
    would write files into a repository nobody mentioned, and the person would
    find out from the diff.
    """
    reply, engine = _send("automate the checkout page", project_id="", only="")

    assert engine.requests == []
    assert reply.error == "no project"
    assert "which project" in reply.text


def test_a_single_project_installation_needs_no_ceremony() -> None:
    """When there is only one answer, asking the question is just friction."""
    sessions = _Sessions(project_id="", only="prj_only")
    engine = _Engine()
    envelope = CommandEnvelope(channel=Channel.SLACK, text="automate the login page", conversation_id="C1")

    reply = asyncio.run(Gateway(engine, sessions=sessions).handle(envelope))

    assert reply.kind == "run"
    assert engine.requests[0].project_id == "prj_only"
    # And the conversation remembers, so the next message needs no lookup.
    assert sessions.bound == [("slack:C1", "prj_only")]


def test_a_pipeline_gets_an_error_it_can_act_on_rather_than_prose() -> None:
    """A CI job asking something unparseable does not want a paragraph.

    Nor does it want a model call: nobody is reading the answer, and the run
    that was meant to start has not.
    """
    reply, engine = _send("zxcvbn qwerty", Channel.CI, answer="Here is a friendly essay.")

    assert not reply.ok
    assert reply.error == "not understood"
    assert engine.requests == []


# --------------------------------------------------------------------------- #
# Fitting the answer to the channel
# --------------------------------------------------------------------------- #
def test_a_spoken_reply_carries_nothing_unspeakable() -> None:
    """There is no scrollback in a phone call.

    A run id, a file path or a table read aloud is noise that cannot be
    re-read, so the structure goes and the sentences stay.
    """
    reply, _ = _send("https://shop.example.com", Channel.VOICE)

    assert "run_abc123" not in reply.text
    assert "\n" not in reply.text
    assert reply.suggestions == []


def test_a_typed_channel_keeps_the_detail() -> None:
    reply, _ = _send("https://shop.example.com", Channel.SLACK)

    assert "run_abc123" in reply.text
    assert "shop.example.com" in reply.text


# --------------------------------------------------------------------------- #
# Adapters translate, and do nothing else
# --------------------------------------------------------------------------- #
def test_a_mention_is_addressing_and_not_instruction() -> None:
    """"<@U0BOT> automate https://x" asks for one thing, not two.

    Left in, the mention looks like part of the message, and a message with an
    extra token in it is no longer "just a URL".
    """
    envelope = from_slack(
        {"event": {"text": "<@U0BOT> automate https://shop.test", "user": "U1", "thread_ts": "9.9"}}
    )
    assert envelope.text == "automate https://shop.test"
    assert envelope.session_key == "slack:9.9"


def test_teams_strips_its_own_mention_markup() -> None:
    envelope = from_teams(
        {
            "text": "<at>QAgentic</at> how many tests failed?",
            "from": {"id": "29:abc"},
            "conversation": {"id": "19:thread"},
        }
    )
    assert envelope.text == "how many tests failed?"
    assert envelope.session_key == "teams:19:thread"


def test_whatsapp_is_read_from_either_provider() -> None:
    """Which provider an installation uses is not this platform's decision."""
    cloud = from_whatsapp(
        {"entry": [{"changes": [{"value": {"messages": [{"from": "447700900000",
                                                         "text": {"body": "status?"}}]}}]}]}
    )
    twilio = from_whatsapp({"From": "whatsapp:+447700900000", "Body": "status?"})

    assert cloud.text == twilio.text == "status?"
    assert cloud.metadata["provider"] == "cloud_api"
    assert twilio.metadata["provider"] == "twilio"


def test_a_status_callback_is_not_mistaken_for_a_message() -> None:
    """WhatsApp sends delivery receipts through the same webhook."""
    envelope = from_whatsapp({"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]})
    assert envelope.text == ""


def test_voice_keeps_its_transcription_confidence() -> None:
    envelope = from_voice({"transcript": "run the tests", "call_id": "c9", "confidence": 0.62})
    assert envelope.metadata["confidence"] == 0.62


def test_ci_carries_the_commit_it_is_testing() -> None:
    envelope = from_ci(
        {"instruction": "run the tests", "project_id": "prj_1", "commit": "abc123", "branch": "main"}
    )
    assert envelope.project_id == "prj_1"
    assert envelope.metadata["commit"] == "abc123"


def test_a_session_key_cannot_collide_across_channels() -> None:
    """A Slack thread id and a WhatsApp chat id may be the same string.

    A collision here would hand one person another person's project binding,
    which is why the channel is part of the key rather than a field beside it.
    """
    slack = CommandEnvelope(channel=Channel.SLACK, text="x", conversation_id="12345")
    whatsapp = CommandEnvelope(channel=Channel.WHATSAPP, text="x", conversation_id="12345")

    assert slack.session_key != whatsapp.session_key


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_slack_gets_blocks_and_a_plain_text_fallback() -> None:
    rendered = to_slack(Reply(text="Run finished.", suggestions=["what failed?"]))

    assert rendered["text"] == "Run finished."
    assert rendered["blocks"][0]["text"]["text"] == "Run finished."
    assert "what failed?" in rendered["blocks"][1]["elements"][0]["text"]


def test_ci_gets_something_to_branch_on() -> None:
    rendered = to_ci(Reply(kind="run", run_id="run_1", text="Starting."))

    assert rendered["ok"] is True
    assert rendered["run_id"] == "run_1"


def test_voice_always_has_something_to_say() -> None:
    assert to_voice(Reply(text=""))["speech"]


# --------------------------------------------------------------------------- #
# "Starting" has to mean starting
# --------------------------------------------------------------------------- #
def test_a_reply_that_says_starting_means_the_run_started() -> None:
    """Creating a run only writes the row.

    The gateway said "Run X is starting" and never called `start`, so a run
    kicked off from Slack sat in `queued` indefinitely while the person who
    asked for it believed it was underway. A claim about work that is not
    happening is the one thing this platform must never make — and the first
    version of this test could not catch it, because the engine double had no
    `start` to leave uncalled.
    """
    reply, engine = _send("https://shop.example.com")

    assert reply.kind == "run"
    assert engine.started == ["run_abc123"]


def test_a_run_that_could_not_be_started_says_so() -> None:
    """Half-done is reported as half-done, with the id, so it can be resumed."""

    class _WontStart(_Engine):
        async def start(self, run_id: str) -> None:
            raise RuntimeError("the queue is unreachable")

    envelope = CommandEnvelope(channel=Channel.SLACK, text="https://shop.test", conversation_id="C1")
    reply = asyncio.run(Gateway(_WontStart(), sessions=_Sessions()).handle(envelope))

    assert not reply.ok
    assert reply.error == "not started"
    assert reply.run_id == "run_abc123"
    assert "queue is unreachable" in reply.text

