"""Code Generation Agent — produce executable test assets.

Division of labour that keeps output trustworthy:

* **Feature files are rendered deterministically** from the approved plan. The
  plan *is* the Gherkin, so there is no second chance for a model to change what
  a human already approved.
* **Page Objects and step definitions are model-generated**, but constrained:
  the prompt carries the real repository conventions, the real base class, the
  real fixtures, and the verified locator catalogue. Generated locators are then
  cross-checked against that catalogue.
* **Every artifact has a deterministic template fallback**, so the agent always
  produces compiling code even with no model available.

Nothing reaches disk here. The agent builds a :class:`CodeBundle` of proposed
changes; writing happens only after the Standards Agent passes it and a human
approves the diff.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from agents.base import AgentContext, BaseAgent, json_block
from packages.aiqa_types.enums import (
    AgentName,
    ArtifactKind,
    Capability,
    ChangeType,
)
from packages.aiqa_types.models import CodeBundle, FeatureSpec, FileChange, Scenario, TestPlan
from tools.filesystem.fs_tools import make_diff

PAGE_SYSTEM = """You write Page Object classes for an existing QA automation repository.

Hard rules:
- Match the repository's conventions exactly: same language, same base class, same import style, \
same method naming as the examples given.
- Locators are PRIVATE. Expose intent-revealing async action/assertion methods only.
- Use ONLY locators from the VERIFIED LOCATOR CATALOGUE. If a needed element is absent, add a method \
whose body is a single line: `// TODO(aiqa): locator not verified — confirm against the live application` \
followed by a best-guess getByRole/getByLabel call. Never silently invent a data-testid.
- No hard waits (no waitForTimeout/sleep). Use web-first assertions.
- No credentials, no console.log, no .only/.skip.
- Return complete, compiling file content — no ellipses, no commentary outside the code.

Reply with ONE JSON object: {"files": [{"path": str, "content": str, "rationale": str, "reuses": [str]}]}
`path` must be repository-relative and inside the pages directory given to you."""

STEPS_SYSTEM = """You write BDD step definitions that glue Gherkin to Page Objects.

Hard rules:
- Implement EVERY step text you are given, exactly as written (parameterise with {string}/{int} where the \
Gherkin uses quoted values or Examples columns).
- Step bodies call Page Object methods. NEVER use page.locator/getBy* directly in a step file.
- Reuse the repository's existing fixtures and utilities rather than constructing pages or data by hand.
- Do not re-implement a step that already exists in the repository — omit it and note it in `reuses`.
- No hard waits, no conditional assertions, no console.log.
- Return complete, compiling file content.

Reply with ONE JSON object: {"files": [{"path": str, "content": str, "rationale": str, "reuses": [str]}]}"""


class CodeGenerationAgent(BaseAgent):
    name = AgentName.CODE_GENERATION
    capability = Capability.CODING
    description = "Generates feature files, Page Objects, step definitions and test data from the approved plan."

    def progress(self, ctx: AgentContext) -> float:
        return 0.6

    async def run(self, ctx: AgentContext) -> None:
        plan = ctx.test_plan
        if plan is None:
            raise ValueError("code generation requires an approved test plan")

        layout = self._layout(ctx)
        changes: list[FileChange] = []

        # 1. Feature files — deterministic render of the approved plan.
        changes.extend(self._feature_changes(ctx, plan, layout))

        # 2. Page Objects.
        page_changes = await self._page_object_changes(ctx, plan, layout)
        changes.extend(page_changes)

        # 3. Step definitions.
        changes.extend(await self._step_changes(ctx, plan, layout, page_changes))

        # 4. Test data for data-driven scenarios.
        changes.extend(self._data_changes(ctx, plan, layout))

        # Attach diffs against the current workspace so review is meaningful.
        for change in changes:
            existing = self.tool(ctx, "fs.read_file", path=change.path)
            if existing.ok and isinstance(existing.data, str):
                change.original_content = existing.data
                change.change_type = ChangeType.MODIFY
            change.diff = make_diff(change.path, change.original_content or "", change.content)

        bundle = CodeBundle(
            run_id=ctx.run_id,
            plan_id=plan.id,
            changes=changes,
            summary=(
                f"{len(changes)} file(s): "
                + ", ".join(sorted({c.kind.value for c in changes}))
                + f"; {plan.scenario_count} scenario(s)"
            ),
        )
        ctx.code_bundle = bundle
        ctx.note(f"generated {len(changes)} file(s), {bundle.total_bytes} bytes: " + ", ".join(c.path for c in changes))
        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = f"{len(changes)} files generated"

    # ------------------------------------------------------------------ #
    def _layout(self, ctx: AgentContext) -> dict[str, str]:
        defaults = (ctx.standards.get("layout", {}) or {}).copy()
        learned = (ctx.repo_profile.detected_layout if ctx.repo_profile else {}) or {}
        defaults.update({k: v for k, v in learned.items() if v})
        return {
            "features_dir": defaults.get("features_dir", "tests/features"),
            "steps_dir": defaults.get("steps_dir", "tests/steps"),
            "pages_dir": defaults.get("pages_dir", "tests/pages"),
            "fixtures_dir": defaults.get("fixtures_dir", "tests/fixtures"),
            "utils_dir": defaults.get("utils_dir", "tests/utils"),
            "data_dir": defaults.get("data_dir", "tests/data"),
        }

    # -- 1. features ---------------------------------------------------- #
    def _feature_changes(self, ctx: AgentContext, plan: TestPlan, layout: dict[str, str]) -> list[FileChange]:
        changes: list[FileChange] = []
        for feature in plan.features:
            path = PurePosixPath(layout["features_dir"]) / (feature.file_name or "feature.feature")
            changes.append(
                FileChange(
                    path=str(path),
                    change_type=ChangeType.CREATE,
                    kind=ArtifactKind.FEATURE,
                    content=feature.to_gherkin(),
                    language="gherkin",
                    rationale="Rendered directly from the approved test plan so the Gherkin matches what was reviewed.",
                )
            )
        return changes

    # -- 2. page objects ------------------------------------------------ #
    async def _page_object_changes(
        self, ctx: AgentContext, plan: TestPlan, layout: dict[str, str]
    ) -> list[FileChange]:
        needed = [name for name in plan.page_objects_needed if name]
        if not needed:
            needed = [_page_class_name(plan.features[0].name)] if plan.features else []
        if not needed:
            return []

        existing_names = {p.name for p in (ctx.repo_profile.symbols_of("page_object") if ctx.repo_profile else [])}
        to_create = [name for name in needed if name not in existing_names]
        if not to_create:
            ctx.note("all required page objects already exist — nothing new to generate")
            return []

        catalog = ctx.metadata.get("locator_catalog", [])
        user = "\n".join(
            [
                "## Page Objects to create",
                *[f"- {name}" for name in to_create],
                "",
                f"Target directory: {layout['pages_dir']}",
                f"Language: {ctx.repo_profile.language if ctx.repo_profile else ctx.project.language}",
                "",
                "## Repository conventions (follow exactly)",
                (ctx.repo_profile.conventions_summary if ctx.repo_profile else "No existing conventions detected."),
                "",
                "## Existing code to imitate and reuse",
                ctx.retrieved_context or "(none retrieved)",
                "",
                "## VERIFIED LOCATOR CATALOGUE (the only locators you may use)",
                json_block(catalog[:60], limit=9000) if catalog else "(empty — the application could not be crawled)",
                "",
                "## Scenarios these page objects must support",
                json_block(
                    [
                        {"test_id": s.test_id, "name": s.name, "steps": [st.render() for st in s.steps]}
                        for f in plan.features
                        for s in f.scenarios
                    ],
                    limit=6000,
                ),
                "",
                "Generate the complete Page Object file(s).",
            ]
        )

        raw = await self.ask_json(
            ctx, PAGE_SYSTEM, user, task="code_generation.page_objects",
            fallback={"files": []}, max_tokens=8000,
        )
        changes = self._files_from(
            raw, ctx, kind=ArtifactKind.PAGE_OBJECT, allowed_dir=layout["pages_dir"],
        )

        # Deterministic fallback for anything the model skipped.
        produced = {PurePosixPath(c.path).stem for c in changes}
        for name in to_create:
            if name in produced:
                continue
            ctx.warn(f"model did not produce {name}; using the deterministic template")
            changes.append(self._page_template(ctx, name, layout, catalog))
        return changes

    def _page_template(
        self, ctx: AgentContext, class_name: str, layout: dict[str, str], catalog: list[dict[str, Any]]
    ) -> FileChange:
        base = ((ctx.repo_profile.naming_conventions if ctx.repo_profile else {}) or {}).get(
            "page_object_base_class", ""
        )
        route = _guess_route(ctx)
        fields = [item for item in catalog if item.get("role") in ("textbox", "combobox", "checkbox")][:10]
        buttons = [item for item in catalog if item.get("role") == "button"][:4]

        getters: list[str] = []
        fill_lines: list[str] = []
        for item in fields:
            prop = _camel(item.get("name") or "field")
            locator = item.get("locator") or "// TODO(aiqa): locator not verified"
            getters.append(
                f"  private get {prop}() {{\n    return this.page.{locator};\n  }}\n"
            )
            fill_lines.append(f"    if (data.{prop} !== undefined) await this.{prop}.fill(String(data.{prop}));")
        submit = buttons[0] if buttons else None
        if submit:
            getters.append(
                f"  private get submitButton() {{\n    return this.page.{submit['locator']};\n  }}\n"
            )

        if not fields and not buttons:
            getters.append(
                "  // TODO(aiqa): no locators were verified against the live application.\n"
                "  // Add locators here once the environment is reachable.\n"
            )

        header = [
            f"import {{ Page, expect }} from '@playwright/test';",
        ]
        class_decl = f"export class {class_name}"
        if base:
            header.append(f"import {{ {base} }} from './{base}';")
            class_decl += f" extends {base}"

        body = [
            f"  readonly path = '{route}';",
            "",
            *getters,
            f"  async fillForm(data: Record<string, string | number>): Promise<void> {{",
            *(fill_lines or ["    // TODO(aiqa): map form fields once locators are verified."]),
            "  }",
            "",
            "  async submit(): Promise<void> {",
            "    await this.submitButton.click();" if submit else "    // TODO(aiqa): add the submit locator.",
            "  }",
            "",
            "  async expectSuccess(message: string): Promise<void> {",
            "    await expect(this.page.getByRole('status')).toContainText(message);",
            "  }",
            "",
            "  async expectValidationError(field: string, message: string): Promise<void> {",
            "    await expect(this.page.getByText(message, { exact: false })).toBeVisible();",
            "  }",
        ]
        if not base:
            body.insert(0, "  constructor(private readonly page: Page) {}")
            body.insert(1, "")

        content = "\n".join(
            [
                f"// {class_name} — generated by AI QA Engineer.",
                *header,
                "",
                f"{class_decl} {{",
                *body,
                "}",
                "",
            ]
        )
        return FileChange(
            path=str(PurePosixPath(layout["pages_dir"]) / f"{class_name}.ts"),
            kind=ArtifactKind.PAGE_OBJECT,
            content=content,
            rationale="Deterministic template (model output unavailable); TODO markers flag unverified locators.",
            reuses=[base] if base else [],
        )

    # -- 3. step definitions -------------------------------------------- #
    async def _step_changes(
        self, ctx: AgentContext, plan: TestPlan, layout: dict[str, str], page_changes: list[FileChange]
    ) -> list[FileChange]:
        existing_steps = {
            _normalise_step(s.name): s.file_path
            for s in (ctx.repo_profile.symbols_of("step") if ctx.repo_profile else [])
        }
        new_steps: list[str] = []
        reused: list[str] = []
        for feature in plan.features:
            for step in list(feature.background) + [s for sc in feature.scenarios for s in sc.steps]:
                key = _normalise_step(step.text)
                if key in existing_steps:
                    reused.append(step.text)
                elif step.text not in new_steps:
                    new_steps.append(step.text)

        if not new_steps:
            ctx.note(f"all {len(reused)} step(s) already exist in the repository — no step file needed")
            return []

        page_classes = [PurePosixPath(c.path).stem for c in page_changes] + plan.page_objects_reused
        user = "\n".join(
            [
                f"Target directory: {layout['steps_dir']}",
                f"Language: {ctx.repo_profile.language if ctx.repo_profile else ctx.project.language}",
                f"BDD framework: {'cucumber' if (ctx.repo_profile and ctx.repo_profile.bdd) else 'cucumber (assumed)'}",
                "",
                "## Steps you MUST implement (verbatim text)",
                *[f"- {text}" for text in new_steps],
                "",
                "## Steps that already exist — DO NOT reimplement",
                *([f"- {text}" for text in dict.fromkeys(reused)] or ["(none)"]),
                "",
                "## Page Objects available to call",
                ", ".join(page_classes) or "(none)",
                "",
                "## Page Object source just generated (call these methods)",
                "\n\n".join(f"// {c.path}\n{c.content}" for c in page_changes)[:9000] or "(none)",
                "",
                "## Repository conventions",
                (ctx.repo_profile.conventions_summary if ctx.repo_profile else "none detected"),
                "",
                "## Existing step/fixture code to imitate",
                ctx.retrieved_context[:6000] or "(none)",
                "",
                "Generate the step definition file.",
            ]
        )

        raw = await self.ask_json(
            ctx, STEPS_SYSTEM, user, task="code_generation.steps", fallback={"files": []}, max_tokens=8000
        )
        changes = self._files_from(raw, ctx, kind=ArtifactKind.STEP_DEFINITION, allowed_dir=layout["steps_dir"])
        if not changes:
            ctx.warn("model did not produce step definitions; using the deterministic template")
            changes.append(self._steps_template(ctx, plan, layout, new_steps, page_classes))
        for change in changes:
            change.reuses = sorted(set(change.reuses) | set(dict.fromkeys(reused)))[:20]
        return changes

    def _steps_template(
        self,
        ctx: AgentContext,
        plan: TestPlan,
        layout: dict[str, str],
        step_texts: list[str],
        page_classes: list[str],
    ) -> FileChange:
        page = page_classes[0] if page_classes else "Page"
        slug = re.sub(r"[^a-z0-9]+", "-", (plan.features[0].name if plan.features else "steps").lower()).strip("-")
        rel_pages = _relative_import(layout["steps_dir"], layout["pages_dir"])

        lines = [
            "// Generated by AI QA Engineer — step definitions.",
            "import { Given, When, Then } from '@cucumber/cucumber';",
            f"import {{ {page} }} from '{rel_pages}/{page}';",
            "",
            f"let pageObject: {page};",
            "",
        ]
        for text in step_texts:
            keyword = _keyword_for(text)
            pattern, params = _parameterise(text)
            signature = ", ".join(f"{name}: string" for name in params)
            lines.append(f"{keyword}('{pattern}', async function ({signature}) {{")
            if keyword == "Given":
                lines.append(f"  pageObject = new {page}(this.page);")
                lines.append("  await pageObject.goto();")
            elif keyword == "When":
                lines.append("  // TODO(aiqa): map this action onto a Page Object method.")
                lines.append("  await pageObject.submit();")
            else:
                lines.append("  // TODO(aiqa): assert the expected outcome via a Page Object method.")
                argument = params[0] if params else "''"
                lines.append(f"  await pageObject.expectSuccess({argument});")
            lines.append("});")
            lines.append("")

        return FileChange(
            path=str(PurePosixPath(layout["steps_dir"]) / f"{slug}.steps.ts"),
            kind=ArtifactKind.STEP_DEFINITION,
            content="\n".join(lines),
            rationale="Deterministic scaffold (model output unavailable); TODO markers show what needs wiring.",
        )

    # -- 4. test data --------------------------------------------------- #
    def _data_changes(self, ctx: AgentContext, plan: TestPlan, layout: dict[str, str]) -> list[FileChange]:
        rows: dict[str, list[dict[str, str]]] = {}
        for feature in plan.features:
            for scenario in feature.scenarios:
                if scenario.examples:
                    rows[scenario.test_id or scenario.name] = scenario.examples
        if not rows:
            return []

        import json as _json

        slug = re.sub(r"[^a-z0-9]+", "-", (plan.features[0].name if plan.features else "data").lower()).strip("-")
        return [
            FileChange(
                path=str(PurePosixPath(layout["data_dir"]) / f"{slug}.data.json"),
                kind=ArtifactKind.TEST_DATA,
                content=_json.dumps(rows, indent=2) + "\n",
                language="json",
                rationale="Externalised Examples tables so datasets can change without touching the feature file.",
            )
        ]

    # -- shared --------------------------------------------------------- #
    def _files_from(
        self, raw: Any, ctx: AgentContext, kind: ArtifactKind, allowed_dir: str
    ) -> list[FileChange]:
        """Validate model-proposed files: path confinement + locator honesty."""
        data = raw if isinstance(raw, dict) else {}
        catalog = {item["locator"] for item in ctx.metadata.get("locator_catalog", []) if item.get("locator")}
        changes: list[FileChange] = []

        for item in data.get("files", []) or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip().replace("\\", "/").lstrip("/")
            content = str(item.get("content", ""))
            if not path or not content.strip():
                continue

            # Force the file into the correct directory rather than trusting the model.
            if not path.startswith(allowed_dir.rstrip("/") + "/"):
                path = str(PurePosixPath(allowed_dir) / PurePosixPath(path).name)
                ctx.note(f"relocated generated file into {allowed_dir}/")

            if catalog:
                invented = _invented_locators(content, catalog)
                if invented:
                    ctx.warn(
                        f"{path}: {len(invented)} locator(s) not in the verified catalogue "
                        f"({', '.join(invented[:3])}) — marked for human review"
                    )
                    content = _annotate_unverified(content, invented)

            changes.append(
                FileChange(
                    path=path,
                    kind=kind,
                    content=content if content.endswith("\n") else content + "\n",
                    language="typescript" if path.endswith((".ts", ".tsx")) else "javascript",
                    rationale=str(item.get("rationale", ""))[:500],
                    reuses=[str(r) for r in (item.get("reuses") or [])][:20],
                )
            )
        return changes


# =========================================================================== #
# Helpers
# =========================================================================== #
_TESTID_RE = re.compile(r"getByTestId\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _invented_locators(content: str, catalog: set[str]) -> list[str]:
    """Find test-id locators the crawl never observed.

    Only `getByTestId` is checked: a fabricated test-id is silently broken,
    whereas a role/label guess is self-documenting and usually recoverable.
    """
    observed_ids = {
        match.group(1) for locator in catalog for match in [_TESTID_RE.search(locator)] if match
    }
    found: list[str] = []
    for match in _TESTID_RE.finditer(content):
        test_id = match.group(1)
        if test_id not in observed_ids and test_id not in found:
            found.append(test_id)
    return found


def _annotate_unverified(content: str, invented: list[str]) -> str:
    lines = content.splitlines()
    out: list[str] = []
    flagged = set(invented)
    for line in lines:
        match = _TESTID_RE.search(line)
        if match and match.group(1) in flagged:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}// TODO(aiqa): '{match.group(1)}' was not observed in the live DOM — verify.")
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else "")


def _page_class_name(feature_name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", feature_name) if part) + "Page"


def _camel(text: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(text)) if p]
    if not parts:
        return "field"
    head = parts[0]
    return head[0].lower() + head[1:] + "".join(p.capitalize() for p in parts[1:])


def _guess_route(ctx: AgentContext) -> str:
    if ctx.exploration and ctx.exploration.snapshots:
        for snapshot in ctx.exploration.snapshots:
            route = snapshot.route_pattern or ""
            if route and route != "/":
                return route
    title = (ctx.requirement.title if ctx.requirement else "").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", title).strip("-")
    return f"/{slug}" if slug else "/"


def _normalise_step(text: str) -> str:
    """Compare step intent, ignoring keyword, parameters and punctuation."""
    stripped = re.sub(r"^(Given|When|Then|And|But|\*)\s+", "", str(text).strip(), flags=re.IGNORECASE)
    stripped = re.sub(r"\{[a-z]+\}", "", stripped)
    stripped = re.sub(r"['\"][^'\"]*['\"]", "", stripped)
    stripped = re.sub(r"<[^>]+>", "", stripped)
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def _keyword_for(text: str) -> str:
    lowered = text.lower()
    if re.match(r"^(i am|i have|there is|there are|a |an |the .* exists)", lowered):
        return "Given"
    if re.match(r"^(i (should|see|expect)|the |a validation|an error)", lowered):
        return "Then"
    return "When"


_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_ANGLE_RE = re.compile(r"<([^>]+)>")


def _parameterise(text: str) -> tuple[str, list[str]]:
    """Turn quoted values / Examples placeholders into Cucumber parameters."""
    params: list[str] = []

    def replace_quoted(match: re.Match[str]) -> str:
        params.append(f"value{len(params) + 1}")
        return "{string}"

    def replace_angle(match: re.Match[str]) -> str:
        params.append(_camel(match.group(1)))
        return "{string}"

    pattern = _ANGLE_RE.sub(replace_angle, text)
    pattern = _QUOTED_RE.sub(replace_quoted, pattern)
    pattern = re.sub(r"^(Given|When|Then|And|But)\s+", "", pattern, flags=re.IGNORECASE)
    return pattern.replace("'", "\\'"), params


def _relative_import(from_dir: str, to_dir: str) -> str:
    """Compute a POSIX relative import specifier between two repo directories."""
    from_parts = [p for p in from_dir.strip("/").split("/") if p]
    to_parts = [p for p in to_dir.strip("/").split("/") if p]
    common = 0
    for a, b in zip(from_parts, to_parts):
        if a != b:
            break
        common += 1
    ups = [".."] * (len(from_parts) - common)
    downs = to_parts[common:]
    joined = "/".join(ups + downs)
    return joined if joined.startswith(".") else f"./{joined}" if joined else "."
