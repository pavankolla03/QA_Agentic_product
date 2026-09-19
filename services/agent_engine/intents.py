"""What a message is asking for, as a closed set.

The first version of this answered one question — reply, or run? — which was
enough to stop "Hi" launching a ten-agent pipeline, and not enough for anything
else. "How many tests failed?" is neither: it is a question the platform can
answer exactly, from its own database, without a model and without a run.

So intents are named. A named intent can be routed to a handler that knows the
answer, and the difference between a 3-second guess and a 20-millisecond fact
is the difference between a chat worth asking and one worth ignoring.

Resolution is deterministic first and a model only where prose genuinely
underdetermines the meaning — and the fallback, always, is to *answer* rather
than to start work. A wrong sentence costs a sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from packages.aiqa_types.enums import RunMode
from services.discovery.credentials import AppCredentials
from services.discovery.credentials import parse as parse_credentials
from services.discovery.urls import URL_RE, extract_url, is_bare_url_request


class Intent(StrEnum):
    """Every distinct thing a person can mean."""

    CONVERSATION = "CONVERSATION"
    HELP = "HELP"

    # Work
    RUN_CREATE = "RUN_CREATE"
    RUN_TESTS = "RUN_TESTS"
    RUN_HEAL = "RUN_HEAL"
    RUN_EXPLORE = "RUN_EXPLORE"
    #: A URL and nothing else: discover the application, then automate what was
    #: found. The one intent whose scope is decided by the crawl rather than by
    #: the sentence that started it.
    RUN_AUTOPILOT = "RUN_AUTOPILOT"
    RUN_CANCEL = "RUN_CANCEL"
    RUN_RETRY = "RUN_RETRY"

    # Questions the platform can answer from its own records
    RUN_STATUS = "RUN_STATUS"
    RUN_FAILURE_QUERY = "RUN_FAILURE_QUERY"
    RUN_REPORT = "RUN_REPORT"
    COST_QUERY = "COST_QUERY"
    PROJECT_QUERY = "PROJECT_QUERY"
    TEST_QUERY = "TEST_QUERY"
    CONFIGURATION_QUERY = "CONFIGURATION_QUERY"

    # Decisions
    APPROVAL_ACCEPT = "APPROVAL_ACCEPT"
    APPROVAL_REJECT = "APPROVAL_REJECT"

    UNKNOWN = "UNKNOWN"

    @property
    def starts_a_run(self) -> bool:
        return self in _RUN_INTENTS

    @property
    def is_a_platform_query(self) -> bool:
        """Can this be answered from the database alone?"""
        return self in _QUERY_INTENTS


_RUN_INTENTS = frozenset(
    {
        Intent.RUN_CREATE,
        Intent.RUN_TESTS,
        Intent.RUN_HEAL,
        Intent.RUN_EXPLORE,
        Intent.RUN_AUTOPILOT,
    }
)
_QUERY_INTENTS = frozenset(
    {
        Intent.RUN_STATUS,
        Intent.RUN_FAILURE_QUERY,
        Intent.RUN_REPORT,
        Intent.COST_QUERY,
        Intent.PROJECT_QUERY,
        Intent.TEST_QUERY,
        Intent.CONFIGURATION_QUERY,
    }
)

#: Which run mode each work intent implies.
RUN_MODES: dict[Intent, RunMode] = {
    Intent.RUN_CREATE: RunMode.FULL,
    Intent.RUN_TESTS: RunMode.EXECUTE_ONLY,
    Intent.RUN_HEAL: RunMode.HEAL_ONLY,
    Intent.RUN_EXPLORE: RunMode.PLAN_ONLY,
    # Autopilot is a full run. Stopping at a plan would mean answering "what
    # can you test here?" with a list, when the question asked was "test it".
    Intent.RUN_AUTOPILOT: RunMode.FULL,
}


@dataclass
class Resolution:
    """What one message resolved to, and how sure we are."""

    intent: Intent
    confident: bool = True
    #: Filled in by a deterministic handler; empty means "somebody else answers".
    text: str = ""
    suggestions: list[str] = field(default_factory=list)
    #: A run id the message referred to, when it named one.
    run_id: str = ""
    #: The application the message pointed at. Set whenever a URL appears,
    #: whatever the intent - "automate the login page at https://x" is a
    #: RUN_CREATE that still knows where to look.
    target_url: str = ""
    #: An account found in the message, already removed from `instruction`.
    #:
    #: Carried rather than stored, because resolving a message is pure and
    #: writing a secret to disk is not. Whoever starts the run decides where it
    #: goes.
    credentials: AppCredentials | None = None
    #: The message with any credentials taken out - what becomes the run's
    #: instruction and what every UI shows afterwards. The only thing standing
    #: between a typed password and a permanent record of it.
    instruction: str = ""

    @property
    def mode(self) -> RunMode:
        return RUN_MODES.get(self.intent, RunMode.FULL)


# --------------------------------------------------------------------------- #
# Deterministic resolution
# --------------------------------------------------------------------------- #
_GREETINGS = {
    "hi", "hii", "hiii", "hey", "heya", "hello", "hallo", "yo", "sup",
    "good morning", "good afternoon", "good evening", "morning", "evening",
    "namaste", "hola", "howdy",
}
_THANKS = {"thanks", "thank you", "ty", "thx", "cheers", "nice", "great", "cool", "ok", "okay", "k"}
_FAREWELL = {"bye", "goodbye", "see you", "later", "good night", "gn"}

_HELP = (
    "what can you do", "what do you do", "who are you", "what are you",
    "help", "how do i", "how does this", "how do you", "what is this",
    "capabilities", "commands", "get started", "getting started",
)

#: Ordered, because the first match wins and some phrases overlap. "run the
#: tests and tell me what failed" is a request to run, not a question about a
#: previous run, so the work patterns are consulted before the query ones.
_WORK_PATTERNS: tuple[tuple[str, Intent], ...] = (
    (r"\b(run|execute)\s+(the\s+)?(tests?|suite|smoke|regression)\b", Intent.RUN_TESTS),
    (r"\b(heal|repair|fix)\b.*\b(fail|broken|test)", Intent.RUN_HEAL),
    (r"\b(explore|crawl)\b.*\b(app|application|site|page)", Intent.RUN_EXPLORE),
)

#: Questions about work already done. `<run>` matches an id if one is present.
_QUERY_PATTERNS: tuple[tuple[str, Intent], ...] = (
    (r"\b(cost|spend|spent|budget|tokens?\s+used|how much)\b", Intent.COST_QUERY),
    (r"\bwhy\b.*\b(fail|failed|failing|broke|broken)\b", Intent.RUN_FAILURE_QUERY),
    (r"\b(what|which|how many)\b.*\b(fail|failed|failing)\b", Intent.RUN_FAILURE_QUERY),
    (r"\b(status|progress|still running|finished|done yet|how far)\b", Intent.RUN_STATUS),
    (r"\b(how many|what)\b.*\b(pass|passed|passing)\b", Intent.RUN_STATUS),
    (r"\b(report|summary|summarise|summarize)\b", Intent.RUN_REPORT),
    (r"\b(last|previous|latest)\s+run\b", Intent.RUN_STATUS),
    (r"\b(what|which)\b.*\b(project|repository|repo)\b", Intent.PROJECT_QUERY),
    (r"\b(what|which)\b.*\b(model|provider|config|configuration|setting)", Intent.CONFIGURATION_QUERY),
    (r"\b(how many|what)\b.*\b(tests?|scenarios?)\b.*\b(exist|have|generated|there)\b", Intent.TEST_QUERY),
)

_APPROVE = {"approve", "approved", "yes approve", "accept", "lgtm", "go ahead", "approve it"}
_REJECT = {"reject", "rejected", "no", "deny", "decline", "reject it"}

_CANCEL = (r"\b(cancel|stop|abort|kill)\b.*\b(run|it|that|test)", Intent.RUN_CANCEL)
_RETRY = (r"\b(retry|re-?run|try again)\b", Intent.RUN_RETRY)

#: Words that mean "do the work", and the things they act on.
_ACTION_VERBS = (
    "automate", "generate", "write", "create", "build", "add", "cover",
    "scaffold", "implement", "produce", "make",
)
_QA_NOUNS = (
    "test", "tests", "scenario", "scenarios", "spec", "specs", "suite",
    "automation", "coverage", "feature file", "page object", "step definition",
    "page", "form", "flow", "screen", "journey", "workflow", "endpoint", "api",
)

_RUN_ID_RE = re.compile(r"\b(run_[0-9a-f]{8,}|QA-\d+)\b", re.IGNORECASE)

_QUESTION_OPENERS = ("what", "why", "when", "where", "who", "which", "is", "are", "can", "does", "do", "how")


def _normalise(message: str) -> str:
    return re.sub(r"[\s!.?,]+$", "", (message or "").strip().lower())


def resolve(message: str) -> Resolution:
    """Name what this message wants, without a model wherever possible.

    Credentials come out first. "http://localhost:8123 user: qa pass: ..." is a
    request to automate that application, but with the account still in the
    sentence it classifies as UNKNOWN, falls through to a model, and comes back
    as a polite refusal to visit URLs — the headline feature failing because of
    two words at the end of the line.
    """
    message, credentials = parse_credentials(message or "")

    run_id = (_RUN_ID_RE.search(message) or [None])[0] if message else None
    run_id = run_id if isinstance(run_id, str) else ""

    target_url = extract_url(message or "")
    text = _normalise(URL_RE.sub(" ", message or "") if target_url else (message or ""))

    if target_url and is_bare_url_request(text):
        # A URL with nothing meaningful around it. There is no requirement to
        # interpret, so the application itself becomes the requirement: crawl
        # it, name what is there, automate that.
        resolution = Resolution(Intent.RUN_AUTOPILOT, run_id=run_id, target_url=target_url)
    else:
        resolution = _classify(text, run_id)
        resolution.target_url = target_url

    resolution.credentials = credentials
    resolution.instruction = message
    return resolution


def _classify(text: str, run_id: str) -> Resolution:
    if not text:
        return Resolution(Intent.CONVERSATION)

    if text in _GREETINGS or text in _THANKS or text in _FAREWELL:
        return Resolution(Intent.CONVERSATION)
    if any(phrase in text for phrase in _HELP):
        return Resolution(Intent.HELP)

    if text in _APPROVE:
        return Resolution(Intent.APPROVAL_ACCEPT, run_id=run_id)
    if text in _REJECT:
        return Resolution(Intent.APPROVAL_REJECT, run_id=run_id)

    for pattern, intent in (_CANCEL, _RETRY):
        if re.search(pattern, text):
            return Resolution(intent, run_id=run_id)

    # Work before questions: "run the tests and tell me what failed" is work.
    for pattern, intent in _WORK_PATTERNS:
        if re.search(pattern, text):
            return Resolution(intent, run_id=run_id)

    for pattern, intent in _QUERY_PATTERNS:
        if re.search(pattern, text):
            return Resolution(intent, run_id=run_id)

    has_verb = any(re.search(rf"\b{verb}\b", text) for verb in _ACTION_VERBS)
    has_noun = any(noun in text for noun in _QA_NOUNS)
    if has_verb and has_noun:
        return Resolution(Intent.RUN_CREATE)

    words = text.split()
    if text.endswith("?") or (words and words[0] in _QUESTION_OPENERS):
        return Resolution(Intent.UNKNOWN, confident=False)
    if len(words) <= 3 and not has_verb:
        return Resolution(Intent.UNKNOWN, confident=False)
    if has_verb or has_noun:
        return Resolution(Intent.RUN_CREATE)

    return Resolution(Intent.UNKNOWN, confident=False)
