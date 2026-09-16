"""Not every message is a job.

Every message typed into the chat became a run: requirement analysis,
repository indexing, a crawl, test design, code generation, the compile gate,
execution. For "Hi" that is ten agents and several minutes of free models, and
the answer it produces is nothing at all — which is indistinguishable from a
chat that does not work, and is precisely how it was reported.

The classifier is deliberately asymmetric. When it cannot tell, it replies
rather than starting a run: a wrong sentence costs a sentence, a wrong run
costs five minutes and writes files into somebody's repository.
"""

from __future__ import annotations

import pytest

from packages.aiqa_types.enums import RunMode
from services.agent_engine.intent import classify


@pytest.mark.parametrize(
    "message",
    ["Hi", "hi", "hello", "Hello!", "hey", "yo", "good morning", "namaste", "  hi  "],
)
def test_a_greeting_is_answered_instantly(message: str) -> None:
    """The bug, in its original form."""
    intent = classify(message)
    assert intent.kind == "reply"
    assert intent.confident, "no model should be consulted about the word 'hi'"
    assert intent.text, "and it must actually say something"


@pytest.mark.parametrize("message", ["thanks", "thank you", "ok", "cool", "bye", "good night"])
def test_conversational_filler_never_starts_a_run(message: str) -> None:
    intent = classify(message)
    assert intent.kind == "reply" and intent.confident


@pytest.mark.parametrize(
    "message",
    ["what can you do?", "help", "how do I get started", "what is this", "commands"],
)
def test_asking_about_the_tool_is_answered_from_a_fixed_string(message: str) -> None:
    """A question with one true answer does not need a model to produce it."""
    intent = classify(message)
    assert intent.kind == "reply" and intent.confident
    assert "automate" in intent.text.lower()


@pytest.mark.parametrize(
    ("message", "mode"),
    [
        ("automate the login page", RunMode.FULL),
        ("Automate the resident registration form: valid submission", RunMode.FULL),
        ("cover the checkout flow", RunMode.FULL),
        ("write tests for the search screen", RunMode.FULL),
        ("generate scenarios for the reports page", RunMode.FULL),
        ("run the tests", RunMode.EXECUTE_ONLY),
        ("run tests and tell me what broke", RunMode.EXECUTE_ONLY),
        ("heal the failing tests", RunMode.HEAL_ONLY),
        ("explore the application", RunMode.PLAN_ONLY),
    ],
)
def test_a_request_for_work_becomes_a_run(message: str, mode: RunMode) -> None:
    intent = classify(message)
    assert intent.kind == "run", message
    assert intent.mode == mode


@pytest.mark.parametrize("message", ["the dashboard", "is the application reachable?", "why did that fail"])
def test_an_ambiguous_message_defers_rather_than_running(message: str) -> None:
    """A model gets asked. Crucially, a *run* does not get started."""
    intent = classify(message)
    assert intent.kind == "reply"
    assert not intent.confident, "the caller should ask a model before answering"


def test_an_empty_message_says_something_useful() -> None:
    intent = classify("   ")
    assert intent.kind == "reply" and intent.text


def test_a_greeting_offers_somewhere_to_go_next() -> None:
    """A reply that ends the conversation is only half an answer."""
    intent = classify("hi")
    assert intent.suggestions, "say hello, then show what to say next"
