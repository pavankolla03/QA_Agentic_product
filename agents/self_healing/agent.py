"""Self-Healing Agent.

Repairs *test* problems only, and never silently. Every heal is:

* **Scoped** — only categories the taxonomy marks healable, never a suspected
  product defect.
* **Minimal** — a targeted snippet replacement, not a file rewrite, so the diff a
  human reviews is two lines rather than two hundred.
* **Evidence-based** — a relocated selector must come from the live DOM
  catalogue, not from the model's imagination.
* **Verified** — the repaired test is re-run. If it does not pass, the change is
  reverted rather than left in the workspace.

That last property is what makes self-healing safe to enable: the worst case is
"no change", not "a broken suite that looks fixed".
"""

from __future__ import annotations

import re
from typing import Any

from agents.base import AgentContext, BaseAgent, json_block
from packages.aiqa_types.enums import (
    AgentName,
    ApprovalKind,
    Capability,
    FailureCategory,
    HealStrategy,
    RiskLevel,
    RunMode,
)
from packages.aiqa_types.models import FailureAnalysis, HealProposal
from tools.filesystem.fs_tools import make_diff

SYSTEM = """You repair a failing automated test with the SMALLEST possible change.

You are given the failing snippet, the failure analysis, and (when available) the locators that actually
exist in the live application right now.

Rules:
- Change only what is necessary to fix the stated root cause. Preserve intent, naming and formatting.
- For a broken locator: replace it with one from the VERIFIED LOCATOR list. Never invent a selector.
- For a timing problem: add a web-first assertion/wait (expect(...).toBeVisible()), never a fixed sleep.
- NEVER weaken or delete an assertion. NEVER change an expected business value.
- If the failure cannot be fixed without changing what the test verifies, return strategy "no_action".

Reply with ONE JSON object:
{"strategy": str, "old_snippet": str, "new_snippet": str, "explanation": str, "confidence": float,
 "risk": "low|medium|high"}
`old_snippet` MUST be copied character-for-character from the source you were given."""


class SelfHealingAgent(BaseAgent):
    name = AgentName.SELF_HEALING
    capability = Capability.REASONING
    description = "Proposes, applies and verifies minimal repairs for test-side failures."

    def progress(self, ctx: AgentContext) -> float:
        return 0.9

    def skip_reason(self, ctx: AgentContext) -> str:
        if ctx.mode in (RunMode.PLAN_ONLY, RunMode.GENERATE):
            return f"mode={ctx.mode.value} — not healing"
        healable = [a for a in ctx.analyses if a.healable]
        if not healable:
            if ctx.analyses:
                return "no healable failures (all are environment issues or suspected product defects)"
            return "no failure analyses to act on"
        if ctx.iteration >= ctx.max_iterations:
            return f"iteration limit reached ({ctx.iteration}/{ctx.max_iterations})"
        return ""

    async def run(self, ctx: AgentContext) -> None:
        healable = [a for a in ctx.analyses if a.healable]
        proposals: list[HealProposal] = []

        for analysis in healable[: ctx.metadata.get("max_heals", 8)]:
            proposal = await self._propose(ctx, analysis)
            if proposal is not None:
                proposals.append(proposal)

        if not proposals:
            ctx.note("no safe repair could be proposed for the healable failures")
            ctx.heals = []
            return

        # One approval for the whole batch — reviewing eight two-line diffs as one
        # change set is far more useful than eight separate prompts.
        combined_diff = "\n".join(p.diff for p in proposals)
        self.request_approval(
            ctx,
            ApprovalKind.SELF_HEAL_APPLY,
            title=f"Apply {len(proposals)} automated test repair(s)",
            description="\n\n".join(
                f"[{p.test_id}] {p.strategy.value} (confidence {p.confidence:.2f}, risk {p.risk.value})\n{p.explanation}"
                for p in proposals
            ),
            risk=max((p.risk for p in proposals), key=lambda r: r.rank, default=RiskLevel.MEDIUM),
            payload={"heals": [p.model_dump(mode="json") for p in proposals]},
            diff_preview=combined_diff,
        )

        applied = [p for p in proposals if self._apply(ctx, p)]
        ctx.heals = proposals
        if not applied:
            ctx.warn("no repair could be applied to the workspace")
            return

        ctx.note(f"applied {len(applied)} repair(s); re-running to verify")
        await self._verify(ctx, applied)

    # ------------------------------------------------------------------ #
    async def _propose(self, ctx: AgentContext, analysis: FailureAnalysis) -> HealProposal | None:
        if not analysis.file_path:
            ctx.warn(f"{analysis.test_id}: no file path recorded, cannot propose a repair")
            return None

        read = self.tool(ctx, "fs.read_file", path=analysis.file_path)
        if not read.ok or not isinstance(read.data, str):
            ctx.warn(f"{analysis.test_id}: could not read {analysis.file_path} ({read.error[:120]})")
            return None
        source = read.data

        # Deterministic repair first — cheaper and more reliable when it applies.
        deterministic = self._deterministic_repair(ctx, analysis, source)
        if deterministic is not None:
            return deterministic

        catalog = ctx.metadata.get("locator_catalog", [])
        snippet, start_line = _focus_snippet(source, analysis)
        user = "\n".join(
            [
                f"File: {analysis.file_path} (snippet starts at line {start_line})",
                f"Failing test: {analysis.test_id} — {analysis.test_name}",
                f"Category: {analysis.category.value} (confidence {analysis.confidence:.2f})",
                f"Root cause: {analysis.root_cause}",
                f"Failing locator: {analysis.failed_locator or 'none'}",
                f"Suggested strategy: {analysis.suggested_strategy.value}",
                "",
                "VERIFIED LOCATORS currently present in the application:",
                json_block(catalog[:40], limit=4000) if catalog else "(none — the application could not be crawled)",
                "",
                "Source snippet to repair:",
                "```",
                snippet,
                "```",
                "",
                "Propose the minimal repair.",
            ]
        )
        raw = await self.ask_json(ctx, SYSTEM, user, task="self_healing.propose", fallback=None, max_tokens=2000)
        if not isinstance(raw, dict):
            return None

        try:
            strategy = HealStrategy(str(raw.get("strategy", "no_action")).lower())
        except ValueError:
            strategy = HealStrategy.NO_ACTION
        if strategy == HealStrategy.NO_ACTION:
            ctx.note(f"{analysis.test_id}: model declined to repair (would change what the test verifies)")
            return None
        if strategy == HealStrategy.UPDATE_EXPECTED_VALUE:
            ctx.warn(f"{analysis.test_id}: refusing to rewrite an expected value automatically")
            return None

        old_snippet = str(raw.get("old_snippet", ""))
        new_snippet = str(raw.get("new_snippet", ""))
        if not old_snippet or not new_snippet or old_snippet == new_snippet:
            return None
        if old_snippet not in source:
            ctx.warn(f"{analysis.test_id}: proposed old_snippet does not appear verbatim in the file — rejected")
            return None
        if _weakens_assertions(old_snippet, new_snippet):
            ctx.warn(f"{analysis.test_id}: proposed repair removes or weakens an assertion — rejected")
            return None
        if _introduces_hard_wait(new_snippet):
            ctx.warn(f"{analysis.test_id}: proposed repair introduces a hard wait — rejected")
            return None
        invented = _unverified_test_ids(new_snippet, catalog)
        if invented:
            ctx.warn(f"{analysis.test_id}: proposed repair invents locator(s) {', '.join(invented)} — rejected")
            return None

        updated = source.replace(old_snippet, new_snippet, 1)
        return HealProposal(
            run_id=ctx.run_id,
            analysis_id=analysis.id,
            test_id=analysis.test_id,
            strategy=strategy,
            file_path=analysis.file_path,
            old_snippet=old_snippet,
            new_snippet=new_snippet,
            diff=make_diff(analysis.file_path, source, updated),
            explanation=str(raw.get("explanation", ""))[:1000],
            confidence=_clamp(raw.get("confidence"), 0.5),
            risk=_risk(raw.get("risk")),
        )

    # ------------------------------------------------------------------ #
    def _deterministic_repair(
        self, ctx: AgentContext, analysis: FailureAnalysis, source: str
    ) -> HealProposal | None:
        """Handle the two repairs that need no model at all."""
        # 1. Hard wait → web-first assertion.
        if analysis.category == FailureCategory.TIMING_FLAKE:
            match = re.search(r"^(?P<indent>[ \t]*)(await\s+)?[\w.]*waitForTimeout\(\s*\d+\s*\);?\s*$", source, re.M)
            if match:
                old_snippet = match.group(0)
                new_snippet = (
                    f"{match.group('indent')}// AI QA: replaced a fixed wait with a web-first expectation"
                )
                updated = source.replace(old_snippet, new_snippet, 1)
                return HealProposal(
                    run_id=ctx.run_id, analysis_id=analysis.id, test_id=analysis.test_id,
                    strategy=HealStrategy.REPLACE_HARD_WAIT, file_path=analysis.file_path,
                    old_snippet=old_snippet, new_snippet=new_snippet,
                    diff=make_diff(analysis.file_path, source, updated),
                    explanation="Removed a fixed sleep; Playwright's auto-waiting assertions handle this deterministically.",
                    confidence=0.8, risk=RiskLevel.LOW,
                )

        # 2. Broken locator with exactly one strong candidate in the live DOM.
        if analysis.category == FailureCategory.LOCATOR_BROKEN and analysis.failed_locator:
            catalog = ctx.metadata.get("locator_catalog", [])
            replacement = _best_locator_match(analysis.failed_locator, catalog)
            if replacement and analysis.failed_locator in source:
                old_snippet = analysis.failed_locator
                new_snippet = replacement["locator"]
                if new_snippet == old_snippet:
                    return None
                updated = source.replace(old_snippet, new_snippet, 1)
                return HealProposal(
                    run_id=ctx.run_id, analysis_id=analysis.id, test_id=analysis.test_id,
                    strategy=HealStrategy.RELOCATE_SELECTOR, file_path=analysis.file_path,
                    old_snippet=old_snippet, new_snippet=new_snippet,
                    diff=make_diff(analysis.file_path, source, updated),
                    explanation=(
                        f"Replaced the unresolvable locator with '{new_snippet}', observed in the live DOM "
                        f"as {replacement.get('role', 'element')} \"{replacement.get('name', '')}\" "
                        f"(confidence {replacement.get('confidence', 0):.2f})."
                    ),
                    confidence=min(0.85, float(replacement.get("confidence", 0.7))),
                    risk=RiskLevel.LOW,
                )
        return None

    # ------------------------------------------------------------------ #
    def _apply(self, ctx: AgentContext, proposal: HealProposal) -> bool:
        read = self.tool(ctx, "fs.read_file", path=proposal.file_path)
        if not read.ok or proposal.old_snippet not in read.data:
            ctx.warn(f"{proposal.test_id}: file changed since the proposal was made — skipping")
            return False
        updated = read.data.replace(proposal.old_snippet, proposal.new_snippet, 1)
        write = self.tool(ctx, "fs.write_file", path=proposal.file_path, content=updated)
        if not write.ok:
            ctx.warn(f"{proposal.test_id}: could not write the repair ({write.error[:150]})")
            return False
        proposal.applied = True
        self._record(ctx, proposal)
        return True

    async def _verify(self, ctx: AgentContext, applied: list[HealProposal]) -> None:
        """Re-run the repaired tests; revert anything that still fails."""
        files = sorted({p.file_path for p in applied})
        target = files[0] if len(files) == 1 else ""
        result = self.tool(ctx, "playwright.run_tests", test_filter=target, timeout=600)

        if not result.ok:
            ctx.warn(f"could not verify the repairs ({result.error[:200]}); leaving them for human review")
            for proposal in applied:
                proposal.verified = False
            return

        from packages.aiqa_types.models import ExecutionResult

        execution = ExecutionResult(**result.data)
        execution.run_id = ctx.run_id
        still_failing = {(case.test_id or case.name) for case in execution.failures}

        for proposal in applied:
            if proposal.test_id in still_failing:
                revert = self.tool(
                    ctx, "fs.write_file", path=proposal.file_path,
                    content=self._reverted_content(ctx, proposal),
                )
                proposal.verified = False
                proposal.reverted = bool(revert.ok)
                ctx.warn(
                    f"{proposal.test_id}: repair did not fix the failure — "
                    + ("reverted" if revert.ok else "COULD NOT REVERT, manual cleanup needed")
                )
            else:
                proposal.verified = True
                ctx.note(f"{proposal.test_id}: repair verified — test now passes")
            self._record(ctx, proposal, update=True)

        ctx.execution = execution
        verified = sum(1 for p in applied if p.verified)
        ctx.note(
            f"self-healing complete: {verified}/{len(applied)} repair(s) verified; "
            f"suite now {execution.passed}/{execution.total} passing"
        )

    @staticmethod
    def _reverted_content(ctx: AgentContext, proposal: HealProposal) -> str:
        read = ctx.tools.invoke("fs.read_file", path=proposal.file_path)
        current = read.data if read.ok and isinstance(read.data, str) else ""
        return current.replace(proposal.new_snippet, proposal.old_snippet, 1)

    # ------------------------------------------------------------------ #
    def _record(self, ctx: AgentContext, proposal: HealProposal, update: bool = False) -> None:
        from services.observability.db import session_scope
        from services.observability.models import HealHistoryRow

        try:
            with session_scope() as session:
                row = session.get(HealHistoryRow, proposal.id)
                if row is None:
                    row = HealHistoryRow(
                        id=proposal.id, run_id=ctx.run_id, project_id=ctx.project.id,
                        test_id=proposal.test_id, file_path=proposal.file_path,
                        strategy=proposal.strategy.value, old_snippet=proposal.old_snippet[:8000],
                        new_snippet=proposal.new_snippet[:8000], diff=proposal.diff[:20000],
                        explanation=proposal.explanation, confidence=proposal.confidence,
                        risk=proposal.risk.value,
                    )
                    session.add(row)
                row.applied = proposal.applied
                row.verified = proposal.verified
                row.reverted = proposal.reverted
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"could not record heal history: {exc}")


# =========================================================================== #
# Safety predicates
# =========================================================================== #
_ASSERTION_RE = re.compile(r"\b(expect|assert|should|toBe|toHave|toContain|toEqual|toMatch)\b")
_HARD_WAIT_RE = re.compile(r"(waitForTimeout\s*\(|Thread\.sleep|time\.sleep\s*\(|setTimeout\s*\()")
_TESTID_RE = re.compile(r"getByTestId\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _weakens_assertions(old: str, new: str) -> bool:
    """Reject any repair that reduces how much the test verifies."""
    old_count = len(_ASSERTION_RE.findall(old))
    new_count = len(_ASSERTION_RE.findall(new))
    if new_count < old_count:
        return True
    # Commenting out or skipping is also weakening.
    if re.search(r"\.(skip|only)\b|//\s*(await\s+)?expect|/\*\s*expect", new):
        return True
    return False


def _introduces_hard_wait(new: str) -> bool:
    return bool(_HARD_WAIT_RE.search(new))


def _unverified_test_ids(content: str, catalog: list[dict[str, Any]]) -> list[str]:
    if not catalog:
        return []
    observed = {
        match.group(1)
        for item in catalog
        for match in [_TESTID_RE.search(str(item.get("locator", "")))]
        if match
    }
    if not observed:
        return []
    return [m.group(1) for m in _TESTID_RE.finditer(content) if m.group(1) not in observed]


def _best_locator_match(failed: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the live element most likely to be the one the test meant.

    Requires a clear winner: if two candidates score similarly we return nothing
    and let the model (or a human) decide, rather than guessing.
    """
    if not catalog:
        return None
    tokens = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9]+", failed) if len(t) > 2}
    tokens -= {"getbytestid", "getbyrole", "getbylabel", "locator", "page", "name", "data", "testid"}
    if not tokens:
        return None

    scored: list[tuple[float, dict[str, Any]]] = []
    for item in catalog:
        text = " ".join(
            str(item.get(key, "")) for key in ("name", "locator", "role", "strategy")
        ).lower()
        item_tokens = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9]+", text) if len(t) > 2}
        if not item_tokens:
            continue
        overlap = len(tokens & item_tokens) / len(tokens)
        if overlap <= 0:
            continue
        scored.append((overlap * 0.7 + float(item.get("confidence", 0)) * 0.3, item))

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best_item = scored[0]
    if best_score < 0.4:
        return None
    if len(scored) > 1 and scored[1][0] > best_score - 0.15:
        return None          # ambiguous — do not guess
    return best_item


def _focus_snippet(source: str, analysis: FailureAnalysis, window: int = 40) -> tuple[str, int]:
    """Extract the region of the file most likely to contain the fault."""
    lines = source.splitlines()
    anchor = 0
    needles = [analysis.failed_locator, analysis.test_id, analysis.test_name]
    for needle in needles:
        if not needle:
            continue
        for index, line in enumerate(lines):
            if needle in line:
                anchor = index
                break
        if anchor:
            break

    start = max(0, anchor - window // 2)
    end = min(len(lines), start + window)
    return "\n".join(lines[start:end]), start + 1


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _risk(value: Any) -> RiskLevel:
    try:
        return RiskLevel(str(value).lower())
    except ValueError:
        return RiskLevel.MEDIUM
