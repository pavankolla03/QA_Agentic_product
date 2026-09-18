"""Test Design Agent — the artifact a human approves.

Produces a risk-based :class:`TestPlan` of Gherkin scenarios traced back to
acceptance criteria. Coverage gaps are detected deterministically afterwards, so
"the model forgot the negative case" is caught by code rather than hoped away.

This is the first human approval gate: nothing is written to the workspace until
the plan is accepted.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from agents.base import AgentContext, BaseAgent
from configs.settings import load_model_config
from packages.aiqa_types.enums import (
    AgentName,
    ApprovalKind,
    Capability,
    Priority,
    RiskLevel,
    TestLayer,
)
from packages.aiqa_types.models import FeatureSpec, GherkinStep, Scenario, TestPlan
from services.discovery.autopilot import DiscoveredFeature
from services.discovery.scenarios import describe, plan_from_features

if TYPE_CHECKING:  # imported lazily at runtime to keep the knowledge service optional
    from services.knowledge_service.test_knowledge import TestKnowledge

SYSTEM = """You are a principal QA automation architect designing an executable BDD test suite.

Design principles:
- Risk-based, not exhaustive. Every scenario must justify its runtime.
- One happy path. Then validation, negative, boundary (as a Scenario Outline with Examples), \
permission and persistence cases as the criteria warrant.
- Declarative Gherkin: "When I submit valid resident details", never "When I click #btn-submit". \
No locators, URLs or technical selectors in step text.
- Reuse step text VERBATIM when an existing step already expresses the same intent — this is how \
step-definition reuse actually happens.
- Each scenario maps to at least one acceptance criterion id.
- Tag everything: @smoke for the critical path, @regression otherwise, plus @P1/@P2/@P3 and @negative.
- Prefer API or DB verification over UI assertions for data-level checks. These become real
  test files, so an api_check may only use a `path` from the observed endpoint list below;
  one that was never observed is discarded.

Reply with ONE JSON object. Write a step as a single string beginning with its keyword.
Do NOT emit test ids, tags, file names or a data_driven flag - those are derived, and
inventing them only costs you output:
{"title": str, "strategy": str,
 "features": [{"name": str, "desc": str,
   "background": ["Given ...", "And ..."],
   "scenarios": [{"name": str, "p": "P0|P1|P2|P3", "layer": "ui|api|db", "neg": bool,
     "criteria": [str],
     "steps": ["Given ...", "When ...", "Then ..."],
     "examples": [{"column": "value"}]}]}],
 "new_pages": [str], "reuse_pages": [str], "reuse_fixtures": [str],
 "api_checks": [{"name": str, "method": "GET|POST|PUT|PATCH|DELETE", "path": str,
                 "expect_status": int, "asserts": [str], "criteria": [str], "negative": bool}],
 "db_checks": [{"name": str, "table": str, "where": str, "expect_rows": int,
                "columns": [str], "criteria": [str]}],
 "risks": [str], "notes": str}"""


class TestDesignAgent(BaseAgent):
    name = AgentName.TEST_DESIGN
    capability = Capability.REASONING
    description = "Designs a risk-based BDD test plan traced to acceptance criteria."

    def progress(self, ctx: AgentContext) -> float:
        return 0.42

    async def run(self, ctx: AgentContext) -> None:
        requirement = ctx.requirement
        if requirement is None:
            raise ValueError("test design requires a requirement")

        # An autopilot run has no requirement of its own: the crawl was supposed
        # to supply one. Reaching here with nothing means the application was
        # never successfully read, and designing tests from an empty requirement
        # would mean inventing every one of them against a site we never saw.
        # Failing here is what stops that becoming a green run full of fiction.
        if ctx.metadata.get("autopilot") and not requirement.acceptance_criteria:
            target = ctx.metadata.get("target_url") or ctx.project.base_url or "the application"
            raise ValueError(
                f"nothing testable was discovered at {target}, so there is nothing to design. "
                "Check that the URL is reachable from this machine and that it serves HTML; "
                "the exploration warnings above say what was attempted."
            )

        # ---- autopilot: the crawl already decided what the tests are ---- #
        # Asking a model to turn observed features into Gherkin is where the
        # invention creeps back in. It produced "the resident should exist in
        # the database" for an application with no database step, "the reports
        # page should load" which asserts nothing, and a Scenario Outline whose
        # placeholder was `{string}` with no Examples table. Rendering the plan
        # from the features costs nothing and cannot describe a page that was
        # never seen.
        plan = self._plan_from_discovery(ctx)

        # ---- 0. reuse discovery (deterministic, free) ------------------- #
        # Ask what we already have before paying a reasoning model to invent it.
        reuse = self._discover_reuse(ctx) if plan is None else {}

        # ---- 1. can memory answer this outright? ------------------------ #
        # The cheapest call is the one never made. If every testable criterion
        # is already expressed by a remembered scenario, the design is a lookup.
        if plan is None:
            plan = self._plan_from_memory(ctx)
        if plan is None:
            # ---- one batched design call for every scenario -------------- #
            # Designing six scenarios in six calls costs six times as much and
            # produces a less coherent suite, because no call sees the others.
            user = self._build_prompt(ctx, reuse)
            # The house standards are instructions, not per-request data, and
            # they are byte-identical between runs. Putting them in the system
            # prompt turns them into a cacheable prefix instead of context that
            # is re-billed on every call.
            prefix = ctx.metadata.get("standards_prefix", "")
            system = SYSTEM + (f"\n\n{prefix}" if prefix else "")
            raw = await self.ask_json(
                ctx, system, user,
                task="test_design.plan",
                fallback=_fallback_plan(ctx),
                # A 12-scenario plan does not fit in 6k, and a plan cut off
                # mid-scenario is discarded entirely — the expensive call is
                # wasted. Room is cheaper than a retry.
                max_tokens=9000,
                cacheable_prefix_chars=len(system),
            )
            plan = _to_plan(raw, ctx)

        # ---- 2. drop scenarios the suite already covers ----------------- #
        self._drop_duplicates(ctx, plan)

        # Deterministic quality gates — the model is a drafter, not the authority.
        gaps = _coverage_gaps(plan, ctx)
        if gaps:
            plan.coverage_notes = (plan.coverage_notes + "\n" if plan.coverage_notes else "") + \
                "Detected gaps: " + "; ".join(gaps)
            for gap in gaps:
                ctx.warn(f"test-plan gap: {gap}")

        _normalise_ids(plan, ctx)
        _ensure_tags(plan)
        ctx.test_plan = plan

        ctx.note(
            f"designed {plan.scenario_count} scenario(s) across {len(plan.features)} feature file(s); "
            f"reusing {len(plan.page_objects_reused)} page object(s), {len(plan.fixtures_reused)} fixture(s)"
        )

        # Publish the plan itself, so a client can show *what was decided*
        # rather than only that a decision happened. Without this the chat can
        # say "test_design finished" and nothing about the suite it designed.
        if ctx.tracker is not None:
            ctx.tracker.emit(
                "plan_ready",
                f"{plan.scenario_count} scenario(s) planned",
                agent=self.name,
                data={
                    "title": plan.title,
                    "strategy": plan.strategy[:600],
                    "reused_pages": list(plan.page_objects_reused)[:10],
                    "new_pages": list(plan.page_objects_needed)[:10],
                    "features": [
                        {
                            "name": feature.name,
                            "file": feature.file_name,
                            "scenarios": [
                                {
                                    "id": s.test_id,
                                    "name": s.name,
                                    "tags": list(s.tags),
                                    "priority": s.priority.value,
                                    "negative": s.negative,
                                    "steps": [step.render() for step in s.steps],
                                }
                                for s in feature.scenarios
                            ],
                        }
                        for feature in plan.features
                    ],
                },
            )
        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = f"{plan.scenario_count} scenarios"

        # ---- Human approval gate -------------------------------------- #
        preview = "\n\n".join(feature.to_gherkin() for feature in plan.features)
        self.request_approval(
            ctx,
            ApprovalKind.TEST_PLAN,
            title=f"Approve test plan: {plan.title} ({plan.scenario_count} scenarios)",
            description=(
                f"{plan.strategy}\n\n"
                f"New page objects: {', '.join(plan.page_objects_needed) or 'none'}\n"
                f"Reused: {', '.join(plan.page_objects_reused) or 'none'}\n"
                f"Risks: {'; '.join(plan.risks) or 'none noted'}"
            ),
            risk=RiskLevel.LOW,
            payload={"plan": plan.model_dump(mode="json")},
            diff_preview=preview,
        )

    # ------------------------------------------------------------------ #
    def _plan_from_discovery(self, ctx: AgentContext) -> TestPlan | None:
        """The plan the crawl already implies, for an autopilot run.

        Only for autopilot, and only when exploration actually found something.
        A run where somebody described the feature they want has information the
        crawl does not, and a model is the right tool for reading it.
        """
        if not ctx.metadata.get("autopilot"):
            return None
        raw = ctx.metadata.get("discovered_features") or []
        features = [DiscoveredFeature(**entry) for entry in raw if isinstance(entry, dict)]
        if not features:
            return None

        plan = plan_from_features(
            features,
            run_id=ctx.run_id,
            requirement_id=ctx.requirement.id if ctx.requirement else "",
            base_url=ctx.metadata.get("target_url") or ctx.project.base_url or "",
            # The crawler needed an account to see these pages, so the suite
            # needs one too, before every scenario.
            signed_in=bool(ctx.metadata.get("signed_in")),
        )
        summary = describe(plan)
        ctx.note(
            f"planned {summary['scenarios']} scenario(s) directly from {len(features)} discovered "
            f"feature(s) — no model call, and nothing that describes a page the crawl did not see"
        )
        return plan

    def _discover_reuse(self, ctx: AgentContext) -> dict[str, Any]:
        """What the platform already knows that answers part of this request.

        Pure lookup against the Test Knowledge Store, the repository map and the
        QA graph. No model call, so it is always worth doing first.
        """
        from services.knowledge_service.test_knowledge import QAKnowledgeGraph, TestKnowledgeStore

        requirement = ctx.requirement
        query = " ".join(
            filter(None, [ctx.instruction, requirement.title if requirement else "",
                          " ".join(c.text for c in (requirement.acceptance_criteria if requirement else [])[:5])])
        )

        store = TestKnowledgeStore(ctx.project.id)
        ctx.test_knowledge = store
        similar = store.find_similar(query, limit=5)
        assets = store.reusable_assets(query)

        graph = QAKnowledgeGraph(ctx.project.id)
        ctx.knowledge_graph = graph
        graph_context = graph.context_for(query, depth=2)

        # Anything the repository already contains is reuse, not new work.
        if ctx.repo_profile:
            for page in ctx.repo_profile.symbols_of("page_object"):
                if page.name not in assets["page_objects"]:
                    assets["page_objects"].append(page.name)
            for fixture in ctx.repo_profile.symbols_of("fixture"):
                if fixture.name not in assets["fixtures"]:
                    assets["fixtures"].append(fixture.name)
        if ctx.application_map is not None:
            for name, entry in (ctx.application_map.components or {}).items():
                if entry.get("shared") and name not in assets["components"]:
                    assets["components"].append(name)

        reuse = {
            "similar_tests": [
                {"test_id": item.test_id, "name": item.name, "score": score,
                 "pages": item.page_objects, "reliability": round(item.reliability, 2)}
                for score, item in similar
            ],
            "assets": assets,
            "graph": graph_context,
            "existing_scenarios": [item.name for item in store.all()][:60],
        }

        if similar:
            ctx.note(
                f"reuse: {len(similar)} similar test(s) already exist "
                f"({', '.join(item.test_id for _s, item in similar[:4])}); "
                f"reusable assets: {sum(len(v) for v in assets.values())}"
            )
        return reuse

    def _plan_from_memory(self, ctx: AgentContext) -> TestPlan | None:
        """Rebuild the plan from remembered tests, or return None to design it.

        Returns a plan only when *every* testable acceptance criterion is
        matched by a stored scenario above the configured similarity. Partial
        cover is not enough: the uncovered criteria are exactly the ones that
        need designing, and a plan that quietly omits them looks complete.
        """
        from services.knowledge_service.test_knowledge import similarity

        policy = (load_model_config().get("reuse") or {})
        if not policy.get("skip_design", True):
            return None

        store = ctx.test_knowledge
        requirement = ctx.requirement
        if store is None or requirement is None:
            return None

        remembered = store.all()
        if len(remembered) < int(policy.get("min_remembered_scenarios", 3) or 3):
            return None

        criteria = [c for c in requirement.acceptance_criteria if c.testable]
        if not criteria:
            return None

        threshold = float(policy.get("skip_design_similarity", 0.80) or 0.80)
        required = float(policy.get("skip_design_coverage", 1.0) or 1.0)

        # Exact traceability where we have it: a scenario recorded which
        # criteria it verifies, so no threshold is involved. Similarity against
        # the scenario *name* is the fallback for tests remembered before that
        # was stored — never against the full signature, which is padded with
        # tags and step text and so scores low for any short criterion.
        by_criterion: dict[str, list[TestKnowledge]] = {}
        for item in remembered:
            for text in item.covers_criteria:
                by_criterion.setdefault(_normalise_criterion(text), []).append(item)

        matched: dict[str, TestKnowledge] = {}
        covered = 0
        for criterion in criteria:
            traced = by_criterion.get(_normalise_criterion(criterion.text)) or []
            if traced:
                covered += 1
                for item in traced:
                    matched[item.test_id] = item
                continue
            best = max(
                ((similarity(criterion.text, item.name), item) for item in remembered),
                key=lambda pair: pair[0],
                default=(0.0, None),
            )
            if best[1] is not None and best[0] >= threshold:
                covered += 1
                matched[best[1].test_id] = best[1]
        if not matched or covered / len(criteria) < required:
            return None

        plan = _plan_from_knowledge(list(matched.values()), ctx)
        if plan is None:
            return None

        ctx.note(
            f"design skipped: all {len(criteria)} testable criteria are already covered by "
            f"{len(matched)} remembered scenario(s) at >= {threshold:.0%} similarity"
        )
        ctx.metadata["design_reused"] = True
        return plan

    def _drop_duplicates(self, ctx: AgentContext, plan: TestPlan) -> None:
        """Remove designed scenarios that the suite already covers.

        Duplicate coverage is pure waste twice over: it costs generation tokens
        now and runtime on every CI run forever.
        """
        store = ctx.test_knowledge
        if store is None:
            return
        dropped: list[str] = []
        for feature in plan.features:
            kept: list[Scenario] = []
            for scenario in feature.scenarios:
                existing = store.duplicate_of(scenario.name)
                if existing is not None:
                    dropped.append(f"{scenario.name} (already covered by {existing.test_id})")
                    continue
                kept.append(scenario)
            feature.scenarios = kept
        if dropped:
            ctx.note(f"dropped {len(dropped)} duplicate scenario(s): " + "; ".join(dropped[:4]))
            ctx.metadata["duplicates_dropped"] = dropped

    # ------------------------------------------------------------------ #
    def _build_prompt(self, ctx: AgentContext, reuse: dict[str, Any] | None = None) -> str:
        requirement = ctx.requirement
        assert requirement is not None
        reuse = reuse or {}
        standards = ctx.standards
        naming = standards.get("naming", {}) or {}
        layout = (ctx.repo_profile.detected_layout if ctx.repo_profile else {}) or standards.get("layout", {})

        sections: list[str] = [
            "## Requirement",
            f"Title: {requirement.title}",
            f"Summary: {requirement.summary}",
            f"Actors: {', '.join(requirement.actors) or 'unspecified'}",
            f"Preconditions: {'; '.join(requirement.preconditions) or 'none stated'}",
            "Acceptance criteria:",
            *[
                f"  [{criterion.id}] {criterion.text}" + ("" if criterion.testable else " (NOT directly testable)")
                for criterion in requirement.acceptance_criteria
            ],
        ]
        # The endpoint list is short and closes the set of API checks the model
        # may plan. Without it, every api_check it writes is a guess that the
        # code generator then throws away — paying for output twice over.
        endpoints = (
            ctx.application_map.api_catalog(limit=25) if ctx.application_map is not None else []
        )
        if endpoints:
            sections += [
                "",
                "## Observed API endpoints (an api_check may use ONLY these paths)",
                *[
                    f"  {e['method']} {e['path']}"
                    + (f"  fields: {', '.join(e.get('required_fields') or e.get('fields') or [])}"
                       if e.get("fields") or e.get("required_fields") else "")
                    for e in endpoints
                ],
            ]

        if requirement.business_rules:
            sections += ["Business rules:", *[f"  - {rule}" for rule in requirement.business_rules]]
        if requirement.data_requirements:
            sections.append("Data requirements: " + "; ".join(requirement.data_requirements))
        if requirement.out_of_scope:
            sections.append("Out of scope: " + "; ".join(requirement.out_of_scope))

        sections += [
            "",
            "## Organization standards you must follow",
            f"Test id prefix: {naming.get('test_id_prefix', 'TC-')}",
            f"Feature file naming: {naming.get('feature_file', 'kebab-case.feature')}",
            f"Every scenario must be tagged: {naming.get('scenario_must_have_tag', True)}",
            f"Max steps per scenario: {_max_steps(standards)}",
            f"Design AT MOST {_max_scenarios(standards)} scenarios for this feature. "
            "Spend them on distinct risks, not variations of the same one — a boundary set "
            "belongs in one Scenario Outline, not five scenarios.",
            f"Target directories: features={layout.get('features_dir', 'tests/features')}, "
            f"steps={layout.get('steps_dir', 'tests/steps')}, pages={layout.get('pages_dir', 'tests/pages')}",
        ]

        if ctx.repo_profile:
            profile = ctx.repo_profile
            sections += ["", "## Existing repository (reuse, do not duplicate)", profile.conventions_summary]
            existing_steps = [s.signature or s.name for s in profile.symbols_of("step")][:25]
            if existing_steps:
                sections += [
                    "",
                    "Existing step definitions — reuse this exact wording where the intent matches:",
                    *[f"  - {step[:130]}" for step in existing_steps],
                ]
            pages = profile.symbols_of("page_object")
            if pages:
                sections.append(
                    "Existing page objects: "
                    + "; ".join(f"{p.name}({', '.join(p.members[:6])})" for p in pages[:10])
                )
            fixtures = profile.symbols_of("fixture")
            if fixtures:
                sections.append("Existing fixtures: " + ", ".join(f.name for f in fixtures[:12]))

        if ctx.exploration and ctx.exploration.snapshots:
            catalog = ctx.metadata.get("locator_catalog", [])[:40]
            sections += [
                "",
                "## Observed application structure (from a live crawl)",
                f"Pages: {', '.join(s.route_pattern or s.url for s in ctx.exploration.snapshots[:8])}",
            ]
            required_fields = [item["name"] for item in catalog if item.get("required") and item.get("name")]
            if required_fields:
                sections.append("Fields marked required in the DOM: " + ", ".join(required_fields[:20]))
            if ctx.exploration.workflows:
                sections.append(
                    "Discovered workflows: "
                    + "; ".join(f"{w.name} ({len(w.steps)} steps)" for w in ctx.exploration.workflows[:5])
                )
        else:
            sections += [
                "",
                "## Note",
                "The application could not be crawled, so design scenarios from the requirement alone "
                "and keep step text declarative.",
            ]

        if ctx.project.api_base_url:
            sections.append(f"\nAPI base URL is available ({ctx.project.api_base_url}) — prefer API checks for data verification.")
        if ctx.project.database_dsn_ref:
            sections.append("A database connection is configured — DB assertions are available for persistence checks.")

        # ---- reuse context: the cheapest tokens in the whole prompt ------ #
        similar = reuse.get("similar_tests") or []
        if similar:
            sections += [
                "",
                "## Coverage that already exists - DO NOT redesign these",
                *[
                    f"  - [{item['test_id']}] {item['name']} (similarity {item['score']:.2f}, "
                    f"pages: {', '.join(item['pages'][:3]) or 'none'})"
                    for item in similar
                ],
                "Design only what these do not already cover.",
            ]

        assets = reuse.get("assets") or {}
        if any(assets.values()):
            sections += [
                "",
                "## Assets available for reuse - prefer these over anything new",
                *[
                    f"  {key.replace('_', ' ')}: {', '.join(str(v) for v in values[:12])}"
                    for key, values in assets.items()
                    if values
                ],
            ]

        graph = reuse.get("graph") or {}
        if graph:
            sections += [
                "",
                "## Related knowledge (from the QA graph)",
                *[
                    f"  {kind}: {', '.join(str(n.get('label', n.get('key', ''))) for n in nodes[:8])}"
                    for kind, nodes in graph.items()
                ],
            ]

        sections += ["", "Design the test plan now, in a single response covering every scenario."]
        return "\n".join(sections)


# =========================================================================== #
# Conversion + deterministic quality gates
# =========================================================================== #
def _max_scenarios(standards: dict[str, Any]) -> int:
    naming = (standards.get("naming", {}) or {})
    try:
        return max(1, int(naming.get("max_scenarios_per_feature", 10)))
    except (TypeError, ValueError):
        return 10


def _max_steps(standards: dict[str, Any]) -> int:
    for rule in standards.get("rules", []) or []:
        if isinstance(rule, dict) and rule.get("check") == "max_scenario_steps":
            return int(rule.get("max_steps", 15))
    return 15


#: Compact key -> the original key it replaces. The compact contract is what
#: the model is asked for; the original is still accepted so that stored plans,
#: fixtures and any provider that echoes the older schema keep working.
_ALIASES = {
    "desc": "description",
    "p": "priority",
    "neg": "negative",
    "criteria": "covers_criteria",
    "new_pages": "page_objects_needed",
    "reuse_pages": "page_objects_reused",
    "reuse_fixtures": "fixtures_reused",
    "notes": "coverage_notes",
}

_STEP_KEYWORDS = ("Given", "When", "Then", "And", "But")


def _pick(data: dict[str, Any], key: str) -> Any:
    """Read a field by its canonical name, falling back to the compact one."""
    if key in data:
        return data[key]
    for short, canonical in _ALIASES.items():
        if canonical == key and short in data:
            return data[short]
    return None


def _steps(raw_steps: Any) -> list[GherkinStep]:
    """Parse steps written either as objects or as single strings.

    `"Given I am signed in"` costs roughly seven fewer tokens than
    `{"keyword": "Given", "text": "I am signed in"}`, and across a suite that is
    the largest single saving available in the design call. Both are accepted.
    """
    steps: list[GherkinStep] = []
    for step in raw_steps or []:
        if isinstance(step, dict):
            text = str(step.get("text", "")).strip()
            keyword = _keyword(step.get("keyword"))
        elif isinstance(step, str):
            head, _, tail = step.strip().partition(" ")
            if head.capitalize() in _STEP_KEYWORDS and tail.strip():
                keyword, text = head.capitalize(), tail.strip()
            else:
                keyword, text = "Given", step.strip()
        else:
            continue
        if text:
            steps.append(GherkinStep(keyword=keyword, text=text))
    return steps


def _to_plan(raw: Any, ctx: AgentContext) -> TestPlan:
    data = raw if isinstance(raw, dict) else {}
    features: list[FeatureSpec] = []

    for raw_feature in data.get("features", []) or []:
        if not isinstance(raw_feature, dict) or not raw_feature.get("name"):
            continue
        scenarios: list[Scenario] = []
        for raw_scenario in raw_feature.get("scenarios", []) or []:
            if not isinstance(raw_scenario, dict) or not raw_scenario.get("name"):
                continue
            steps = _steps(raw_scenario.get("steps"))
            examples = [
                {str(k): str(v) for k, v in row.items()}
                for row in raw_scenario.get("examples", []) or []
                if isinstance(row, dict) and row
            ]
            scenarios.append(
                Scenario(
                    test_id=str(raw_scenario.get("test_id", "")).strip(),
                    name=str(raw_scenario["name"]).strip()[:200],
                    tags=[_tag(t) for t in raw_scenario.get("tags", []) or [] if str(t).strip()],
                    steps=steps,
                    examples=examples,
                    priority=_priority(_pick(raw_scenario, "priority")),
                    layer=_layer(raw_scenario.get("layer")),
                    negative=bool(_pick(raw_scenario, "negative") or False),
                    # Derived, not asked for: a scenario with an Examples table
                    # is data-driven by definition.
                    data_driven=bool(examples) or bool(raw_scenario.get("data_driven", False)),
                    covers_criteria=[str(c) for c in _pick(raw_scenario, "covers_criteria") or []],
                )
            )
        features.append(
            FeatureSpec(
                name=str(raw_feature["name"]).strip()[:200],
                file_name=str(raw_feature.get("file_name", "")).strip() or _feature_file_name(raw_feature["name"], ctx),
                description=str(_pick(raw_feature, "description") or "").strip(),
                tags=[_tag(t) for t in raw_feature.get("tags", []) or [] if str(t).strip()],
                background=_steps(raw_feature.get("background")),
                scenarios=scenarios,
            )
        )

    if not features:
        features = _to_plan(_fallback_plan(ctx), ctx).features

    def strings(key: str) -> list[str]:
        value = _pick(data, key) or []
        return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []

    plan = TestPlan(
        run_id=ctx.run_id,
        requirement_id=ctx.requirement.id if ctx.requirement else "",
        title=str(data.get("title", "")).strip() or f"Test plan — {ctx.requirement.title if ctx.requirement else 'feature'}",
        strategy=str(data.get("strategy", "")).strip(),
        features=features,
        page_objects_needed=strings("page_objects_needed"),
        page_objects_reused=strings("page_objects_reused"),
        fixtures_reused=strings("fixtures_reused"),
        # Structured objects now, but a plain string is still accepted and parsed
        # by the renderer, so plans stored before this change keep working.
        api_checks=_checks(data.get("api_checks")),
        db_checks=_checks(data.get("db_checks")),
        risks=strings("risks"),
        coverage_notes=str(_pick(data, "coverage_notes") or "").strip(),
    )

    # Cross-check reuse claims against what the repository actually contains.
    if ctx.repo_profile:
        real_pages = {p.name for p in ctx.repo_profile.symbols_of("page_object")}
        real_fixtures = {f.name for f in ctx.repo_profile.symbols_of("fixture")}
        invented = [p for p in plan.page_objects_reused if p not in real_pages]
        if invented:
            ctx.warn(f"plan claimed reuse of non-existent page object(s): {', '.join(invented)} — treating as new")
            plan.page_objects_reused = [p for p in plan.page_objects_reused if p in real_pages]
            plan.page_objects_needed = sorted(set(plan.page_objects_needed) | set(invented))
        plan.fixtures_reused = [f for f in plan.fixtures_reused if f in real_fixtures]
    return plan


def _keyword(value: Any) -> str:
    text = str(value or "Given").strip().capitalize()
    return text if text in ("Given", "When", "Then", "And", "But") else "Given"


def _tag(value: Any) -> str:
    text = str(value).strip()
    return text if text.startswith("@") else f"@{text}"


def _priority(value: Any) -> Priority:
    try:
        return Priority(str(value).upper())
    except ValueError:
        return Priority.P2


def _layer(value: Any) -> TestLayer:
    try:
        return TestLayer(str(value).lower())
    except ValueError:
        return TestLayer.UI


def _feature_file_name(name: str, ctx: AgentContext) -> str:
    import re

    convention = ((ctx.repo_profile.naming_conventions if ctx.repo_profile else {}) or {}).get("feature_file", "")
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "feature"
    if "snake_case" in convention:
        slug = slug.replace("-", "_")
    elif "lowercase" in convention:
        slug = slug.replace("-", "")
    return f"{slug}.feature"


def _normalise_ids(plan: TestPlan, ctx: AgentContext) -> None:
    """Guarantee unique, convention-compliant test ids."""
    import re

    prefix = ((ctx.standards.get("naming", {}) or {}).get("test_id_prefix") or "TC-").rstrip("-") + "-"
    if ctx.repo_profile:
        learned = (ctx.repo_profile.naming_conventions or {}).get("test_id_prefix")
        if learned:
            prefix = learned if learned.endswith("-") else learned + "-"

    area = re.sub(r"[^A-Z]", "", (ctx.requirement.title if ctx.requirement else "TST").upper())[:4] or "TST"
    seen: set[str] = set()
    counter = 0
    for feature in plan.features:
        for scenario in feature.scenarios:
            candidate = scenario.test_id.strip()
            if not candidate or candidate in seen or not candidate.startswith(prefix):
                counter += 1
                candidate = f"{prefix}{area}-{counter:03d}"
                while candidate in seen:
                    counter += 1
                    candidate = f"{prefix}{area}-{counter:03d}"
            seen.add(candidate)
            scenario.test_id = candidate


def _checks(raw: Any) -> list[Any]:
    """Keep planned checks as they came: dicts stay dicts, prose stays prose."""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) or (isinstance(item, str) and item.strip())][:20]


def _normalise_criterion(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _plan_from_knowledge(items: list[TestKnowledge], ctx: AgentContext) -> TestPlan | None:
    """Turn remembered scenarios back into a plan.

    Everything needed is already stored per scenario: the step text, the feature
    it belonged to, its tags and the page objects it drove. Reconstruction is
    exact for the parts that matter and empty for the parts that do not
    (strategy prose, risks) rather than invented.
    """
    usable = [item for item in items if item.steps and item.name]
    if not usable:
        return None

    by_feature: dict[str, list[TestKnowledge]] = {}
    for item in usable:
        by_feature.setdefault(item.feature or (ctx.requirement.title if ctx.requirement else "Feature"), []).append(item)

    features: list[FeatureSpec] = []
    pages: list[str] = []
    for name, remembered in by_feature.items():
        scenarios = [
            Scenario(
                test_id=item.test_id,
                name=item.name,
                tags=list(item.tags),
                steps=_steps(item.steps),
                priority=_priority(next((t[1:] for t in item.tags if t.startswith("@P")), None)),
                negative="@negative" in item.tags,
                data_driven="@data-driven" in item.tags,
                covers_criteria=list(item.covers_criteria),
            )
            for item in remembered
        ]
        features.append(
            FeatureSpec(
                name=name,
                file_name=_feature_file_name(name, ctx),
                description="",
                tags=[],
                background=[],
                scenarios=scenarios,
            )
        )
        for item in remembered:
            pages.extend(p for p in item.page_objects if p not in pages)

    return TestPlan(
        run_id=ctx.run_id,
        requirement_id=ctx.requirement.id if ctx.requirement else "",
        title=f"Test plan (reused) - {ctx.requirement.title if ctx.requirement else 'feature'}",
        strategy=(
            "Reused from the Test Knowledge Store: every testable acceptance criterion is already "
            "covered by an existing scenario, so no new design was generated."
        ),
        features=features,
        page_objects_needed=[],
        page_objects_reused=pages,
        fixtures_reused=sorted({f for item in usable for f in item.fixtures}),
        coverage_notes="Plan rebuilt from remembered tests; no design call was made.",
    )


#: Priority order for choosing a feature's critical path.
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _ensure_tags(plan: TestPlan) -> None:
    """Derive every scenario's tags.

    Tags are a pure function of the scenario's own attributes, so the design
    call is not asked for them: the critical path is the highest-priority
    positive scenario in a feature, everything else is regression, and
    @negative / @data-driven follow the flags.
    """
    for feature in plan.features:
        positives = [s for s in feature.scenarios if not s.negative]
        critical = min(
            positives,
            key=lambda s: _PRIORITY_RANK.get(s.priority.value, 9),
            default=None,
        )
        for scenario in feature.scenarios:
            tags = set(scenario.tags)
            if not any(t in ("@smoke", "@regression", "@sanity") for t in tags):
                tags.add("@smoke" if scenario is critical else "@regression")
            if not any(t.startswith("@P") for t in tags):
                tags.add(f"@{scenario.priority.value}")
            if scenario.negative:
                tags.add("@negative")
            if scenario.data_driven:
                tags.add("@data-driven")
            scenario.tags = sorted(tags)


def _coverage_gaps(plan: TestPlan, ctx: AgentContext) -> list[str]:
    """Deterministic audit of the model's plan."""
    gaps: list[str] = []
    scenarios = [s for f in plan.features for s in f.scenarios]
    if not scenarios:
        return ["the plan contains no scenarios"]

    if not any(s.negative for s in scenarios):
        gaps.append("no negative scenario")
    if not any("@smoke" in s.tags for s in scenarios):
        gaps.append("no scenario tagged @smoke (critical path unmarked)")
    if not any(s.examples for s in scenarios):
        gaps.append("no data-driven scenario (boundary/validation coverage may be thin)")

    max_steps = _max_steps(ctx.standards)
    for scenario in scenarios:
        if len(scenario.steps) > max_steps:
            gaps.append(f"{scenario.test_id or scenario.name}: {len(scenario.steps)} steps exceeds the limit of {max_steps}")
        if not scenario.steps:
            gaps.append(f"{scenario.test_id or scenario.name}: no steps")
        elif not any(s.keyword in ("Then", "And", "But") for s in scenario.steps):
            gaps.append(f"{scenario.test_id or scenario.name}: no Then step — nothing is asserted")

    if ctx.requirement:
        covered = {c for s in scenarios for c in s.covers_criteria}
        uncovered = [
            criterion.text[:70]
            for criterion in ctx.requirement.acceptance_criteria
            if criterion.testable and criterion.id not in covered
        ]
        # Only report when traceability was attempted at all.
        if covered and uncovered:
            gaps.append(f"{len(uncovered)} acceptance criterion/criteria not traced to any scenario")
    return gaps


def _fallback_plan(ctx: AgentContext) -> dict[str, Any]:
    """Deterministic plan built straight from the acceptance criteria."""
    requirement = ctx.requirement
    title = requirement.title if requirement else "Feature"
    criteria = requirement.acceptance_criteria if requirement else []

    scenarios: list[dict[str, Any]] = []
    for index, criterion in enumerate(criteria or [], start=1):
        negative = bool(
            {"reject", "invalid", "missing", "duplicate", "error", "not allowed", "fail"}
            & set(criterion.text.lower().split())
        )
        scenarios.append(
            {
                "test_id": "",
                "name": criterion.text[:140],
                "tags": ["@smoke"] if index == 1 else ["@regression"],
                "priority": "P1" if index <= 2 else "P2",
                "layer": "ui",
                "negative": negative,
                "covers_criteria": [criterion.id],
                "steps": [
                    {"keyword": "Given", "text": "I am authenticated in the application"},
                    {"keyword": "When", "text": f"I exercise {title.lower()}"},
                    {"keyword": "Then", "text": criterion.text[:140]},
                ],
                "examples": [],
            }
        )
    if not scenarios:
        scenarios = [
            {
                "test_id": "", "name": f"{title} happy path", "tags": ["@smoke"], "priority": "P1",
                "layer": "ui", "negative": False, "covers_criteria": [],
                "steps": [
                    {"keyword": "Given", "text": "I am authenticated in the application"},
                    {"keyword": "When", "text": f"I complete {title.lower()}"},
                    {"keyword": "Then", "text": "the operation succeeds"},
                ],
                "examples": [],
            }
        ]

    return {
        "title": f"Test plan — {title}",
        "strategy": "Deterministic plan generated directly from the acceptance criteria (no model available).",
        "features": [
            {
                "name": title,
                "file_name": "",
                "description": requirement.summary if requirement else "",
                "tags": ["@regression"],
                "background": [{"keyword": "Given", "text": "I am authenticated in the application"}],
                "scenarios": scenarios,
            }
        ],
        "page_objects_needed": [f"{''.join(w.capitalize() for w in title.split())}Page"],
        "page_objects_reused": [],
        "fixtures_reused": [],
        "api_checks": [],
        "db_checks": [],
        "risks": ["Plan derived without model reasoning; review coverage carefully."],
        "coverage_notes": "One scenario per acceptance criterion.",
    }
