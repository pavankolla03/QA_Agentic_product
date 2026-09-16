"""Application Map — persistent knowledge of the application under test.

Re-crawling an application for every scenario is the other half of the naive
cost problem. Designing 500 scenarios against one app should explore it *once*,
not 500 times.

This module stores what exploration learned — routes, elements, verified
locators, forms, validation messages, navigation edges — with a confidence and a
`last_verified` timestamp on every locator. A later run answers "what is on the
registration page?" from the map, and only re-explores when the map says it
cannot be trusted:

* the page has never been seen
* its DOM hash changed
* a locator from it failed in a recent run
* confidence decayed below the floor
* the entry is older than the TTL
* the application version changed

Locators also carry an outcome history, so a selector that keeps working earns
confidence and one that fails loses it. That feedback loop is what stops the
platform from confidently reusing a stale selector forever.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("aiqa.appmap")

MAP_VERSION = 1
MAP_RELATIVE_PATH = Path(".aiqa") / "application_map.json"

#: A page is re-explored once its knowledge is older than this.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
#: Below this, a locator is not trusted for code generation.
CONFIDENCE_FLOOR = 0.45


def _now() -> float:
    return time.time()


def route_of(url: str) -> str:
    """Generalise a URL into a route pattern: `/residents/482/edit` -> `/residents/:id/edit`."""
    import re

    path = urlparse(url).path or "/"
    parts: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        if segment.isdigit() or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", segment):
            parts.append(":id")
        else:
            parts.append(segment)
    return "/" + "/".join(parts)


# --------------------------------------------------------------------------- #
@dataclass
class LocatorKnowledge:
    """One element, and how much we trust the way we address it."""

    name: str = ""
    role: str = ""
    locator: str = ""
    strategy: str = ""
    confidence: float = 0.5
    source: str = "exploration"        # exploration | http_probe | healing | manual
    required: bool = False
    input_type: str | None = None
    alternatives: list[str] = field(default_factory=list)
    last_verified: float = field(default_factory=_now)
    verified_count: int = 1
    failed_count: int = 0

    @property
    def trustworthy(self) -> bool:
        return self.confidence >= CONFIDENCE_FLOOR and self.failed_count < 3

    def record_success(self) -> None:
        self.verified_count += 1
        self.last_verified = _now()
        # Asymptotic gain: repeated success approaches, but never reaches, certainty.
        self.confidence = min(0.99, self.confidence + (1.0 - self.confidence) * 0.35)

    def record_failure(self) -> None:
        self.failed_count += 1
        # Failure is punished harder than success is rewarded: a selector that
        # breaks once is suspect, and we would rather re-explore than guess.
        self.confidence = max(0.05, self.confidence * 0.45)

    def age_seconds(self) -> float:
        return max(0.0, _now() - self.last_verified)


#: Methods a form element can legitimately declare. Anything else is markup we
#: do not understand, and is normalised rather than trusted.
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


@dataclass
class PageKnowledge:
    """Everything known about one route of the application."""

    route: str
    url: str = ""
    title: str = ""
    dom_hash: str = ""
    elements: list[dict[str, Any]] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)
    validation_messages: list[str] = field(default_factory=list)
    navigations: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    components: list[str] = field(default_factory=list)
    explored_at: float = field(default_factory=_now)
    visit_count: int = 1
    confidence: float = 0.7
    simulated: bool = False            # captured via the HTTP fallback, not a browser
    #: Consecutive explorations that produced the same `dom_hash`.
    #:
    #: A visual regression test on a page whose markup changes every visit is a
    #: guaranteed false positive, and a suite that cries wolf gets ignored. This
    #: counter is how the platform tells a settled page from a churning one.
    stable_visits: int = 0

    def visually_stable(self, required: int = 2) -> bool:
        """Has this page looked the same often enough to be worth baselining?"""
        # A page captured over plain HTTP was never rendered, so its screenshot
        # would not reflect what a user sees.
        return not self.simulated and bool(self.dom_hash) and self.stable_visits >= required

    def locators(self) -> list[LocatorKnowledge]:
        out: list[LocatorKnowledge] = []
        for raw in self.elements:
            try:
                out.append(LocatorKnowledge(**raw))
            except TypeError:
                continue
        return out

    def trustworthy_locators(self) -> list[LocatorKnowledge]:
        return [locator for locator in self.locators() if locator.trustworthy]

    def endpoints(self) -> list[dict[str, Any]]:
        """The API surface this page exposes, taken from its forms.

        A form's `action` and `method` are the application telling us, in its
        own markup, which endpoint it posts to and with which fields. That is
        evidence, not inference, which is the only basis on which an API test
        should be generated at all — the same rule that governs locators.

        Endpoints reached only by client-side fetch/XHR are not visible here.
        They are simply absent rather than guessed at.
        """
        out: list[dict[str, Any]] = []
        for form in self.forms:
            action = str(form.get("action") or "").strip()
            if not action or action.startswith(("javascript:", "#")):
                continue
            method = str(form.get("method") or "get").upper()
            fields = [
                locator.name
                for locator in self.locators()
                if locator.role in ("textbox", "combobox", "checkbox", "radio")
            ]
            out.append(
                {
                    "path": action if action.startswith("/") else f"/{action.lstrip('./')}",
                    "method": method if method in _HTTP_METHODS else "GET",
                    "page": self.route,
                    "fields": fields,
                    "required_fields": [
                        locator.name
                        for locator in self.locators()
                        if locator.required and locator.role in ("textbox", "combobox")
                    ],
                    "source": "form_action",
                }
            )
        return out

    def age_seconds(self) -> float:
        return max(0.0, _now() - self.explored_at)


@dataclass
class ApplicationMap:
    """The durable model of the application under test."""

    version: int = MAP_VERSION
    project_id: str = ""
    base_url: str = ""
    app_version: str = ""
    explored_at: float = 0.0
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    navigation: list[dict[str, str]] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    #: Routes that turned out to be another route's page — usually a catch-all
    #: handler answering 200 to a candidate path the crawler proposed. Kept so
    #: exploration does not re-crawl them, and so "why is /valid not a page?"
    #: has an answer, but never offered as a page anything can be bound to.
    aliases: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, root: Path | str) -> ApplicationMap | None:
        path = Path(root) / MAP_RELATIVE_PATH
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or int(data.get("version", 0)) != MAP_VERSION:
            return None
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load_or_new(cls, root: Path | str, project_id: str = "", base_url: str = "") -> ApplicationMap:
        """Load the persisted map, or start an empty one for this project."""
        existing = cls.load(root)
        if existing is not None:
            existing.project_id = project_id or existing.project_id
            # A changed base URL means a different environment; keep the routes
            # but record the move so staleness checks can reason about it.
            if base_url and existing.base_url and base_url != existing.base_url:
                existing.base_url = base_url
            elif base_url:
                existing.base_url = base_url
            return existing
        return cls(project_id=project_id, base_url=base_url)

    def save(self, root: Path | str) -> None:
        path = Path(root) / MAP_RELATIVE_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2, default=str), encoding="utf-8", newline="\n")
        except OSError as exc:  # pragma: no cover
            log.warning("could not persist the application map: %s", exc)

    # ------------------------------------------------------------------ #
    def page(self, route: str) -> PageKnowledge | None:
        # An alias is a real path that reaches a page we already hold, so a
        # lookup for it should succeed — it just must not be a *separate* page.
        raw = self.pages.get(route) or self.pages.get(self.aliases.get(route, ""))
        if not raw:
            return None
        try:
            return PageKnowledge(**raw)
        except TypeError:
            return None

    def put_page(self, page: PageKnowledge) -> None:
        # Two routes serving byte-identical DOM are one page. The crawler
        # proposes candidate routes from the words in the instruction --
        # /automation, /form, /valid -- and an application that answers 200 to
        # everything accepts all of them. One demo map held 25 "routes" of
        # which 21 were the same login page, so a registration Page Object was
        # bound to the login form's fields and every generated method filled
        # username and password. An alias is not a page.
        twin = self._route_with_same_dom(page)
        if twin is not None:
            # Which of the two is the real one matters. Crawl order is an
            # accident, so recording whichever arrived first would happily make
            # the guessed `/automation` the page and demote the linked
            # `/dashboard` to an alias of it.
            if self._more_canonical(page.route, twin):
                self.pages[page.route] = self.pages.pop(twin)
                self.pages[page.route]["route"] = page.route
                self.aliases = {
                    route: (page.route if target == twin else target)
                    for route, target in self.aliases.items()
                }
                self.aliases[twin] = page.route
            else:
                self.aliases[page.route] = twin
            return

        existing = self.page(page.route)
        if existing is not None:
            page.visit_count = existing.visit_count + 1
            page.stable_visits = (
                existing.stable_visits + 1
                if page.dom_hash and page.dom_hash == existing.dom_hash
                else 0
            )
            # Keep a screenshot we already had if this capture produced none.
            page.screenshot_path = page.screenshot_path or existing.screenshot_path
            # Carry forward the outcome history of locators we have seen before.
            previous = {locator.locator: locator for locator in existing.locators()}
            merged: list[dict[str, Any]] = []
            for locator in page.locators():
                prior = previous.get(locator.locator)
                if prior is not None:
                    locator.verified_count = prior.verified_count + 1
                    locator.failed_count = prior.failed_count
                    locator.confidence = max(locator.confidence, prior.confidence)
                merged.append(asdict(locator))
            page.elements = merged
        self.pages[page.route] = asdict(page)
        self.explored_at = _now()

    def known_routes(self) -> list[str]:
        return sorted(self.pages)

    def _more_canonical(self, candidate: str, current: str) -> bool:
        """Is `candidate` the better name for a page currently filed as `current`?

        Three rules, in order:

        1. A route another page links to is one the application admits exists.
           A route the crawler invented from the words in an instruction is
           not, however plausibly it answers.
        2. `/` loses to any named route. The root usually redirects, and a Page
           Object whose `path` is `/` says nothing about what it models —
           `/login` is both truer and more stable.
        3. Otherwise keep what is already recorded. Between two routes the
           application never links to and that serve the same bytes, there is
           no evidence to prefer either — "shorter wins" would make `/form`
           beat `/login`, which is arbitrary dressed up as a rule. First seen
           wins instead, and first seen is the order the run asked for.
        """
        linked = self._linked_routes()
        if (candidate in linked) != (current in linked):
            return candidate in linked
        if (candidate == "/") != (current == "/"):
            return current == "/"
        return False

    def _linked_routes(self) -> set[str]:
        """Every route reachable by a link from a page already in the map."""
        routes: set[str] = set()
        for known in self.pages.values():
            for href in known.get("navigations", []) or []:
                routes.add(route_of(str(href)))
        return routes

    def _route_with_same_dom(self, page: PageKnowledge) -> str | None:
        """The route already holding this exact DOM, if another one does.

        Only an exact hash counts. Two genuinely different pages that happen to
        look similar must stay separate, and a page whose DOM is empty tells us
        nothing at all.
        """
        if not page.dom_hash:
            return None
        for route, known in self.pages.items():
            if route != page.route and known.get("dom_hash") == page.dom_hash:
                return route
        return None

    # ------------------------------------------------------------------ #
    def needs_exploration(
        self,
        route: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        app_version: str = "",
        failed_locators: set[str] | None = None,
        browser_available: bool = False,
    ) -> tuple[bool, str]:
        """Decide whether a route must be re-crawled. Returns ``(needed, reason)``.

        This is the function that turns "explore every time" into "explore once",
        so it errs toward re-exploring only for concrete, nameable reasons.
        """
        page = self.page(route)
        if page is None:
            return True, "route has never been explored"
        if app_version and self.app_version and app_version != self.app_version:
            return True, f"application version changed ({self.app_version} -> {app_version})"
        if page.simulated and browser_available:
            # Worth re-crawling only because we can now do better than the HTTP
            # probe. Without a browser this would loop forever re-capturing the
            # same static HTML, so a simulated page stays cached instead.
            return True, "a browser is now available to improve on the HTTP-only capture"
        if not page.elements:
            return True, "no elements were captured previously"

        if failed_locators:
            known = {locator.locator for locator in page.locators()}
            overlap = known & failed_locators
            if overlap:
                return True, f"{len(overlap)} locator(s) from this page failed in a recent run"

        trustworthy = page.trustworthy_locators()
        if not trustworthy:
            return True, "no locator on this page is still trusted"
        if len(trustworthy) / max(1, len(page.elements)) < 0.5:
            return True, "more than half of this page's locators have decayed"
        if page.age_seconds() > ttl_seconds:
            return True, f"knowledge is {page.age_seconds() / 86400:.1f} days old (TTL exceeded)"

        return False, f"cached ({len(trustworthy)} trusted locators, visit #{page.visit_count})"

    def plan_exploration(
        self,
        routes: list[str],
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        app_version: str = "",
        failed_locators: set[str] | None = None,
        browser_available: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        """Split candidate routes into those needing a crawl and those already known."""
        to_explore: list[str] = []
        cached: dict[str, str] = {}
        for route in routes:
            needed, reason = self.needs_exploration(
                route, ttl_seconds=ttl_seconds, app_version=app_version,
                failed_locators=failed_locators, browser_available=browser_available,
            )
            if needed:
                to_explore.append(route)
            else:
                cached[route] = reason
        return to_explore, cached

    # ------------------------------------------------------------------ #
    def record_locator_outcome(self, locator: str, success: bool) -> bool:
        """Feed execution results back into locator confidence."""
        touched = False
        for route, raw in self.pages.items():
            page = PageKnowledge(**raw)
            changed = False
            elements = page.locators()
            for element in elements:
                if element.locator == locator:
                    element.record_success() if success else element.record_failure()
                    changed = True
            if changed:
                page.elements = [asdict(e) for e in elements]
                self.pages[route] = asdict(page)
                touched = True
        return touched

    def catalog(self, routes: list[str] | None = None, limit: int = 120) -> list[dict[str, Any]]:
        """Flat, prompt-ready locator list — the *only* app context codegen needs."""
        out: list[dict[str, Any]] = []
        for route, raw in self.pages.items():
            if routes and route not in routes:
                continue
            page = PageKnowledge(**raw)
            for element in page.trustworthy_locators():
                out.append(
                    {
                        "page": route,
                        "name": element.name,
                        "role": element.role,
                        "required": element.required,
                        "locator": element.locator,
                        "strategy": element.strategy,
                        "confidence": round(element.confidence, 3),
                    }
                )
        out.sort(key=lambda item: item["confidence"], reverse=True)
        return out[:limit]

    def api_catalog(self, limit: int = 40) -> list[dict[str, Any]]:
        """Every endpoint the application revealed, de-duplicated.

        This is to API tests what `catalog()` is to page objects: the closed set
        of things a generated test is allowed to call. A path that is not in
        here was never observed, so a test against it would be a guess.
        """
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in self.pages.values():
            page = PageKnowledge(**raw)
            for endpoint in page.endpoints():
                key = (endpoint["method"], endpoint["path"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(endpoint)
                if len(out) >= limit:
                    return out
        return out

    def visual_candidates(self, required_stable_visits: int = 2) -> list[dict[str, Any]]:
        """Routes settled enough that a screenshot assertion would be meaningful.

        Visual regression is the easiest kind of test to make worthless: point it
        at a dashboard with a live clock and it fails every run until somebody
        deletes it. So a route qualifies only after its markup has come back
        identical on consecutive visits.
        """
        out: list[dict[str, Any]] = []
        for route, raw in sorted(self.pages.items()):
            page = PageKnowledge(**raw)
            if not page.visually_stable(required_stable_visits):
                continue
            out.append(
                {
                    "route": route,
                    "title": page.title,
                    "dom_hash": page.dom_hash,
                    "screenshot_path": page.screenshot_path,
                    "stable_visits": page.stable_visits,
                }
            )
        return out

    def stats(self) -> dict[str, Any]:
        pages = [PageKnowledge(**raw) for raw in self.pages.values()]
        all_locators = [locator for page in pages for locator in page.locators()]
        trusted = [locator for locator in all_locators if locator.trustworthy]
        return {
            "pages": len(pages),
            "components": len(self.components),
            "locators": len(all_locators),
            "trusted_locators": len(trusted),
            "avg_confidence": round(
                sum(locator.confidence for locator in all_locators) / len(all_locators), 3
            )
            if all_locators
            else 0.0,
            "endpoints": len(self.api_catalog()),
            "visually_stable_routes": len(self.visual_candidates()),
            "navigation_edges": len(self.navigation),
            "explored_at": self.explored_at,
            "app_version": self.app_version,
        }


# --------------------------------------------------------------------------- #
# Component detection
# --------------------------------------------------------------------------- #
#: Signatures of UI components worth registering as reusable.
_COMPONENT_SIGNATURES: dict[str, tuple[str, ...]] = {
    "NavigationBar": ("nav", "menu", "navbar", "sidebar"),
    "SearchBox": ("search", "filter", "query"),
    "Table": ("table", "grid", "row", "column"),
    "Pagination": ("pagination", "next page", "previous page", "page size"),
    "Modal": ("modal", "dialog", "popup"),
    "Toast": ("toast", "notification", "alert", "snackbar"),
    "DatePicker": ("date", "calendar", "datepicker"),
    "Dropdown": ("select", "dropdown", "combobox"),
    "FileUpload": ("upload", "attach", "file"),
    "LoginForm": ("username", "password", "sign in", "log in"),
}


def detect_components(pages: list[PageKnowledge]) -> dict[str, dict[str, Any]]:
    """Identify recurring UI components across pages.

    A component that appears on several pages is a reuse opportunity: generating
    a fresh Page Object for the same navigation bar five times is exactly the
    duplication the platform exists to prevent.
    """
    found: dict[str, dict[str, Any]] = {}

    for page in pages:
        for component, keywords in _COMPONENT_SIGNATURES.items():
            matches = [
                locator
                for locator in page.locators()
                if any(
                    keyword in f"{locator.name} {locator.role} {locator.locator}".lower()
                    for keyword in keywords
                )
            ]
            if not matches:
                continue
            entry = found.setdefault(
                component,
                {"name": component, "pages": [], "locators": [], "occurrences": 0},
            )
            if page.route not in entry["pages"]:
                entry["pages"].append(page.route)
            entry["occurrences"] += len(matches)
            for locator in matches:
                candidate = {
                    "name": locator.name,
                    "role": locator.role,
                    "locator": locator.locator,
                    "confidence": round(locator.confidence, 3),
                }
                if candidate not in entry["locators"]:
                    entry["locators"].append(candidate)

    # Only components seen on more than one page are genuinely shared.
    for entry in found.values():
        entry["shared"] = len(entry["pages"]) > 1
        entry["locators"] = entry["locators"][:20]
    return found


def dom_hash(elements: list[dict[str, Any]]) -> str:
    """Stable fingerprint of a page's interactive surface.

    Deliberately built from roles and names rather than raw HTML: cosmetic markup
    churn should not invalidate perfectly good locator knowledge, but a field
    appearing or disappearing should.
    """
    signature = sorted(
        f"{element.get('role', '')}:{element.get('name', '')}:{element.get('input_type', '')}"
        for element in elements
    )
    return hashlib.sha256("|".join(signature).encode("utf-8", "ignore")).hexdigest()[:24]
