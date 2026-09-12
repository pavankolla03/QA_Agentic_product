"""Application Exploration Agent.

Drives a real browser over the application under test to harvest **verified**
locators. This matters more than any prompt-engineering trick: a test written
against a locator that was observed in the live DOM passes; one written against a
locator the model imagined does not.

When no application is reachable the agent degrades honestly — it records that
locators are unverified, and the Code Generation Agent emits clearly-marked
TODO locators instead of plausible-looking fiction.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from agents.base import AgentContext, BaseAgent, json_block
from packages.aiqa_types.enums import AgentName, Capability
from packages.aiqa_types.models import (
    DiscoveredElement,
    DiscoveredWorkflow,
    ExplorationResult,
    PageSnapshot,
    WorkflowStep,
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


class ExplorationAgent(BaseAgent):
    name = AgentName.EXPLORATION
    capability = Capability.FAST
    description = "Crawls the application under test to capture verified locators and user workflows."
    optional = True          # a run can still produce value without a live app

    def progress(self, ctx: AgentContext) -> float:
        return 0.3

    def skip_reason(self, ctx: AgentContext) -> str:
        if not (ctx.project.base_url or ctx.metadata.get("target_url")):
            return "no base_url configured for this project — skipping live exploration"
        return ""

    async def run(self, ctx: AgentContext) -> None:
        base_url = ctx.metadata.get("target_url") or ctx.project.base_url or ""

        # Cheap reachability probe first: a 5-second failure beats a 60-second one.
        health = self.tool(ctx, "api.health", url=base_url)
        if health.ok and not (health.data or {}).get("reachable", False):
            ctx.warn(
                f"application at {base_url} is not reachable "
                f"({(health.data or {}).get('reason', 'unknown')}). Locators will be unverified."
            )
            ctx.exploration = ExplorationResult(
                project_id=ctx.project.id, base_url=base_url, simulated=True,
                notes="Application unreachable; no locators verified.",
            )
            ctx.metadata["locators_verified"] = False
            return

        paths = _candidate_paths(ctx)
        result = self.tool(
            ctx, "playwright.explore",
            base_url=base_url, paths=paths, max_pages=ctx.metadata.get("max_explore_pages", 6),
        )
        if not result.ok:
            ctx.warn(f"exploration failed: {result.error[:250]}")
            ctx.exploration = ExplorationResult(
                project_id=ctx.project.id, base_url=base_url, simulated=True, notes=result.error[:500]
            )
            ctx.metadata["locators_verified"] = False
            return

        data = result.data or {}
        snapshots = [PageSnapshot(**snap) for snap in data.get("snapshots", [])]
        exploration = ExplorationResult(
            project_id=ctx.project.id,
            base_url=base_url,
            snapshots=snapshots,
            unreachable=data.get("unreachable", []),
            simulated=bool(data.get("simulated", False)),
            notes="; ".join(data.get("errors", [])[:3]),
        )

        total_elements = sum(len(s.elements) for s in snapshots)
        verified = [
            e for s in snapshots for e in s.elements if e.recommended_locator and e.confidence >= 0.7
        ]
        ctx.metadata["locators_verified"] = bool(verified)
        ctx.metadata["locator_catalog"] = _locator_catalog(snapshots)

        if exploration.simulated:
            ctx.warn(
                "exploration used the HTTP fallback (no browser). Locators come from static HTML — "
                "verify any JavaScript-rendered elements."
            )

        # Ask the model to assemble journeys from the observed elements.
        if snapshots:
            exploration.workflows = await self._infer_workflows(ctx, snapshots)

        ctx.exploration = exploration
        ctx.note(
            f"explored {len(snapshots)} page(s), {total_elements} element(s), "
            f"{len(verified)} high-confidence locator(s), {len(exploration.workflows)} workflow(s)"
        )
        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = f"{len(snapshots)} pages, {len(verified)} verified locators"

    # ------------------------------------------------------------------ #
    async def _infer_workflows(self, ctx: AgentContext, snapshots: list[PageSnapshot]) -> list[DiscoveredWorkflow]:
        feature = ctx.requirement.title if ctx.requirement else ctx.instruction
        page_map: list[dict[str, Any]] = []
        for snapshot in snapshots[:6]:
            page_map.append(
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
            )

        user = (
            f"Feature to automate: {feature}\n\n"
            f"Application map:\n{json_block(page_map, limit=14000)}\n\n"
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
                # Reject hallucinated locators outright.
                if locator and locator not in valid_locators:
                    locator = ""
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
def _candidate_paths(ctx: AgentContext) -> list[str]:
    """Guess likely entry points from the feature name to keep the crawl focused."""
    paths = ["/"]
    title = (ctx.requirement.title if ctx.requirement else ctx.instruction) or ""
    words = [w.lower() for w in title.replace("-", " ").split() if len(w) > 3]
    for word in words[:3]:
        paths.extend([f"/{word}", f"/{word}s", f"/{word}/new"])
    for common in ("/login", "/dashboard"):
        if common not in paths:
            paths.append(common)
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique[:10]


def _locator_catalog(snapshots: list[PageSnapshot]) -> list[dict[str, Any]]:
    """Flat, prompt-friendly list of every verified locator, best first."""
    catalog: list[dict[str, Any]] = []
    for snapshot in snapshots:
        for element in snapshot.elements:
            if not element.recommended_locator:
                continue
            catalog.append(
                {
                    "page": snapshot.route_pattern or urlparse(snapshot.url).path or "/",
                    "name": element.name or element.placeholder or element.test_id or "",
                    "role": element.role,
                    "required": element.required,
                    "locator": element.recommended_locator,
                    "strategy": element.locator_strategy,
                    "confidence": element.confidence,
                }
            )
    catalog.sort(key=lambda item: item["confidence"], reverse=True)
    return catalog[:120]


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
            "name": f"{feature} — primary journey",
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
