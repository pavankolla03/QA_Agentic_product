"""Step Coverage Agent — close the gaps, or name them.

Code generation produces a bundle that compiles and reads well and, until now,
could still contain steps that do nothing. A `return 'pending'` body and a call
to a Page Object method that was never written are both "finished" by every
measure the pipeline previously applied, and neither verifies anything.

This stage sits between generation and standards, and it does three things:

1. **Finds** both kinds of gap in the bundle, before any file reaches disk.
2. **Closes** what the evidence supports — an existing method, or a locator the
   crawler actually observed. The ladder lives in `GapResolver`; nothing here
   invents an interaction.
3. **Names** the rest. A step nothing in the application corresponds to is
   reported as blocking, with the step quoted and the reason given, so a human
   answers one small question rather than reading a diff to find out what went
   wrong.

Bounded to three passes. Each pass can only close gaps using evidence that was
already available, so a pass that closes nothing will not close anything next
time either — the loop exists for the case where closing one gap reveals the
method needed by another, not to keep trying the same thing.
"""

from __future__ import annotations

import re

from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import AgentName, ArtifactKind, Capability, RunMode
from packages.aiqa_types.models import FileChange
from services.execution_service.gap_resolver import GapResolver, Resolution

#: `await somePage.someMethod(args);` inside a step body.
_CALL_RE = re.compile(r"await\s+(?P<instance>[A-Za-z_$][\w$]*)\.(?P<method>[A-Za-z_$][\w$]*)\s*\(")
#: `async someMethod(` — a method the page object actually declares.
_METHOD_RE = re.compile(r"^\s{2}async\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)
#: A step definition and everything up to its closing brace.
_STEP_BLOCK_RE = re.compile(
    r"^(?P<head>(?:Given|When|Then)\(\s*(?P<quote>['\"])(?P<pattern>(?:\\.|(?!(?P=quote)).)*)(?P=quote)"
    r"[^\n]*\n)(?P<body>(?:(?!^\}\);).*\n)*?)^\}\);",
    re.MULTILINE,
)
_PENDING_RE = re.compile(r"^\s*return\s+['\"]pending['\"]\s*;\s*$", re.MULTILINE)
#: The callback's declared arguments: `async function (username: string, ...)`.
_ARGS_RE = re.compile(r"async function\s*\(([^)]*)\)")

MAX_PASSES = 3


class StepCoverageAgent(BaseAgent):
    name = AgentName.STEP_COVERAGE
    capability = Capability.CHEAP          # only ever used for the report line
    description = "Closes generated steps that would not verify anything, or names them as blocking."
    optional = True

    def progress(self, ctx: AgentContext) -> float:
        return 0.68

    def skip_reason(self, ctx: AgentContext) -> str:
        if ctx.mode in (RunMode.EXECUTE_ONLY, RunMode.HEAL_ONLY):
            return f"mode={ctx.mode.value} — nothing was generated"
        if ctx.code_bundle is None or not ctx.code_bundle.changes:
            return "no generated code to check"
        return ""

    # ------------------------------------------------------------------ #
    async def run(self, ctx: AgentContext) -> None:
        bundle = ctx.code_bundle
        assert bundle is not None

        resolver = GapResolver(
            catalog=ctx.metadata.get("locator_catalog", []) or [],
            existing_steps=self._repository_steps(ctx),
            existing_methods=self._repository_methods(ctx),
            route=str(ctx.metadata.get("primary_route", "")),
        )

        closed: list[Resolution] = []
        blocked: list[Resolution] = []

        for attempt in range(MAX_PASSES):
            gaps = self._find_gaps(bundle.changes)
            if not gaps:
                break

            progress = 0
            blocked = []
            for step_text, page_class in gaps:
                resolution = resolver.resolve(step_text)
                if not resolution.resolved:
                    blocked.append(resolution)
                    continue
                if self._apply(bundle.changes, step_text, page_class, resolution):
                    closed.append(resolution)
                    progress += 1
                else:
                    # It resolved but could not be written. `_apply` explains
                    # why when it knows — a step that supplies no value, say —
                    # and only a silent failure needs the generic wording.
                    # Leaving the *resolution's* reason in place would claim the
                    # element was found and that the step is blocked, which
                    # reads as a contradiction and tells a reader nothing.
                    if resolution.strategy != "blocked":
                        resolution.reason = (
                            f'matched "{resolution.locator_name or resolution.method}" but the '
                            "generated code offered no Page Object to put it on"
                        )
                        resolution.strategy = "blocked"
                    blocked.append(resolution)

            if not progress:
                # Nothing moved. Another pass has the same evidence available
                # and would reach the same answer.
                if attempt:
                    ctx.note(f"step coverage settled after {attempt + 1} pass(es)")
                break

        self._report(ctx, closed, blocked)

    # ------------------------------------------------------------------ #
    def _find_gaps(self, changes: list[FileChange]) -> list[tuple[str, str]]:
        """Every step that would not do anything, and the page it belongs to.

        Two shapes, both of which the pipeline used to call finished: a body
        that is only `return 'pending'`, and a call to a method the page object
        never declares. The second compiles in the model's head and nowhere
        else.
        """
        declared = self._declared_methods(changes)
        gaps: list[tuple[str, str]] = []

        for change in changes:
            if change.kind != ArtifactKind.STEP_DEFINITION:
                continue
            for block in _STEP_BLOCK_RE.finditer(change.content):
                body, pattern = block.group("body"), block.group("pattern")
                call = _CALL_RE.search(body)
                page_class = self._page_of(change.content, body, call)

                if _PENDING_RE.search(body):
                    gaps.append((pattern, page_class))
                elif call and page_class and call.group("method") not in declared.get(page_class, set()):
                    # `goto` comes from the base class, which is not in this
                    # bundle and is not this stage's business.
                    if call.group("method") != "goto":
                        gaps.append((pattern, page_class))
        return gaps

    @staticmethod
    def _declared_methods(changes: list[FileChange]) -> dict[str, set[str]]:
        declared: dict[str, set[str]] = {}
        for change in changes:
            if change.kind != ArtifactKind.PAGE_OBJECT:
                continue
            match = re.search(r"export class (\w+)", change.content)
            if match:
                declared[match.group(1)] = {m.group("name") for m in _METHOD_RE.finditer(change.content)}
        return declared

    @staticmethod
    def _page_of(source: str, body: str, call: re.Match[str] | None) -> str:
        """Which Page Object this step belongs to.

        A call names its own instance, but a *pending* body has no call — that
        is what makes it pending — so the page has to come from elsewhere. It is
        almost always right there in the body as `page ??= new SomePage(...)`,
        and failing that a step file declaring exactly one page leaves no room
        for doubt.

        Getting this wrong was not harmless: every pending step resolved to a
        real observed element and was then reported as blocked anyway, because
        there was no class to hang the new method on.
        """
        if call is not None:
            match = re.search(
                rf"^let {re.escape(call.group('instance'))}!?:\s*(\w+);", source, re.MULTILINE
            )
            if match:
                return match.group(1)

        construction = re.search(r"new\s+(\w+)\s*\(", body)
        if construction:
            return construction.group(1)

        declared = re.findall(r"^let \w+!?:\s*(\w+);", source, re.MULTILINE)
        return declared[0] if len(set(declared)) == 1 else ""

    # ------------------------------------------------------------------ #
    def _apply(
        self,
        changes: list[FileChange],
        step_text: str,
        page_class: str,
        resolution: Resolution,
    ) -> bool:
        """Write the resolution into the bundle. False if it could not be placed."""
        if resolution.strategy == "reuse_step":
            # Already implemented elsewhere in the repository; the duplicate
            # definition here is the problem, so drop its body to a call-free
            # comment rather than binding it twice.
            return self._rewrite_step(changes, step_text, None, comment=resolution.reason)

        target_class = resolution.page_class or page_class
        if not target_class:
            return False

        if resolution.strategy == "generate_method" and not self._add_method(
            changes, target_class, resolution
        ):
            return False

        # A method that needs a value can only be called by a step that carries
        # one. Emitting `fillEmail(value)` into a callback that declares no
        # `value` is TypeScript that does not compile — the same defect the main
        # renderer refuses by returning no binding at all, for the same reason.
        available = self._step_arguments(changes, step_text)
        if len(available) < len(resolution.params):
            resolution.strategy = "blocked"
            resolution.reason = (
                f'"{resolution.locator_name or resolution.method}" needs a value and the step '
                "supplies none — quote one in the step, and I will bind it"
            )
            return False

        args = ", ".join(available[: len(resolution.params)])
        return self._rewrite_step(
            changes, step_text, f"{resolution.method}({args})", page_class=target_class
        )

    @staticmethod
    def _step_arguments(changes: list[FileChange], step_text: str) -> list[str]:
        """What this step definition actually receives, read from its signature."""
        for change in changes:
            if change.kind != ArtifactKind.STEP_DEFINITION:
                continue
            for block in _STEP_BLOCK_RE.finditer(change.content):
                if block.group("pattern") != step_text:
                    continue
                match = _ARGS_RE.search(block.group("head"))
                if not match or not match.group(1).strip():
                    return []
                return [
                    part.split(":")[0].strip()
                    for part in match.group(1).split(",")
                    if part.strip()
                ]
        return []

    def _add_method(self, changes: list[FileChange], page_class: str, resolution: Resolution) -> bool:
        """Insert a locator getter and a method into the generated page object."""
        for change in changes:
            if change.kind != ArtifactKind.PAGE_OBJECT:
                continue
            if not re.search(rf"export class {re.escape(page_class)}\b", change.content):
                continue
            if re.search(rf"^\s{{2}}async {re.escape(resolution.method)}\s*\(", change.content, re.MULTILINE):
                return True            # a previous pass already added it

            prop = _property_name(resolution.locator_name, change.content)
            getter = (
                f"  private get {prop}() {{\n"
                f"    return this.page.{resolution.locator};\n"
                f"  }}\n\n"
            )
            body = _method_body(resolution, prop)
            signature = ", ".join(f"{p}: string" for p in resolution.params)
            method = (
                f"  /** {resolution.reason} */\n"
                f"  async {resolution.method}({signature}): Promise<void> {{\n"
                f"{body}"
                f"  }}\n"
            )

            closing = change.content.rstrip().rfind("}")
            if closing == -1:
                return False
            head = change.content[:closing].rstrip() + '\n\n'
            change.content = f"{head}{getter}{method}}}\n"
            change.bytes = len(change.content.encode("utf-8"))
            if resolution.action == "assert" and "import { expect }" not in change.content:
                change.content = _ensure_expect_import(change.content)
                change.bytes = len(change.content.encode("utf-8"))
            return True
        return False

    def _rewrite_step(
        self,
        changes: list[FileChange],
        step_text: str,
        call: str | None,
        *,
        page_class: str = "",
        comment: str = "",
    ) -> bool:
        """Replace one step's body with a real call."""
        for change in changes:
            if change.kind != ArtifactKind.STEP_DEFINITION:
                continue

            def replace(block: re.Match[str]) -> str:
                if block.group("pattern") != step_text:
                    return block.group(0)
                if call is None:
                    body = f"  // {comment}\n" if comment else "  // handled elsewhere\n"
                else:
                    instance = page_class[:1].lower() + page_class[1:] if page_class else ""
                    lines = []
                    if instance:
                        lines.append(f"  {instance} ??= new {page_class}(this.page);")
                    lines.append(f"  await {instance}.{call};" if instance else f"  await {call};")
                    body = "\n".join(lines) + "\n"
                return f"{block.group('head')}{body}}});"

            updated = _STEP_BLOCK_RE.sub(replace, change.content)
            if updated != change.content:
                change.content = updated
                change.bytes = len(change.content.encode("utf-8"))
                return True
        return False

    # ------------------------------------------------------------------ #
    def _report(self, ctx: AgentContext, closed: list[Resolution], blocked: list[Resolution]) -> None:
        if closed:
            ctx.note(
                f"step coverage: closed {len(closed)} gap(s) — "
                + "; ".join(f"{r.strategy} for \"{r.step[:50]}\"" for r in closed[:4])
            )
        ctx.metadata["step_coverage"] = {
            "closed": [{"step": r.step, "strategy": r.strategy, "method": r.method} for r in closed],
            "blocked": [{"step": r.step, "reason": r.reason} for r in blocked],
        }

        if not blocked:
            if closed:
                ctx.note("step coverage: every generated step is now bound to a real action")
            return

        # Blocking is the honest outcome, and it has to be loud. A run that
        # reports "complete" with an unimplemented step in it is the thing this
        # stage exists to prevent.
        ctx.metadata["automation_blocked"] = [
            {"step": r.step, "reason": r.reason} for r in blocked
        ]
        ctx.warn(
            f"AUTOMATION_BLOCKED: {len(blocked)} step(s) have no verified action behind them "
            "and cannot be generated safely"
        )
        for resolution in blocked[:6]:
            ctx.warn(f'  "{resolution.step}" — {resolution.reason}')

    # ------------------------------------------------------------------ #
    @staticmethod
    def _repository_steps(ctx: AgentContext) -> list[str]:
        if ctx.repo_profile is None:
            return []
        return [s.name for s in ctx.repo_profile.symbols_of("step") if s.name]

    @staticmethod
    def _repository_methods(ctx: AgentContext) -> dict[str, set[str]]:
        if ctx.repo_profile is None:
            return {}
        return {
            symbol.name: set(symbol.members or [])
            for symbol in ctx.repo_profile.symbols_of("page_object")
            if symbol.name
        }


# --------------------------------------------------------------------------- #
def _property_name(label: str, existing: str) -> str:
    """A getter name from the element's own label, not colliding with anything."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", label or "") if w]
    base = (words[0][:1].lower() + words[0][1:]) if words else "element"
    base += "".join(w[:1].upper() + w[1:] for w in words[1:])
    candidate, index = base, 2
    while re.search(rf"\b{re.escape(candidate)}\b", existing):
        candidate, index = f"{base}{index}", index + 1
    return candidate


def _method_body(resolution: Resolution, prop: str) -> str:
    """The statement that drives this element, chosen by its ARIA role.

    A `<select>` does not respond to `fill()` and a checkbox does not take a
    string — the same rule the main renderer applies, for the same reason.
    """
    param = resolution.params[0] if resolution.params else "''"
    if resolution.action == "assert":
        return f"    await expect(this.{prop}).toContainText({param});\n"
    if resolution.action == "navigate":
        return f"    await this.{prop}.click();\n"
    if resolution.action in ("check", "uncheck"):
        return f"    await this.{prop}.setChecked({str(resolution.action == 'check').lower()});\n"
    if resolution.action == "fill":
        if resolution.role == "combobox":
            return f"    await this.{prop}.selectOption({param});\n"
        return f"    await this.{prop}.fill({param});\n"
    return f"    await this.{prop}.click();\n"


def _ensure_expect_import(source: str) -> str:
    """An assertion needs `expect`; the original render had no reason to import it."""
    if "from '@playwright/test'" in source:
        return re.sub(
            r"import \{ ([^}]*) \} from '@playwright/test';",
            lambda m: f"import {{ expect, {m.group(1)} }} from '@playwright/test';"
            if "expect" not in m.group(1)
            else m.group(0),
            source,
            count=1,
        )
    lines = source.splitlines(keepends=True)
    insert = next((i for i, line in enumerate(lines) if line.startswith("import ")), 0)
    lines.insert(insert, "import { expect } from '@playwright/test';\n")
    return "".join(lines)
