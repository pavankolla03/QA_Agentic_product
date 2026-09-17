"""What did the person actually ask for?

Every message typed into the chat was turned into a full run: requirement
analysis, repository indexing, a crawl, test design, code generation, the
compile gate, execution. For "Hi" that is ten agents, several minutes of free
models, and no answer — which is exactly what a broken chat looks like.

Most messages can be answered without a model at all. A greeting is a greeting;
"what can you do" has a fixed answer; "automate the login page" is obviously a
run. Only genuinely ambiguous messages are worth asking a model about, and even
then it is one short cheap call, not a pipeline.

The classifier is deliberately conservative in one direction: when in doubt it
replies rather than starting a run. A wrong reply costs a sentence. A wrong run
costs five minutes and writes files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.aiqa_types.enums import RunMode


@dataclass
class Intent:
    """What to do with one chat message."""

    kind: str                      # "reply" | "run"
    text: str = ""                 # the answer, when kind == "reply"
    mode: RunMode = RunMode.FULL   # how far to go, when kind == "run"
    confident: bool = True         # False means a model should double-check
    suggestions: list[str] = field(default_factory=list)


#: Openers that are never a request to do anything.
_GREETINGS = {
    "hi", "hii", "hiii", "hey", "heya", "hello", "hallo", "yo", "sup",
    "good morning", "good afternoon", "good evening", "morning", "evening",
    "namaste", "hola", "howdy",
}
_THANKS = {"thanks", "thank you", "thanks!", "ty", "thx", "cheers", "nice", "great", "cool", "ok", "okay", "k"}
_FAREWELL = {"bye", "goodbye", "see you", "later", "good night", "gn"}

#: Words that mean "do the work". One of these plus a noun is a run.
_ACTION_VERBS = (
    "automate", "generate", "write", "create", "build", "add", "cover",
    "scaffold", "implement", "produce", "make",
)
#: The things people actually name when they want work done. "page" and "form"
#: matter as much as "test": nobody writes "automate the login test", they write
#: "automate the login page".
_QA_NOUNS = (
    "test", "tests", "scenario", "scenarios", "spec", "specs", "suite",
    "automation", "coverage", "feature file", "page object", "step definition",
    "page", "form", "flow", "screen", "journey", "workflow", "endpoint", "api",
)
_RUN_TESTS = ("run the tests", "run tests", "execute the tests", "execute tests", "run the suite")
_HEAL = ("heal", "repair the failing", "fix the failing test", "fix failing")
_EXPLORE = ("explore the", "crawl the", "look at the application", "look at the app")

#: A question about the platform, not a request to use it on something.
_META = (
    "what can you do", "what do you do", "who are you", "what are you",
    "help", "how do i", "how does this", "how do you", "what is this",
    "capabilities", "commands", "get started", "getting started",
)

_CAPABILITIES = """I automate UI testing for the application this project points at.

Ask me for work in plain language, for example:
  - "automate the login page: valid sign-in and wrong password"
  - "cover the registration form, including required-field validation"
  - "run the tests and repair anything that fails"

What happens then: I read the requirement, index this repository, crawl the
application to collect verified locators, design scenarios, generate Gherkin,
Page Objects and step definitions, compile-check them, run them, and attempt a
repair on anything test-side that fails.

On free models a full run takes several minutes. You will see each agent start
and finish as it goes."""


def _normalise(message: str) -> str:
    return re.sub(r"[\s!.?,]+$", "", message.strip().lower())


def classify(message: str) -> Intent:
    """Decide without a model wherever the message allows it."""
    text = _normalise(message)
    if not text:
        return Intent("reply", "Tell me what to automate and I will get started.")

    if text in _GREETINGS:
        return Intent(
            "reply",
            "Hello. Tell me what to automate — a page, a form, a flow — and I will "
            "take it from there. Ask what I can do if you want the longer version.",
            suggestions=["automate the login page", "what can you do"],
        )
    if text in _THANKS:
        return Intent("reply", "Any time.")
    if text in _FAREWELL:
        return Intent("reply", "Right you are.")
    if any(phrase in text for phrase in _META):
        return Intent("reply", _CAPABILITIES)

    if any(phrase in text for phrase in _RUN_TESTS):
        return Intent("run", mode=RunMode.EXECUTE_ONLY)
    if any(phrase in text for phrase in _HEAL):
        return Intent("run", mode=RunMode.HEAL_ONLY)
    if any(phrase in text for phrase in _EXPLORE):
        return Intent("run", mode=RunMode.PLAN_ONLY)

    has_verb = any(re.search(rf"\b{verb}\b", text) for verb in _ACTION_VERBS)
    has_noun = any(noun in text for noun in _QA_NOUNS)
    if has_verb and has_noun:
        return Intent("run", mode=RunMode.FULL)

    # A bare question is a question, however long.
    if text.endswith("?") or text.split()[0] in ("what", "why", "when", "where", "who", "which", "is", "are", "can", "does", "do"):
        return Intent("reply", "", confident=False)

    # Short and verbless: almost certainly conversation.
    if len(text.split()) <= 3 and not has_verb:
        return Intent("reply", "", confident=False)

    # Long, with a verb or a QA noun: treat as work.
    if has_verb or has_noun:
        return Intent("run", mode=RunMode.FULL)

    return Intent("reply", "", confident=False)


ANSWER_SYSTEM = """You are the assistant inside a QAgentic automation tool, answering in its chat panel.

Answer the user's message in at most four sentences, plainly, with no preamble and no markdown headings.

If they are asking you to automate, test, or generate something, reply with exactly: RUN
Otherwise answer their question using what you know about the project below."""
