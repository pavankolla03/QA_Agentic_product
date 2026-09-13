"""Test Design Agent — the artifact a human approves.

Produces a risk-based :class:`TestPlan` of Gherkin scenarios traced back to
acceptance criteria. Coverage gaps are detected deterministically afterwards, so
"the model forgot the negative case" is caught by code rather than hoped away.

This is the first human approval gate: nothing is written to the workspace until
the plan is accepted.
"""

from __future__ import annotations

from typing import Any

from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import (
    AgentName,
    ApprovalKind,
    Capability,
    Priority,
    RiskLevel,
    TestLayer,
)
from packages.aiqa_types.models import FeatureSpec, GherkinStep, Scenario, TestPlan

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
- Prefer API or DB verification over UI assertions for data-level checks.

Reply with ONE JSON object:
{"title": str, "strategy": str,
 "features": [{"name": str, "file_name": str, "description": str, "tags": [str],
   "background": [{"keyword": str, "text": str}],
   "scenarios": [{"test_id": str, "name": str, "tags": [str], "priority": "P0|P1|P2|P3",
     "layer": "ui|api|database", "negative": bool, "data_driven": bool,
     "covers_criteria": [str],
     "steps": [{"keyword": "Given|When|Then|And|But", "text": str}],
     "examples": [{"column": "value"}]}]}],
 "page_objects_needed": [str], "page_objects_reused": [str], "fixtures_reused": [str],
 "api_checks": [str], "db_checks": [str], "risks": [str], "coverage_notes": str}"""


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

        # ---- 0. reuse discovery (deterministic, free) ------------------- #
        # Ask what we already have before paying a reasoning model to invent it.
        reuse = self._discover_reuse(ctx)

        # ---- 1. one batched design call for every scenario -------------- #
        # Designing six scenarios in six calls costs six times as much and
        # produces a less coherent suite, because no call sees the others.
        user = self._build_prompt(ctx, reuse)
        raw = await self.ask_json(
            ctx, SYSTEM, user,
            task="test_design.plan",
            fallback=_fallback_plan(ctx),
            max_tokens=6000,
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
                    f"  {key.replace('_', ' ')}: {', '.join(values[:12])}"
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
def _max_steps(standards: dict[str, Any]) -> int:
    for rule in standards.get("rules", []) or []:
        if isinstance(rule, dict) and rule.get("check") == "max_scenario_steps":
            return int(rule.get("max_steps", 15))
    return 15


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
            steps = [
                GherkinStep(keyword=_keyword(step.get("keyword")), text=str(step.get("text", "")).strip())
                for step in raw_scenario.get("steps", []) or []
                if isinstance(step, dict) and str(step.get("text", "")).strip()
            ]
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
                    priority=_priority(raw_scenario.get("priority")),
                    layer=_layer(raw_scenario.get("layer")),
                    negative=bool(raw_scenario.get("negative", False)),
                    data_driven=bool(examples) or bool(raw_scenario.get("data_driven", False)),
                    covers_criteria=[str(c) for c in raw_scenario.get("covers_criteria", []) or []],
                )
            )
        features.append(
            FeatureSpec(
                name=str(raw_feature["name"]).strip()[:200],
                file_name=str(raw_feature.get("file_name", "")).strip() or _feature_file_name(raw_feature["name"], ctx),
                description=str(raw_feature.get("description", "")).strip(),
                tags=[_tag(t) for t in raw_feature.get("tags", []) or [] if str(t).strip()],
                background=[
                    GherkinStep(keyword=_keyword(step.get("keyword")), text=str(step.get("text", "")).strip())
                    for step in raw_feature.get("background", []) or []
                    if isinstance(step, dict) and str(step.get("text", "")).strip()
                ],
                scenarios=scenarios,
            )
        )

    if not features:
        features = _to_plan(_fallback_plan(ctx), ctx).features

    def strings(key: str) -> list[str]:
        value = data.get(key) or []
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
        api_checks=strings("api_checks"),
        db_checks=strings("db_checks"),
        risks=strings("risks"),
        coverage_notes=str(data.get("coverage_notes", "")).strip(),
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


def _ensure_tags(plan: TestPlan) -> None:
    """Every scenario carries at least a suite tag and a priority tag."""
    for feature in plan.features:
        for scenario in feature.scenarios:
            tags = set(scenario.tags)
            if not any(t in ("@smoke", "@regression", "@sanity") for t in tags):
                tags.add("@regression")
            if not any(t.startswith("@P") for t in tags):
                tags.add(f"@{scenario.priority.value}")
            if scenario.negative:
                tags.add("@negative")
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
