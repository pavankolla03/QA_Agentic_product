"""Page objects and bindings for a plan the crawl already decided.

The scenarios autopilot writes use a fixed vocabulary, and a fixed vocabulary
needs a fixed set of methods behind it. Asking a model to invent those was the
last place invention could get in, and it duly did: a run planned fifteen good
scenarios and blocked twenty of their steps, because the generated page objects
had no `goto`, no way to sign in, and nothing that could say "we are still on
this page". The plan was right and none of it ran.

Nothing here is guesswork. The route, the elements, their roles and which ones
the application marks required all came off the page during the crawl, and the
step text came from this platform two stages ago. When both ends are known, the
mapping between them is a lookup, not a judgement — so it is written down once
here instead of being re-derived per run by a model that may be having a bad
day.

The credentials are the one thing that must not be written down. `signIn()`
reads them from the environment at run time, using the same variable names the
crawler reads, so a generated suite can be committed and run in CI without a
password ever entering the repository.
"""

from __future__ import annotations

import re
from typing import Any

from agents.code_generation.renderer import (
    GenerationPlan,
    MethodPlan,
    PagePlan,
    StepPlan,
    camel,
    pascal,
    safe_identifier,
)
from packages.aiqa_types.models import TestPlan
from services.discovery.autopilot import KIND_AUTH, KIND_LISTING, DiscoveredFeature

#: Roles that accept a typed or chosen value.
_FILLABLE = ("textbox", "combobox", "checkbox", "radio", "spinbutton")
_SUBMIT_WORDS = ("submit", "save", "sign in", "log in", "login", "create", "add", "send", "continue", "search")

_ENTER_RE = re.compile(r'^I enter "(?P<value>[^"]*)" in the (?P<field>.+) field$')
_ON_PAGE_RE = re.compile(r"^I am on the (?P<page>.+) page$")
_LEFT_RE = re.compile(r"^I am taken away from the (?P<page>.+) page$")
_STILL_RE = re.compile(r"^I am still on the (?P<page>.+) page$")
_TITLE_RE = re.compile(r'^the page title is "(?P<title>[^"]*)"$')


def generation_plan(
    features: list[DiscoveredFeature],
    plan: TestPlan,
    catalog: list[dict[str, Any]],
    *,
    base_class: str = "",
    page_titles: dict[str, str] | None = None,
) -> GenerationPlan:
    """Everything the rendered suite needs, derived rather than designed."""
    titles = page_titles or {}
    pages: dict[str, PagePlan] = {}
    by_page_name: dict[str, PagePlan] = {}

    for feature in features:
        page = _page_plan(feature, catalog, base_class=base_class, title=titles.get(feature.route, ""))
        if page is None:
            continue
        pages[feature.route] = page
        by_page_name[_page_name(feature)] = page

    steps = _bind_steps(plan, by_page_name)
    return GenerationPlan(pages=list(pages.values()), steps=steps)


# --------------------------------------------------------------------------- #
def _page_plan(
    feature: DiscoveredFeature,
    catalog: list[dict[str, Any]],
    *,
    base_class: str,
    title: str,
) -> PagePlan | None:
    entries = [item for item in catalog if item.get("page") == feature.route]
    if not entries:
        return None

    locators: dict[str, str] = {}
    roles: dict[str, str] = {}
    props: dict[str, str] = {}          # element name -> property name
    for entry in entries:
        name = str(entry.get("name") or "")
        expression = str(entry.get("locator") or "")
        if not name or not expression:
            continue
        prop = _unique(safe_identifier(name, fallback="element"), locators)
        locators[prop] = expression
        roles[prop] = str(entry.get("role") or "")
        props.setdefault(name, prop)

    methods: list[MethodPlan] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        prop = props.get(name)
        if not prop or roles.get(prop) not in _FILLABLE:
            continue
        methods.append(
            MethodPlan(
                name=f"enter{pascal(name)}",
                kind="action",
                params=["value"],
                locators=[prop],
                intent=f"Type into the {name} field.",
            )
        )

    submit = _submit_property(entries, props, roles)
    if submit:
        methods.append(
            MethodPlan(name="submitForm", kind="action", locators=[submit],
                       intent="Submit the form on this page.")
        )

    # The page-level checks every scenario ends with. They reference no element,
    # which is exactly why nothing else could generate them.
    methods.append(MethodPlan(name="expectHere", kind="page_assertion", expect="here",
                              intent="Still on this page — what a rejected submission looks like."))
    methods.append(MethodPlan(name="expectLeft", kind="page_assertion", expect="left",
                              intent="No longer on this page — what an accepted one looks like."))
    if title:
        methods.append(
            MethodPlan(name="expectTitle", kind="page_assertion", expect="title", params=["title"],
                       intent="The title this page showed during exploration.")
        )

    if feature.kind == KIND_AUTH:
        methods.append(
            MethodPlan(
                name="signIn", kind="credentials",
                intent="Sign in with the credentials in the environment.",
                locators=[p for p in (_identifier_property(entries, props, roles), _password_property(entries, props)) if p],
            )
        )
        methods.append(
            MethodPlan(
                name="signInWithWrongPassword", kind="credentials", expect="wrong",
                intent="Sign in with a deliberately incorrect password.",
                locators=[p for p in (_identifier_property(entries, props, roles), _password_property(entries, props)) if p],
            )
        )
    if feature.kind == KIND_LISTING:
        row = next((prop for prop, role in roles.items() if role in ("row", "listitem")), "")
        if row:
            methods.append(
                MethodPlan(name="expectAtLeastOneRow", kind="assertion", locators=[row],
                           expect="toBeVisible", intent="The list rendered its rows.")
            )

    return PagePlan(
        class_name=_class_name(feature),
        route=feature.route,
        base_class=base_class,
        locators=locators,
        roles=roles,
        methods=methods,
        description=feature.rationale[:160],
        route_source="model",           # observed, which is stronger than named
        title=title,
    )


def _bind_steps(plan: TestPlan, by_page_name: dict[str, PagePlan]) -> list[StepPlan]:
    """Map each step in the plan onto the method that performs it.

    Both ends were produced here, so every shape is known. A step that does not
    match one is left unbound on purpose rather than approximated — the
    step-coverage stage exists to report exactly that, and silently binding it
    to something close is how a suite ends up testing the wrong control.
    """
    steps: list[StepPlan] = []
    seen: set[str] = set()
    current: PagePlan | None = None

    for spec in plan.features:
        # Background first, and on the same footing as any other step: Cucumber
        # runs it before every scenario, so it needs definitions like the rest.
        # A Background whose steps are undefined fails every scenario in the
        # file before the first assertion.
        for scenario_steps in [spec.background, *(s.steps for s in spec.scenarios)]:
            for step in scenario_steps:
                text = step.text
                on_page = _ON_PAGE_RE.match(text)
                if on_page:
                    current = by_page_name.get(on_page.group("page"))
                    if current and text not in seen:
                        seen.add(text)
                        steps.append(
                            StepPlan(text=text, keyword=step.keyword,
                                     page=current.class_name, setup=True)
                        )
                    continue

                if current is None or text in seen:
                    continue
                call = _call_for(text, current)
                if call is None:
                    continue
                seen.add(text)
                steps.append(
                    StepPlan(text=text, keyword=step.keyword, page=current.class_name, call=call)
                )
    return steps


def _call_for(text: str, page: PagePlan) -> str | None:
    enter = _ENTER_RE.match(text)
    if enter:
        method = f"enter{pascal(enter.group('field'))}"
        return f"{method}(value1)" if _has(page, method) else None

    if text == "I submit the form":
        return "submitForm()" if _has(page, "submitForm") else None
    if text == "I sign in with valid credentials":
        return "signIn()" if _has(page, "signIn") else None
    if text == "I sign in with an incorrect password":
        return "signInWithWrongPassword()" if _has(page, "signInWithWrongPassword") else None
    if text == "I should see at least one row":
        return "expectAtLeastOneRow()" if _has(page, "expectAtLeastOneRow") else None
    if _LEFT_RE.match(text):
        return "expectLeft()"
    if _STILL_RE.match(text):
        return "expectHere()"
    if _TITLE_RE.match(text):
        return "expectTitle(value1)" if _has(page, "expectTitle") else None
    return None


# --------------------------------------------------------------------------- #
def _has(page: PagePlan, method: str) -> bool:
    return any(m.name == method for m in page.methods)


def _class_name(feature: DiscoveredFeature) -> str:
    return f"{pascal(_page_name(feature))}Page"


def _page_name(feature: DiscoveredFeature) -> str:
    name = re.split(r"\s+-\s+", feature.name)[0].strip()
    return name or (feature.route.strip("/") or "home")


def _unique(candidate: str, taken: dict[str, str]) -> str:
    if candidate not in taken:
        return candidate
    index = 2
    while f"{candidate}{index}" in taken:
        index += 1
    return f"{candidate}{index}"


def _submit_property(
    entries: list[dict[str, Any]], props: dict[str, str], roles: dict[str, str]
) -> str:
    """The control that submits this form.

    A button whose text says so, else any button. Falling back to "any button"
    is deliberate: a form with one button is submitted by that button whatever
    it is captioned, and refusing to bind would cost the whole scenario.
    """
    buttons = [
        props[str(entry.get("name"))]
        for entry in entries
        if str(entry.get("name")) in props and roles.get(props[str(entry.get("name"))]) == "button"
    ]
    for entry in entries:
        name = str(entry.get("name") or "")
        prop = props.get(name)
        if prop and roles.get(prop) == "button" and any(word in name.lower() for word in _SUBMIT_WORDS):
            return prop
    return buttons[0] if buttons else ""


def _identifier_property(
    entries: list[dict[str, Any]], props: dict[str, str], roles: dict[str, str]
) -> str:
    for entry in entries:
        name = str(entry.get("name") or "")
        prop = props.get(name)
        if not prop or roles.get(prop) != "textbox":
            continue
        if "password" in name.lower():
            continue
        return prop
    return ""


def _password_property(entries: list[dict[str, Any]], props: dict[str, str]) -> str:
    for entry in entries:
        name = str(entry.get("name") or "")
        if "password" in name.lower() and name in props:
            return props[name]
    return ""


def describe(generation: GenerationPlan) -> str:
    bound = sum(1 for step in generation.steps if step.call or step.setup)
    return (
        f"{len(generation.pages)} page object(s) and {bound} bound step(s), "
        "every one of them from an element the crawl observed"
    )


__all__ = ["generation_plan", "describe", "camel"]
