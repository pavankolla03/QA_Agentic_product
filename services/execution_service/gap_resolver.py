"""Closing a step-coverage gap with evidence, or admitting it cannot be closed.

A step like "When I enter the resident's email" needs a UI action behind it.
There are two ways to produce one: work out which observed element it refers to,
or invent something plausible. The second is how a suite ends up green while
clicking a button that does not exist, so this module only does the first.

The search is a ladder, cheapest and most certain first:

1. **An existing step definition.** The step is already implemented somewhere in
   the repository under a slightly different wording. Nothing to generate.
2. **An existing Page Object method.** The capability exists; only the binding
   is missing. Generate the binding alone.
3. **A verified locator.** The element was observed in the live DOM. Generate a
   Page Object method that drives it, then the binding.
4. **Nothing.** Say so, name the step, and say what a human needs to supply.

Rung four is not a failure of the resolver. It is the resolver declining to
guess, which is the only useful thing it can do when the application never
showed it the control in question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

#: Words that carry no signal when matching a step to an element. "the user
#: clicks the submit button" and "submit" should look alike.
_NOISE = frozenset(
    {
        "a", "an", "the", "i", "user", "users", "he", "she", "they", "we",
        "is", "are", "am", "be", "been", "being", "was", "were",
        "on", "in", "at", "to", "into", "from", "with", "and", "or", "of", "for",
        "should", "must", "will", "can", "that", "this", "it", "its",
        "page", "screen", "form", "field", "button", "link", "box",
        "see", "sees", "shown", "displayed", "appear", "appears",
    }
)

#: How alike a step and an element have to be before the match is used. Set
#: high on purpose: a wrong binding produces a test that passes for the wrong
#: reason, which is worse than an honest gap.
_MATCH_FLOOR = 0.62

#: What a step is asking to be done, inferred from its verb.
_ACTIONS: tuple[tuple[str, str], ...] = (
    (r"\b(click|press|tap|push|select|choose|submit|save|confirm|continue)\b", "click"),
    (r"\b(enter|type|fill|input|provide|supply|set|write)\b", "fill"),
    (r"\b(check|tick|enable)\b", "check"),
    (r"\b(uncheck|untick|disable)\b", "uncheck"),
    (r"\b(see|show|display|appear|visible|contains?|shows?)\b", "assert"),
    (r"\b(go|navigate|open|visit|browse)\b", "navigate"),
)


@dataclass
class Resolution:
    """How one gap can be closed, or why it cannot."""

    step: str
    strategy: str                       # reuse_step | reuse_method | generate_method | blocked
    reason: str = ""
    page_class: str = ""
    method: str = ""
    action: str = ""
    locator: str = ""
    locator_name: str = ""
    role: str = ""
    params: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def resolved(self) -> bool:
        return self.strategy != "blocked"


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _NOISE and len(w) > 1}


def _similarity(step: str, candidate: str) -> float:
    """How much of the candidate's name the step accounts for.

    Recall carries the decision — an element called "Email" is fully named by
    the step "I enter the resident's email address", even though most of that
    step is about something else. A plain character ratio scores that pairing
    badly and would miss it.

    Precision then breaks ties, and it has to. Recall alone rates the method
    `login` and the method `expectLoginError` identically against "I expect a
    login error": both are fully contained in the step. The first is a worse
    answer, and without this term it was the one being chosen.
    """
    step_tokens, candidate_tokens = _tokens(step), _tokens(candidate)
    if not candidate_tokens or not step_tokens:
        return 0.0
    shared = step_tokens & candidate_tokens
    recall = len(shared) / len(candidate_tokens)
    precision = len(shared) / len(step_tokens)
    return recall * (0.7 + 0.3 * precision)


def action_for(step: str) -> str:
    for pattern, action in _ACTIONS:
        if re.search(pattern, step.lower()):
            return action
    return ""


#: Every verb that maps to an action, so a name does not repeat it. The prefix
#: already says "fill"; `fillFillFullName` says it twice.
_ACTION_WORDS = frozenset(
    {
        "click", "press", "tap", "push", "select", "choose", "submit", "save",
        "confirm", "continue", "enter", "type", "fill", "input", "provide",
        "supply", "set", "write", "check", "tick", "enable", "uncheck",
        "untick", "disable", "see", "show", "display", "appear", "visible",
        "contain", "contains", "shows", "go", "navigate", "open", "visit",
        "browse", "in",
    }
)


def method_name_for(step: str, action: str) -> str:
    """A method name a reviewer would accept, derived from the step's own words.

    The action verb is dropped from the tail because the prefix already carries
    it, and a stranded possessive "s" is dropped too: without either,
    "I enter the resident's email" became `fillEnterResidentSEmail`.
    """
    # `{string}` is a placeholder, not a word about the element. Left in, it
    # produced `fillResidentEmailAddressString`.
    without_placeholders = re.sub(r"\{[a-z]+\}", " ", step.lower())
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", without_placeholders)
        if word not in _NOISE and word not in _ACTION_WORDS and word != "s"
    ][:4]
    if not words:
        words = ["step"]
    prefix = {"assert": "expect", "navigate": "goTo"}.get(action, action or "do")
    tail = "".join(w[:1].upper() + w[1:] for w in words)
    name = f"{prefix}{tail}"
    return name[:1].lower() + name[1:]


class GapResolver:
    """Finds the evidence for a step, or reports that there is none."""

    def __init__(
        self,
        catalog: list[dict[str, Any]] | None = None,
        existing_steps: list[str] | None = None,
        existing_methods: dict[str, set[str]] | None = None,
        route: str = "",
    ) -> None:
        self.catalog = list(catalog or [])
        self.existing_steps = list(existing_steps or [])
        self.existing_methods = {k: set(v) for k, v in (existing_methods or {}).items()}
        self.route = route

    # ------------------------------------------------------------------ #
    def resolve(self, step: str) -> Resolution:
        """Walk the ladder. The first rung that holds wins."""
        return (
            self._existing_step(step)
            or self._existing_method(step)
            or self._from_catalogue(step)
            or self._blocked(step)
        )

    def resolve_all(self, steps: list[str]) -> list[Resolution]:
        return [self.resolve(step) for step in steps]

    # -- 1. already implemented ----------------------------------------- #
    def _existing_step(self, step: str) -> Resolution | None:
        """Is this the same step, differently worded?

        Compared as whole sentences rather than as token overlap. Overlap is
        the right measure for "does this step refer to this element", where the
        element's name is a fragment of the step — and exactly the wrong one
        here: an existing step that reduces to the single word "login" scored a
        perfect match against "I expect a login error", which is a different
        step entirely and would have been silently bound to the wrong body.
        """
        target = " ".join(sorted(_tokens(step)))
        for existing in self.existing_steps:
            candidate = " ".join(sorted(_tokens(existing)))
            if not candidate:
                continue
            if SequenceMatcher(None, target, candidate).ratio() >= 0.9:
                return Resolution(
                    step=step, strategy="reuse_step", confidence=0.95,
                    reason=f"already implemented as \"{existing}\"",
                )
        return None

    # -- 2. the capability exists, the binding does not ----------------- #
    def _existing_method(self, step: str) -> Resolution | None:
        best: tuple[float, str, str] | None = None
        for page_class, methods in self.existing_methods.items():
            for method in methods:
                # Method names are camelCase; split them so "fillResidentName"
                # is compared as three words rather than one.
                readable = re.sub(r"(?<!^)(?=[A-Z])", " ", method)
                score = _similarity(step, readable)
                if score >= _MATCH_FLOOR and (best is None or score > best[0]):
                    best = (score, page_class, method)
        if best is None:
            return None
        score, page_class, method = best
        return Resolution(
            step=step, strategy="reuse_method", page_class=page_class, method=method,
            confidence=round(score, 3),
            reason=f"{page_class}.{method}() already does this",
        )

    # -- 3. the element was observed in the live DOM -------------------- #
    def _from_catalogue(self, step: str) -> Resolution | None:
        action = action_for(step)
        if not action:
            return None

        wanted_roles = {
            "click": {"button", "link"},
            "fill": {"textbox", "combobox"},
            "check": {"checkbox", "radio"},
            "uncheck": {"checkbox", "radio"},
            "assert": set(),          # anything can be asserted on
            "navigate": {"link"},
        }[action]

        best: tuple[float, dict[str, Any]] | None = None
        for entry in self.catalog:
            if self.route and entry.get("page") not in (self.route, ""):
                continue
            role = str(entry.get("role", ""))
            if wanted_roles and role not in wanted_roles:
                continue
            score = _similarity(step, str(entry.get("name", "")))
            if score >= _MATCH_FLOOR and (best is None or score > best[0]):
                best = (score, entry)

        if best is None:
            return None
        score, entry = best
        params = ["value"] if action == "fill" else (["message"] if action == "assert" else [])
        return Resolution(
            step=step, strategy="generate_method",
            method=method_name_for(step, action),
            action=action,
            locator=str(entry.get("locator", "")),
            locator_name=str(entry.get("name", "")),
            role=str(entry.get("role", "")),
            params=params,
            confidence=round(min(score, float(entry.get("confidence", 0.9))), 3),
            reason=(
                f"\"{entry.get('name')}\" was observed on {entry.get('page')} as a "
                f"{entry.get('role')} (confidence {entry.get('confidence')})"
            ),
        )

    # -- 4. no evidence ------------------------------------------------- #
    @staticmethod
    def _blocked(step: str) -> Resolution:
        action = action_for(step)
        if not action:
            detail = (
                "the step does not name an action I can recognise, so I cannot tell "
                "what it should do"
            )
        else:
            detail = (
                f"no verified element matches it — nothing observed in the application "
                f"corresponds to a \"{action}\" for this step"
            )
        return Resolution(step=step, strategy="blocked", reason=detail)
