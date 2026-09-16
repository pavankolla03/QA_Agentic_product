"""Application Exploration Agent — crawl once, reuse forever.

The naive design re-crawls the application for every scenario. Designing 500
scenarios against one app should explore it *once*.

This agent consults the persistent :class:`ApplicationMap` first and only visits
routes the map cannot vouch for — never seen, DOM changed, locators failed,
confidence decayed, TTL expired, or the app version moved. Everything else is
answered from cache at zero cost. Execution results are fed back so a locator
that keeps working gains confidence and one that breaks loses it.

When no application is reachable the agent degrades honestly: it records that
locators are unverified so Code Generation emits clearly-marked TODOs rather
than plausible fiction.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from agents.base import AgentContext, BaseAgent, json_block
from packages.aiqa_types.enums import AgentName, Capability
from packages.aiqa_types.models import (
    DiscoveredElement,
    DiscoveredWorkflow,
    ExplorationResult,
    PageSnapshot,
    WorkflowStep,
)
from services.knowledge_service.application_map import (
    ApplicationMap,
    LocatorKnowledge,
    PageKnowledge,
    detect_components,
    dom_hash,
    route_of,
)

SYSTEM = """You identify end-to-end user workflows from a structural map of an application's pages.

You are given real pages with their real interactive elements. Infer the journeys a QA engineer would
automate for the stated feature, using ONLY the elements listed.

Reply with ONE JSON object:
{"workflows": [{"name": str, "description": str, "entry_url": str, "confidence": float,
  "steps": [{"order": int, "action": "navigate|fill|click|select|assert|wait|upload",
             "target": str, "value": str, "description": str, "locator": str}]}]}

`locator` must be copied verbatim from a `recommended_locator` in the input, or left empty.
Never invent a locator."""



#: A path someone typed: a leading slash, then path-ish characters. Deliberately
#: strict — `/` alone and anything with a space is not a route worth crawling.
_EXPLICIT_PATH = re.compile(r"(?<![\w/])(/[A-Za-z0-9][A-Za-z0-9._\-/]{0,60})")



def _requirement_text(ctx: AgentContext) -> str:
    """Everything the user actually wrote, for pulling explicit paths out of.

    `raw_input` is the instruction verbatim, which is the only field certain to
    still contain a path the user typed; a summary may well have paraphrased it
    away.
    """
    parts = [ctx.instruction or ""]
    requirement = ctx.requirement
    if requirement is not None:
        parts += [requirement.raw_input, requirement.summary, *requirement.preconditions]
        parts += [criterion.text for criterion in requirement.acceptance_criteria]
    return " ".join(part for part in parts if part)


def _explicit_paths(text: str) -> list[str]:
    """Paths named outright in the request, in the order they appear."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _EXPLICIT_PATH.finditer(text or ""):
        route = match.group(1).rstrip(".,;:)")
        if route not in seen:
            seen.add(route)
            out.append(route)
    return out[:6]


class ExplorationAgent(BaseAgent):
    name = AgentName.EXPLORATION
    capability = Capability.CHEAP     # the browser does the work; the model only picks targets
    description = "Crawls the application under test, caching pages and locators in the Application Map."
    optional = True                   # a run can still produce value without a live app

    def progress(self, ctx: AgentContext) -> float:
        return 0.3

    def skip_reason(self, ctx: AgentContext) -> str:
        if not (ctx.project.base_url or ctx.metadata.get("target_url")):
            return "no base_url configured for this project - skipping live exploration"
        return ""

    async def run(self, ctx: AgentContext) -> None:
        base_url = ctx.metadata.get("target_url") or ctx.project.base_url or ""
        root = ctx.project_root

        app_map = ApplicationMap.load_or_new(root, project_id=ctx.project.id, base_url=base_url)
        ctx.application_map = app_map

        candidate_routes = self._candidate_routes(ctx, app_map)
        failed_locators = set(ctx.metadata.get("failed_locators") or [])

        # A real browser can improve on a previous HTTP-only capture; without
        # one, re-crawling would just re-read the same static HTML.
        browser_available = bool((ctx.toolchain or {}).get("playwright_installed"))
        to_explore, cached = app_map.plan_exploration(
            candidate_routes,
            app_version=ctx.metadata.get("app_version", ""),
            failed_locators=failed_locators,
            browser_available=browser_available,
        )

        if cached:
            ctx.note(
                f"application map hit for {len(cached)} route(s) - skipping crawl: "
                + "; ".join(f"{route} ({reason})" for route, reason in list(cached.items())[:4])
            )
            saved = sum(
                len(app_map.page(route).elements) if app_map.page(route) else 0 for route in cached
            )
            if hasattr(ctx.budget, "record_saving"):
                ctx.budget.record_saving(saved * 40)   # ~40 tokens per element description

        if not to_explore:
            ctx.exploration = self._result_from_map(ctx, app_map, base_url, crawled=[])
            ctx.metadata["locators_verified"] = bool(app_map.catalog())
            ctx.metadata["locator_catalog"] = app_map.catalog()
            ctx.note(
                f"no crawl needed - {len(app_map.pages)} page(s) already known "
                f"({len(ctx.metadata['locator_catalog'])} trusted locators)"
            )
            self._finish(ctx, app_map)
            return

        # ---- reachability probe before spending time on a browser ------- #
        health = self.tool(ctx, "api.health", url=base_url)
        if health.ok and not (health.data or {}).get("reachable", False):
            reason = (health.data or {}).get("reason", "unknown")
            ctx.warn(f"application at {base_url} is not reachable ({reason}). Locators will be unverified.")
            ctx.exploration = self._result_from_map(
                ctx, app_map, base_url, crawled=[], notes="Application unreachable; no locators verified."
            )
            catalog = app_map.catalog()
            ctx.metadata["locator_catalog"] = catalog
            ctx.metadata["locators_verified"] = bool(catalog)
            if catalog:
                ctx.note(f"falling back to {len(catalog)} cached locator(s) from a previous crawl")
            self._finish(ctx, app_map)
            return

        # ---- crawl only what the map could not vouch for ---------------- #
        ctx.note(f"crawling {len(to_explore)} route(s): {', '.join(to_explore[:6])}")
        result = self.tool(
            ctx, "playwright.explore",
            base_url=base_url, paths=to_explore,
            max_pages=min(len(to_explore) + 2, ctx.metadata.get("max_explore_pages", 8)),
        )
        if not result.ok:
            ctx.warn(f"exploration failed: {result.error[:250]}")
            ctx.exploration = self._result_from_map(ctx, app_map, base_url, crawled=[], notes=result.error[:500])
            ctx.metadata["locator_catalog"] = app_map.catalog()
            ctx.metadata["locators_verified"] = bool(ctx.metadata["locator_catalog"])
            self._finish(ctx, app_map)
            return

        data = result.data or {}
        snapshots = [PageSnapshot(**snap) for snap in data.get("snapshots", [])]
        simulated = bool(data.get("simulated", False))

        # The probe reports failures inside a successful result, and they were
        # being read as an empty crawl. A missing browser binary produced
        # "crawled 0 page(s)" with a green tick, so every locator downstream
        # came from nothing and nobody was told.
        probe_errors = [str(e) for e in data.get("errors", []) if str(e).strip()]
        browser = str(data.get("browser", "") or "")
        if browser:
            ctx.note(f"explored with {browser}")
        if not snapshots:
            detail = probe_errors[0][:200] if probe_errors else "the probe returned no pages"
            ctx.warn(
                f"exploration reached no pages ({detail}). Locators cannot be verified, "
                "so anything generated from here is scaffolding, not tested automation."
            )
        elif probe_errors:
            # Routes that failed individually still matter: they are the pages
            # the plan will silently skip.
            for problem in probe_errors[:4]:
                ctx.warn(f"exploration: {problem[:200]}")

        for snapshot in snapshots:
            app_map.put_page(self._to_page_knowledge(snapshot, simulated=simulated))
        app_map.unreachable = data.get("unreachable", [])[:20]

        # ---- component registry ---------------------------------------- #
        pages = [PageKnowledge(**raw) for raw in app_map.pages.values()]
        app_map.components = detect_components(pages)
        shared = [name for name, entry in app_map.components.items() if entry.get("shared")]
        if shared:
            ctx.note(f"reusable components detected across pages: {', '.join(shared[:8])}")

        catalog = app_map.catalog()
        ctx.metadata["locator_catalog"] = catalog
        ctx.metadata["locators_verified"] = bool(catalog)
        ctx.metadata["components"] = app_map.components

        if simulated:
            ctx.warn(
                "exploration used the HTTP fallback (no browser). Locators come from static HTML - "
                "verify any JavaScript-rendered elements."
            )

        exploration = self._result_from_map(
            ctx, app_map, base_url, crawled=[s.url for s in snapshots], simulated=simulated
        )
        if snapshots:
            exploration.workflows = await self._infer_workflows(ctx, snapshots)
        ctx.exploration = exploration

        ctx.note(
            f"crawled {len(snapshots)} page(s); map now holds {len(app_map.pages)} route(s), "
            f"{len(catalog)} trusted locator(s), {len(app_map.components)} component(s)"
        )
        self._finish(ctx, app_map)

    # ------------------------------------------------------------------ #
    def _finish(self, ctx: AgentContext, app_map: ApplicationMap) -> None:
        app_map.save(ctx.project_root)
        # Only a JSON-safe summary belongs in metadata (it is persisted).
        ctx.metadata["application_map_stats"] = app_map.stats()
        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    stats = app_map.stats()
                    trace.output_summary = (
                        f"{stats['pages']} pages known, {stats['trusted_locators']} trusted locators"
                    )

    @staticmethod
    def _to_page_knowledge(snapshot: PageSnapshot, simulated: bool) -> PageKnowledge:
        elements = [
            asdict(
                LocatorKnowledge(
                    name=element.name or element.placeholder or element.test_id or "",
                    role=element.role,
                    locator=element.recommended_locator,
                    strategy=element.locator_strategy,
                    confidence=element.confidence,
                    required=element.required,
                    input_type=element.input_type,
                    alternatives=element.alternatives[:3],
                    source="http_probe" if simulated else "exploration",
                )
            )
            for element in snapshot.elements
            if element.recommended_locator
        ]
        return PageKnowledge(
            route=snapshot.route_pattern or route_of(snapshot.url),
            url=snapshot.url,
            title=snapshot.title,
            dom_hash=dom_hash([e for e in elements]),
            elements=elements,
            forms=snapshot.forms[:8],
            navigations=snapshot.navigations[:40],
            screenshot_path=snapshot.screenshot_path,
            simulated=simulated,
        )

    def _result_from_map(
        self,
        ctx: AgentContext,
        app_map: ApplicationMap,
        base_url: str,
        crawled: list[str],
        notes: str = "",
        simulated: bool = False,
    ) -> ExplorationResult:
        """Present the map as an ExplorationResult so downstream agents are unchanged."""
        snapshots: list[PageSnapshot] = []
        for raw in app_map.pages.values():
            page = PageKnowledge(**raw)
            snapshots.append(
                PageSnapshot(
                    url=page.url,
                    title=page.title,
                    route_pattern=page.route,
                    elements=[
                        DiscoveredElement(
                            role=locator.role, name=locator.name, tag="",
                            test_id=None, label=None, placeholder=None, text=None,
                            input_type=locator.input_type, required=locator.required,
                            recommended_locator=locator.locator,
                            locator_strategy=locator.strategy,
                            confidence=locator.confidence,
                            alternatives=locator.alternatives,
                        )
                        for locator in page.locators()
                    ],
                    forms=page.forms,
                    navigations=page.navigations,
                    screenshot_path=page.screenshot_path,
                    dom_hash=page.dom_hash,
                )
            )
        return ExplorationResult(
            project_id=ctx.project.id,
            base_url=base_url,
            snapshots=snapshots,
            unreachable=app_map.unreachable,
            simulated=simulated,
            notes=notes or f"{len(crawled)} route(s) crawled this run; {len(snapshots)} known in total",
        )

    # ------------------------------------------------------------------ #
    def _candidate_routes(self, ctx: AgentContext, app_map: ApplicationMap) -> list[str]:
        """Guess likely entry points from the feature name, plus what we already know.

        A path the user wrote down goes first and is not a guess. Asking for
        "the registration form at /residents/new" and then crawling
        `/registration`, `/registrations` and `/resident/new` — inflections of
        the *words* — while never visiting the path in the sentence is a
        strange way to treat the one piece of certain information available.
        """
        explicit = _explicit_paths(_requirement_text(ctx))
        routes: list[str] = [*explicit, "/"]
        title = (ctx.requirement.title if ctx.requirement else ctx.instruction) or ""
        words = [w.lower() for w in title.replace("-", " ").split() if len(w) > 3]
        for word in words[:3]:
            routes.extend([f"/{word}", f"/{word}s", f"/{word}/new"])
        for common in ("/login", "/dashboard"):
            routes.append(common)
        # Known routes are cheap to include: the map answers them without a crawl.
        routes.extend(app_map.known_routes())

        seen: set[str] = set()
        unique: list[str] = []
        for route in routes:
            if route not in seen:
                seen.add(route)
                unique.append(route)
        return unique[:14]

    # ------------------------------------------------------------------ #
    async def _infer_workflows(self, ctx: AgentContext, snapshots: list[PageSnapshot]) -> list[DiscoveredWorkflow]:
        feature = ctx.requirement.title if ctx.requirement else ctx.instruction
        page_map: list[dict[str, Any]] = [
            {
                "url": snapshot.url,
                "route": snapshot.route_pattern,
                "title": snapshot.title,
                "forms": snapshot.forms[:4],
                "elements": [
                    {
                        "role": element.role,
                        "name": element.name,
                        "required": element.required,
                        "input_type": element.input_type,
                        "recommended_locator": element.recommended_locator,
                    }
                    for element in snapshot.elements[:40]
                    if element.recommended_locator
                ],
            }
            for snapshot in snapshots[:6]
        ]

        user = (
            f"Feature to automate: {feature}\n\n"
            f"Application map:\n{json_block(page_map, limit=12000)}\n\n"
            "Identify the workflows worth automating for this feature."
        )
        raw = await self.ask_json(
            ctx, SYSTEM, user, task="exploration.workflows",
            fallback={"workflows": _fallback_workflows(snapshots, feature)}, max_tokens=2500,
        )

        valid_locators = {
            element.recommended_locator
            for snapshot in snapshots
            for element in snapshot.elements
            if element.recommended_locator
        }
        workflows: list[DiscoveredWorkflow] = []
        for item in (raw or {}).get("workflows", []) or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            steps: list[WorkflowStep] = []
            for index, raw_step in enumerate(item.get("steps", []) or [], start=1):
                if not isinstance(raw_step, dict):
                    continue
                locator = str(raw_step.get("locator") or "")
                if locator and locator not in valid_locators:
                    locator = ""          # reject hallucinated locators outright
                steps.append(
                    WorkflowStep(
                        order=int(raw_step.get("order", index) or index),
                        action=str(raw_step.get("action", "click")),
                        target=str(raw_step.get("target", "")),
                        value=str(raw_step.get("value")) if raw_step.get("value") is not None else None,
                        description=str(raw_step.get("description", "")),
                        locator=locator or None,
                    )
                )
            workflows.append(
                DiscoveredWorkflow(
                    name=str(item["name"])[:160],
                    description=str(item.get("description", ""))[:600],
                    entry_url=str(item.get("entry_url", snapshots[0].url if snapshots else "")),
                    steps=sorted(steps, key=lambda s: s.order),
                    pages_touched=[s.url for s in snapshots[:4]],
                    confidence=_clamp(item.get("confidence", 0.5)),
                )
            )
        return workflows


# --------------------------------------------------------------------------- #
def _fallback_workflows(snapshots: list[PageSnapshot], feature: str) -> list[dict[str, Any]]:
    """Build a workflow from the largest observed form, without a model."""
    best: PageSnapshot | None = None
    best_inputs = 0
    for snapshot in snapshots:
        inputs = sum(1 for e in snapshot.elements if e.role in ("textbox", "combobox", "checkbox"))
        if inputs > best_inputs:
            best, best_inputs = snapshot, inputs
    if best is None:
        return []

    steps: list[dict[str, Any]] = [
        {"order": 1, "action": "navigate", "target": best.route_pattern or best.url,
         "description": f"Open {best.title or best.url}", "locator": ""}
    ]
    order = 2
    for element in best.elements:
        if element.role in ("textbox", "combobox") and element.recommended_locator:
            steps.append(
                {
                    "order": order,
                    "action": "select" if element.role == "combobox" else "fill",
                    "target": element.name or element.placeholder or "field",
                    "value": "<test data>",
                    "description": f"Provide {element.name or 'value'}",
                    "locator": element.recommended_locator,
                }
            )
            order += 1
    submit = next((e for e in best.elements if e.role == "button" and e.recommended_locator), None)
    if submit:
        steps.append({"order": order, "action": "click", "target": submit.name or "submit",
                      "description": "Submit the form", "locator": submit.recommended_locator})
        order += 1
    steps.append({"order": order, "action": "assert", "target": "confirmation",
                  "description": "Verify the operation succeeded", "locator": ""})

    return [
        {
            "name": f"{feature} - primary journey",
            "description": "Derived from the largest observed form (no model inference available).",
            "entry_url": best.url,
            "confidence": 0.5,
            "steps": steps,
        }
    ]


def _clamp(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
