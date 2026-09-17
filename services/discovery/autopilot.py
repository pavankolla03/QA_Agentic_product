"""What to test, derived from what the application actually showed us.

Given only a URL there is no requirement document, no ticket and no prose - so
there is nothing to interpret and everything to observe. This module reads the
crawled `ApplicationMap` and names the features it can *see*: a form with a
password field is a sign-in, a form with required fields has a rejection path, a
page that renders rows has a list to assert on.

The rule that governs everything here is the same one that governs locators:
**only claim what the crawl is evidence for.** A validation message observed on
a page is evidence that the page validates something; it is not evidence of what
triggers it, so it goes in the rationale and never becomes an assertion. A page
reached over the HTTP fallback was never rendered, so its interactions are
proposed at reduced confidence and said to be unverified. Nothing here invents a
business rule, because a business rule cannot be seen from outside.

That restraint is the point. A model asked "what should I test at this URL?"
will happily produce forty plausible scenarios for an application it has never
loaded, and every one of them will be bound to a locator that does not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from services.knowledge_service.application_map import ApplicationMap, PageKnowledge

#: Feature kinds, in the order they are worth a tester's attention. Sign-in
#: gates everything behind it, so it is first whatever else was found.
KIND_AUTH = "authentication"
KIND_FORM = "form_submission"
KIND_SEARCH = "search"
KIND_LISTING = "listing"
KIND_NAVIGATION = "navigation"
KIND_PAGE_LOAD = "page_load"

_PRIORITY = {
    KIND_AUTH: 0,
    KIND_FORM: 1,
    KIND_SEARCH: 2,
    KIND_LISTING: 3,
    KIND_NAVIGATION: 4,
    KIND_PAGE_LOAD: 5,
}

#: A whole application is not a single run. Past this many features the plan
#: stops being something a person reviews and starts being something they
#: rubber-stamp, which is how unverified automation gets merged.
DEFAULT_FEATURE_BUDGET = 12

_PASSWORD_HINTS = ("password", "passwd", "pwd", "passphrase")
_SEARCH_HINTS = ("search", "query", "keyword", "filter")
_INPUT_ROLES = ("textbox", "combobox", "checkbox", "radio", "spinbutton")
_LIST_ROLES = ("table", "grid", "list", "listitem", "row", "rowgroup")

_WORD_RE = re.compile(r"[A-Za-z][a-z]+|[A-Z]{2,}")
#: "Sign in - Acme" is a page and a site; repeating the site in every feature
#: name makes all of them look alike.
_TITLE_SPLIT_RE = re.compile(r"\s+[|–—-]\s+")


@dataclass
class DiscoveredFeature:
    """One testable thing, and the evidence that it exists."""

    name: str
    kind: str
    route: str
    #: Why this is here, phrased as what was seen rather than what was assumed.
    rationale: str
    criteria: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    confidence: float = 0.7
    #: Captured over plain HTTP rather than in a browser, so nothing on it has
    #: been interacted with and its criteria are proposals, not observations.
    simulated: bool = False

    @property
    def priority(self) -> int:
        return _PRIORITY.get(self.kind, 9)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "route": self.route,
            "rationale": self.rationale,
            "criteria": list(self.criteria),
            "fields": list(self.fields),
            "confidence": round(self.confidence, 2),
            "simulated": self.simulated,
        }


# --------------------------------------------------------------------------- #
def derive_features(
    app_map: ApplicationMap, budget: int = DEFAULT_FEATURE_BUDGET
) -> list[DiscoveredFeature]:
    """Name every feature the crawl is evidence for, best first.

    Ordering is by kind and then by confidence, so truncating at the budget
    drops the least-supported page-load checks rather than the sign-in flow.
    """
    features: list[DiscoveredFeature] = []
    aliases = set(app_map.aliases or {})

    for route, raw in sorted((app_map.pages or {}).items()):
        if route in aliases:
            # Another route's page wearing a different URL. Testing it twice
            # would double the suite and halve what it means.
            continue
        page = _page(raw)
        if page is None:
            continue
        features.extend(_features_of(page))

    navigation = _navigation_feature(app_map)
    if navigation is not None:
        features.append(navigation)

    features.sort(key=lambda f: (f.priority, -f.confidence, f.route))
    return features[: max(1, budget)]


def _page(raw: Any) -> PageKnowledge | None:
    if isinstance(raw, PageKnowledge):
        return raw
    if not isinstance(raw, dict):
        return None
    known = set(PageKnowledge.__dataclass_fields__)
    try:
        return PageKnowledge(**{k: v for k, v in raw.items() if k in known})
    except TypeError:
        return None


# --------------------------------------------------------------------------- #
def _features_of(page: PageKnowledge) -> list[DiscoveredFeature]:
    locators = page.trustworthy_locators()
    inputs = [loc for loc in locators if loc.role in _INPUT_ROLES]
    required = [loc for loc in inputs if loc.required]
    label = _label(page)

    if page.forms and _has_password(inputs):
        return [_authentication(page, label, inputs, required)]
    if page.forms and inputs:
        return [_form(page, label, inputs, required)]
    if _is_search(inputs):
        return [_search(page, label, inputs)]
    if _renders_rows(locators):
        return [_listing(page, label, locators)]
    return [_page_load(page, label, locators)]


def _authentication(
    page: PageKnowledge, label: str, inputs: list[Any], required: list[Any]
) -> DiscoveredFeature:
    identifier = _identifier_field(inputs)
    secret = next((loc.name for loc in inputs if _is_password(loc)), "password")
    criteria = [
        f"Signing in at {page.route} with valid credentials leaves the sign-in page",
        f"Signing in at {page.route} with an incorrect {secret} does not sign the user in",
    ]
    criteria += _required_field_criteria(page.route, required)
    return DiscoveredFeature(
        name=_name(label, "sign in"),
        kind=KIND_AUTH,
        route=page.route,
        rationale=(
            f"{page.route} has a form containing a password field ({secret})"
            + (f" alongside {identifier}" if identifier else "")
            + _validation_evidence(page)
        ),
        criteria=criteria,
        fields=[loc.name for loc in inputs],
        confidence=_confidence(page, inputs),
        simulated=page.simulated,
    )


def _form(
    page: PageKnowledge, label: str, inputs: list[Any], required: list[Any]
) -> DiscoveredFeature:
    criteria = [f"Submitting the form at {page.route} with every field completed is accepted"]
    criteria += _required_field_criteria(page.route, required)
    return DiscoveredFeature(
        name=_name(label, "form submission"),
        kind=KIND_FORM,
        route=page.route,
        rationale=(
            f"{page.route} has {len(page.forms)} form(s) with "
            f"{len(inputs)} input(s), {len(required)} of them marked required"
            + _validation_evidence(page)
        ),
        criteria=criteria,
        fields=[loc.name for loc in inputs],
        confidence=_confidence(page, inputs),
        simulated=page.simulated,
    )


def _search(page: PageKnowledge, label: str, inputs: list[Any]) -> DiscoveredFeature:
    box = next((loc.name for loc in inputs if _looks_like(loc, _SEARCH_HINTS)), "search")
    return DiscoveredFeature(
        name=_name(label, "search"),
        kind=KIND_SEARCH,
        route=page.route,
        rationale=f"{page.route} has a search input ({box}) outside any form",
        criteria=[f"Searching from {page.route} updates what the page shows"],
        fields=[box],
        confidence=_confidence(page, inputs) * 0.9,
        simulated=page.simulated,
    )


def _listing(page: PageKnowledge, label: str, locators: list[Any]) -> DiscoveredFeature:
    rows = sum(1 for loc in locators if loc.role in ("row", "listitem"))
    # "At least one row" is only assertable because rows were counted during the
    # crawl. Against an empty list that criterion is simply not produced, rather
    # than producing a test that fails the first time it meets clean data.
    criteria = [f"{page.route} renders its list"]
    if rows:
        criteria.append(f"{page.route} shows at least one row")
    return DiscoveredFeature(
        name=_name(label, "listing"),
        kind=KIND_LISTING,
        route=page.route,
        rationale=f"{page.route} renders tabular or list markup ({rows} row(s) seen)",
        criteria=criteria,
        confidence=_confidence(page, locators) * 0.85,
        simulated=page.simulated,
    )


def _page_load(page: PageKnowledge, label: str, locators: list[Any]) -> DiscoveredFeature:
    criteria = [f"{page.route} loads without error"]
    if page.title:
        criteria.append(f'{page.route} shows the title "{page.title}"')
    return DiscoveredFeature(
        name=_name(label, "page loads"),
        kind=KIND_PAGE_LOAD,
        route=page.route,
        rationale=(
            f"{page.route} was reachable and rendered "
            f"{len(locators)} addressable element(s), but nothing to interact with"
        ),
        criteria=criteria,
        confidence=_confidence(page, locators) * 0.8,
        simulated=page.simulated,
    )


def _navigation_feature(app_map: ApplicationMap) -> DiscoveredFeature | None:
    """One feature for the links between pages, not one per link."""
    edges = [
        edge
        for edge in (app_map.navigation or [])
        if isinstance(edge, dict) and edge.get("from") and edge.get("to")
    ]
    if len(edges) < 2:
        return None
    destinations = sorted({str(edge["to"]) for edge in edges})[:6]
    return DiscoveredFeature(
        name="Navigation between pages",
        kind=KIND_NAVIGATION,
        route="/",
        rationale=f"{len(edges)} link(s) were followed during the crawl",
        criteria=[f"{dest} is reachable by following a link" for dest in destinations],
        confidence=0.7,
    )


# --------------------------------------------------------------------------- #
def _required_field_criteria(route: str, required: list[Any]) -> list[str]:
    # Each required field is its own rejection path, but past three of them the
    # suite is testing the browser's own validation rather than the application.
    return [f"Submitting {route} with {loc.name} left empty is rejected" for loc in required[:3]]


def _validation_evidence(page: PageKnowledge) -> str:
    """Observed messages, as evidence - never as an assertion.

    We saw the text. We did not see what produced it, so it cannot become a
    "then" without inventing the "when".
    """
    messages = [str(m).strip() for m in (page.validation_messages or []) if str(m).strip()]
    if not messages:
        return ""
    return ". Validation text present on the page: " + "; ".join(messages[:2])


def _confidence(page: PageKnowledge, locators: list[Any]) -> float:
    base = float(page.confidence or 0.7)
    if page.simulated:
        # Nothing on this page has been clicked; the markup is real but the
        # behaviour is entirely unobserved.
        base *= 0.7
    if not locators:
        base *= 0.6
    return max(0.1, min(0.99, base))


def _has_password(inputs: list[Any]) -> bool:
    return any(_is_password(loc) for loc in inputs)


def _is_password(locator: Any) -> bool:
    if (getattr(locator, "input_type", "") or "").lower() == "password":
        return True
    return _looks_like(locator, _PASSWORD_HINTS)


def _is_search(inputs: list[Any]) -> bool:
    return any(_looks_like(loc, _SEARCH_HINTS) for loc in inputs)


def _renders_rows(locators: list[Any]) -> bool:
    return any(getattr(loc, "role", "") in _LIST_ROLES for loc in locators)


def _looks_like(locator: Any, hints: tuple[str, ...]) -> bool:
    haystack = f"{getattr(locator, 'name', '')} {getattr(locator, 'locator', '')}".lower()
    return any(hint in haystack for hint in hints)


def _identifier_field(inputs: list[Any]) -> str:
    for locator in inputs:
        if _is_password(locator):
            continue
        if _looks_like(locator, ("email", "user", "login", "account", "phone")):
            return str(locator.name)
    named = [str(loc.name) for loc in inputs if not _is_password(loc)]
    return named[0] if named else ""


def _name(label: str, suffix: str) -> str:
    """Page name plus what is being tested, without saying it twice.

    A page titled "Sign in" produced "Sign in - sign in", which reads like a
    bug even though it is only a repetition.
    """
    if suffix.rstrip("s").lower() in label.lower():
        return label
    return f"{label} - {suffix}"


def _label(page: PageKnowledge) -> str:
    """A human name for the page: its title if it has one, else its route."""
    title = (page.title or "").strip()
    if title:
        head = _TITLE_SPLIT_RE.split(title)[0].strip()
        if head:
            return head[:60]
    spaced = page.route.replace("/", " ").replace("-", " ").replace("_", " ")
    words = _WORD_RE.findall(spaced)
    return " ".join(word.capitalize() for word in words) or "Home"


# --------------------------------------------------------------------------- #
def summarise(features: list[DiscoveredFeature], base_url: str) -> str:
    """One paragraph describing what was found, for the requirement summary."""
    if not features:
        return (
            f"Nothing testable was discovered at {base_url}. Either the crawl could not "
            "reach it, or every page it reached had no form, no list and no links."
        )
    by_kind: dict[str, int] = {}
    for feature in features:
        by_kind[feature.kind] = by_kind.get(feature.kind, 0) + 1
    parts = [f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(by_kind.items())]
    text = f"Crawled {base_url} and found {len(features)} testable feature(s): " + ", ".join(parts) + "."
    unverified = sum(1 for feature in features if feature.simulated)
    if unverified:
        text += (
            f" {unverified} of them come from pages captured over plain HTTP rather than in a "
            "browser, so their interactions are proposed rather than observed."
        )
    return text
