"""Failure Analysis Agent.

The single most valuable judgement in autonomous QA: **is this a broken test or a
broken product?** Getting it wrong in one direction hides real defects; getting it
wrong in the other direction "heals" a test until it stops detecting a genuine
bug.

So the classification is layered:

1. A deterministic signature matcher handles the unambiguous cases (locator
   resolved to 0 elements, ECONNREFUSED, timeout waiting for selector). These are
   high-confidence and free.
2. The model is consulted for the ambiguous remainder, with the error, the stack,
   the failing step and the current DOM evidence.
3. A safety override runs last: anything that looks like an assertion on business
   data is never marked healable, regardless of what the model said.
"""

from __future__ import annotations

import re
from typing import Any

from agents.base import AgentContext, BaseAgent, json_block
from packages.aiqa_types.enums import (
    AgentName,
    Capability,
    FailureCategory,
    HealStrategy,
)
from packages.aiqa_types.models import FailureAnalysis, TestCaseResult

SYSTEM = """You are a test-failure triage specialist. Decide whether a failure is caused by the TEST or by the APPLICATION.

Categories:
- locator_broken: the element could not be found/resolved (renamed, moved, re-rendered). Test-side.
- timing_flake: the test raced the UI; the element/state arrives later. Test-side.
- test_data: data collision, stale seed data, missing prerequisite record. Test-side.
- test_logic_error: the test does the wrong thing (wrong order, wrong assumption). Test-side.
- environment: environment down/unhealthy, 5xx, DNS, container not ready. Not a code fault.
- network: transient connectivity. Not a code fault.
- configuration: wrong config/base URL/credentials wiring. Not a code fault.
- dependency: an upstream service or fixture failed.
- assertion_mismatch: the application produced a different value than expected. Likely APPLICATION.
- application_bug: the application clearly misbehaved. APPLICATION.

Critical instruction: if the evidence suggests the application produced wrong data, wrong state, or a wrong
message — classify it as assertion_mismatch or application_bug and set is_product_defect true. Never
classify a genuine defect as a test problem to make it "fixable".

Reply with ONE JSON object:
{"category": str, "confidence": float, "root_cause": str, "evidence": [str],
 "suggested_strategy": "relocate_selector|add_explicit_wait|replace_hard_wait|refresh_test_data|update_expected_value|retry_with_backoff|fix_step_logic|no_action",
 "is_product_defect": bool, "defect_summary": str, "recommended_action": str}"""


# --------------------------------------------------------------------------- #
# Deterministic signatures — ordered most-specific first.
# --------------------------------------------------------------------------- #
_SIGNATURES: list[tuple[re.Pattern[str], FailureCategory, HealStrategy, float]] = [
    (re.compile(r"resolved to 0 elements|strict mode violation.*resolved to 0|no element matches", re.I),
     FailureCategory.LOCATOR_BROKEN, HealStrategy.RELOCATE_SELECTOR, 0.93),
    (re.compile(r"strict mode violation.*resolved to \d+ elements", re.I),
     FailureCategory.LOCATOR_BROKEN, HealStrategy.RELOCATE_SELECTOR, 0.88),
    (re.compile(r"waiting for (?:locator|selector|element).*(?:to be visible|to be enabled|to be attached)", re.I),
     FailureCategory.TIMING_FLAKE, HealStrategy.ADD_EXPLICIT_WAIT, 0.8),
    (re.compile(r"ECONNREFUSED|ENOTFOUND|EAI_AGAIN|socket hang up|net::ERR_CONNECTION", re.I),
     FailureCategory.ENVIRONMENT, HealStrategy.RETRY_WITH_BACKOFF, 0.92),
    (re.compile(r"\b(50[0234])\b.*(?:Gateway|Unavailable|Internal Server|Timeout)|Internal Server Error", re.I),
     FailureCategory.ENVIRONMENT, HealStrategy.RETRY_WITH_BACKOFF, 0.85),
    (re.compile(r"(?:duplicate key|already exists|unique constraint|23505)", re.I),
     FailureCategory.TEST_DATA, HealStrategy.REFRESH_TEST_DATA, 0.87),
    (re.compile(r"(?:401|403)\b|Unauthorized|Forbidden|invalid credentials", re.I),
     FailureCategory.CONFIGURATION, HealStrategy.NO_ACTION, 0.78),
    (re.compile(r"Test timeout of \d+ms exceeded", re.I),
     FailureCategory.TIMING_FLAKE, HealStrategy.ADD_EXPLICIT_WAIT, 0.7),
    (re.compile(r"waitForTimeout|Thread\.sleep", re.I),
     FailureCategory.TIMING_FLAKE, HealStrategy.REPLACE_HARD_WAIT, 0.72),
    (re.compile(r"is not a function|undefined is not|Cannot read propert|TypeError|ReferenceError", re.I),
     FailureCategory.TEST_LOGIC_ERROR, HealStrategy.FIX_STEP_LOGIC, 0.75),
    (re.compile(r"Cannot find module|Module not found", re.I),
     FailureCategory.DEPENDENCY, HealStrategy.NO_ACTION, 0.9),
]

# Signals that the *application* produced the wrong answer.
# DOTALL matters: Playwright prints "Expected string: ...\nReceived string: ..."
# across two lines, and without it the most common product-defect shape of all
# would slip through the safety override.
_PRODUCT_SIGNALS = re.compile(
    r"(?:expected\s+(?:string|value|pattern|substring|array|object)?[:\s].{0,120}received|"
    r"toHaveText|toContainText|toHaveValue|toEqual|toBe\(|"
    r"expected .{0,120} but (?:got|received))",
    re.I | re.DOTALL,
)

#: Above this confidence a deterministic signature is authoritative and the
#: model is not consulted for the category at all. A specific regex on a known
#: framework error message is more reliable than an LLM's impression of it.
_DETERMINISTIC_TRUST = 0.75


def _repairable_path(case: TestCaseResult) -> str:
    """The file a repair actually belongs in.

    `case.file_path` identifies the test. Under Cucumber that is the .feature,
    and the healer took it literally: it appended a TypeScript step definition
    to the bottom of a Gherkin file, which then failed to parse and took the
    whole suite with it. The runner knows where the step's code lives; use that
    when it does.
    """
    return case.code_path or case.file_path


class FailureAnalysisAgent(BaseAgent):
    name = AgentName.FAILURE_ANALYSIS
    capability = Capability.REASONING
    description = "Classifies each failure as a test problem or a product defect, with a root cause."

    def progress(self, ctx: AgentContext) -> float:
        return 0.85

    def skip_reason(self, ctx: AgentContext) -> str:
        if ctx.execution is None:
            return "nothing was executed"
        if not ctx.execution.failures:
            return "no failures to analyse"
        return ""

    async def run(self, ctx: AgentContext) -> None:
        execution = ctx.execution
        assert execution is not None
        analyses: list[FailureAnalysis] = []

        for case in execution.failures[: ctx.metadata.get("max_analyses", 15)]:
            analysis = self._deterministic(ctx, case)
            if analysis is None or analysis.confidence < _DETERMINISTIC_TRUST:
                model_analysis = await self._ask_model(ctx, case, analysis)
                if model_analysis is not None:
                    # Keep whichever verdict is better supported. A model that is
                    # less sure than the regex does not get to overrule it.
                    if analysis is None or model_analysis.confidence >= analysis.confidence:
                        analysis = model_analysis
            if analysis is None:
                analysis = FailureAnalysis(
                    run_id=ctx.run_id, test_id=case.test_id or case.name, test_name=case.name,
                    file_path=_repairable_path(case), category=FailureCategory.UNKNOWN, confidence=0.2,
                    root_cause="Could not determine a root cause from the available output.",
                    recommended_action="Inspect the trace/screenshot manually.",
                )

            self._safety_override(analysis, case)
            analysis.run_id = ctx.run_id
            analysis.healable = analysis.category.healable and not analysis.is_product_defect
            analyses.append(analysis)

        ctx.analyses = analyses

        product_defects = [a for a in analyses if a.is_product_defect]
        healable = [a for a in analyses if a.healable]
        counts: dict[str, int] = {}
        for analysis in analyses:
            counts[analysis.category.value] = counts.get(analysis.category.value, 0) + 1

        summary = (
            f"analysed {len(analyses)} failure(s): "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            + f"; {len(healable)} healable, {len(product_defects)} suspected product defect(s)"
        )
        ctx.note(summary)
        for analysis in product_defects:
            ctx.warn(
                f"SUSPECTED PRODUCT DEFECT in {analysis.test_id}: {analysis.defect_summary or analysis.root_cause}"
            )

        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = summary

    # ------------------------------------------------------------------ #
    def _deterministic(self, ctx: AgentContext, case: TestCaseResult) -> FailureAnalysis | None:
        haystack = "\n".join([case.error_message, case.error_stack, case.stdout])
        if not haystack.strip():
            return None
        for pattern, category, strategy, confidence in _SIGNATURES:
            match = pattern.search(haystack)
            if not match:
                continue
            return FailureAnalysis(
                run_id=ctx.run_id,
                test_id=case.test_id or case.name,
                test_name=case.name,
                file_path=_repairable_path(case),
                category=category,
                confidence=confidence,
                root_cause=_root_cause_for(category, case),
                evidence=[match.group(0)[:200]],
                failed_locator=case.failed_locator,
                suggested_strategy=strategy,
                recommended_action=(
                    "Apply an automated repair and re-run"
                    if category.healable
                    else "Investigate the environment/configuration; this is not a test-code fault"
                ),
            )
        return None

    async def _ask_model(
        self, ctx: AgentContext, case: TestCaseResult, prior: FailureAnalysis | None
    ) -> FailureAnalysis | None:
        dom_hint = ""
        catalog = ctx.metadata.get("locator_catalog", [])
        if case.failed_locator and catalog:
            dom_hint = json_block(
                [item for item in catalog if item.get("role") in ("button", "textbox", "combobox")][:25],
                limit=3500,
            )

        user = "\n".join(
            [
                f"Test: {case.test_id or case.name}",
                f"File: {_repairable_path(case)}",
                f"Status: {case.status.value} after {case.retries} retry/retries ({case.duration_ms} ms)",
                f"Failing step: {case.failed_step or 'unknown'}",
                f"Failing locator: {case.failed_locator or 'none extracted'}",
                "",
                "Error message:",
                case.error_message[:3000] or "(empty)",
                "",
                "Stack (truncated):",
                case.error_stack[:2000] or "(empty)",
                "",
                ("Currently observed elements on the application:\n" + dom_hint) if dom_hint else "",
                (f"\nA deterministic matcher suggested: {prior.category.value} (confidence {prior.confidence:.2f}). "
                 f"Confirm or correct it." if prior else ""),
                "",
                "Classify this failure.",
            ]
        )
        raw = await self.ask_json(
            ctx, SYSTEM, user, task="failure_analysis.classify", fallback=None, max_tokens=1500
        )
        if not isinstance(raw, dict):
            return prior

        try:
            category = FailureCategory(str(raw.get("category", "unknown")).lower())
        except ValueError:
            category = prior.category if prior else FailureCategory.UNKNOWN
        try:
            strategy = HealStrategy(str(raw.get("suggested_strategy", "no_action")).lower())
        except ValueError:
            strategy = HealStrategy.NO_ACTION

        return FailureAnalysis(
            run_id=ctx.run_id,
            test_id=case.test_id or case.name,
            test_name=case.name,
            file_path=_repairable_path(case),
            category=category,
            confidence=_clamp(raw.get("confidence"), 0.5),
            root_cause=str(raw.get("root_cause", ""))[:1500] or (prior.root_cause if prior else ""),
            evidence=[str(e)[:200] for e in (raw.get("evidence") or [])][:5],
            failed_locator=case.failed_locator,
            suggested_strategy=strategy,
            is_product_defect=bool(raw.get("is_product_defect", False)) or category.is_product_defect,
            defect_summary=str(raw.get("defect_summary", ""))[:600],
            recommended_action=str(raw.get("recommended_action", ""))[:500],
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _safety_override(analysis: FailureAnalysis, case: TestCaseResult) -> None:
        """Last-line protection against healing away a real defect.

        If the failure text shows a value-level assertion mismatch, we refuse to
        treat it as a test problem no matter how the model classified it. A
        false "product defect" costs a human five minutes; a false "just fix the
        test" can ship a bug.
        """
        haystack = f"{case.error_message}\n{case.error_stack}"
        looks_like_assertion = bool(_PRODUCT_SIGNALS.search(haystack))
        locator_missing = bool(re.search(r"resolved to 0 elements|no element matches", haystack, re.I))

        if looks_like_assertion and not locator_missing:
            if analysis.category in (
                FailureCategory.LOCATOR_BROKEN,
                FailureCategory.TIMING_FLAKE,
                FailureCategory.TEST_DATA,
                FailureCategory.TEST_LOGIC_ERROR,
            ):
                analysis.evidence.append(
                    f"safety override: value-level assertion mismatch detected, "
                    f"downgraded from {analysis.category.value}"
                )
                analysis.category = FailureCategory.ASSERTION_MISMATCH
                analysis.suggested_strategy = HealStrategy.NO_ACTION
                analysis.confidence = min(analysis.confidence, 0.6)
            analysis.is_product_defect = True
            if not analysis.defect_summary:
                analysis.defect_summary = (
                    f"{case.name}: the application returned a value that differs from the expectation. "
                    f"{case.error_message.splitlines()[0][:200] if case.error_message else ''}"
                )
            analysis.recommended_action = (
                analysis.recommended_action
                or "Do not auto-heal. Confirm the expected behaviour with the product owner and raise a defect."
            )

        if analysis.suggested_strategy == HealStrategy.UPDATE_EXPECTED_VALUE:
            # Rewriting an expectation to match observed behaviour is how a suite
            # silently stops testing anything. Always a human decision.
            analysis.is_product_defect = True
            analysis.recommended_action = (
                "Updating an expected value requires human confirmation that the new behaviour is correct."
            )


def _root_cause_for(category: FailureCategory, case: TestCaseResult) -> str:
    locator = case.failed_locator or "the target element"
    return {
        FailureCategory.LOCATOR_BROKEN: f"{locator} no longer resolves in the application DOM.",
        FailureCategory.TIMING_FLAKE: f"The step acted on {locator} before the application had settled.",
        FailureCategory.ENVIRONMENT: "The application under test was unreachable or returned a server error.",
        FailureCategory.TEST_DATA: "Test data collided with pre-existing state; the dataset is not isolated.",
        FailureCategory.CONFIGURATION: "Authentication or configuration for the target environment is wrong.",
        FailureCategory.TEST_LOGIC_ERROR: "The test code raised a runtime error before reaching its assertion.",
        FailureCategory.DEPENDENCY: "A required module or fixture could not be resolved.",
    }.get(category, "See the captured error output.")


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
