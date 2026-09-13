"""Requirement Agent — turn an instruction into testable acceptance criteria.

Input is whatever the QA engineer typed ("Automate the Resident Registration
functionality") plus optionally a Jira issue. Output is a structured
:class:`Requirement`. Ambiguity is measured, not hidden: if the instruction is
too vague, the agent says so and surfaces the questions a human should answer.
"""

from __future__ import annotations

import re
from typing import Any

from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import AgentName, Capability
from packages.aiqa_types.models import AcceptanceCriterion, Requirement
from tools.jira.jira_tools import ISSUE_KEY_RE

SYSTEM = """You are a senior QA requirements analyst. You convert a feature request into precise, \
testable acceptance criteria for automated testing.

Rules:
- Every acceptance criterion must be objectively verifiable by an automated test. No "works well", no "is fast".
- Cover the happy path, mandatory-field/validation behaviour, at least one negative case, boundary values, \
and persistence/retrieval where relevant.
- Identify preconditions (login state, seed data, permissions) explicitly — automation needs them.
- If information is genuinely missing, list it under open_questions instead of inventing business rules.
- ambiguity_score: 0.0 = fully specified, 1.0 = unusable. Be honest.

Reply with ONE JSON object using exactly these keys:
{"title": str, "summary": str, "feature_area": str, "actors": [str], "preconditions": [str],
 "acceptance_criteria": [{"text": str, "testable": bool, "rationale": str}],
 "business_rules": [str], "data_requirements": [str], "out_of_scope": [str],
 "open_questions": [str], "ambiguity_score": float}"""


class RequirementAgent(BaseAgent):
    name = AgentName.REQUIREMENT
    capability = Capability.REASONING
    description = "Converts a free-text or Jira request into structured, testable acceptance criteria."

    def progress(self, ctx: AgentContext) -> float:
        return 0.08

    async def run(self, ctx: AgentContext) -> None:
        source = "chat"
        source_ref: str | None = None
        requirement_text = ctx.instruction

        # Pull a Jira issue when the instruction references one.
        issue_key = ctx.metadata.get("jira_issue") or _first_issue_key(ctx.instruction)
        if issue_key:
            result = self.tool(ctx, "jira.fetch_issue", issue_key=issue_key)
            if result.ok and result.data:
                data = result.data
                requirement_text = f"{ctx.instruction}\n\n--- Jira {data['key']} ---\n{data['requirement_text']}"
                source, source_ref = "jira", data.get("url") or data["key"]
                ctx.note(f"loaded Jira issue {data['key']}: {data.get('summary', '')[:120]}")
            else:
                ctx.warn(f"could not load Jira issue {issue_key}: {result.error[:160]}")

        context_lines = [
            f"Project: {ctx.project.name}",
            f"Application under test: {ctx.project.base_url or 'not configured'}",
            f"Framework: {ctx.project.framework} ({ctx.project.language})",
        ]
        if ctx.repo_profile and ctx.repo_profile.existing_features:
            context_lines.append(
                "Feature areas already automated: " + ", ".join(ctx.repo_profile.existing_features[:15])
            )

        user = (
            "\n".join(context_lines)
            + "\n\nQA engineer's request:\n"
            + requirement_text.strip()
            + "\n\nProduce the structured requirement JSON."
        )

        raw = await self.ask_json(
            ctx, SYSTEM, user,
            task="requirement.analyze",
            fallback=_heuristic_requirement(requirement_text),
            max_tokens=3000,
        )
        requirement = _to_requirement(raw, requirement_text, source, source_ref)
        ctx.requirement = requirement

        if requirement.ambiguity_score >= 0.75:
            ctx.warn(
                f"requirement is highly ambiguous (score {requirement.ambiguity_score:.2f}); "
                f"open questions: {'; '.join(requirement.open_questions[:3])}"
            )

        testable = sum(1 for c in requirement.acceptance_criteria if c.testable)
        ctx.note(
            f"requirement '{requirement.title}': {testable}/{len(requirement.acceptance_criteria)} "
            f"testable criteria, ambiguity {requirement.ambiguity_score:.2f}"
        )
        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = f"{len(requirement.acceptance_criteria)} acceptance criteria"


def _first_issue_key(text: str) -> str:
    match = ISSUE_KEY_RE.search(text or "")
    return match.group(1) if match else ""


def _to_requirement(raw: Any, raw_input: str, source: str, source_ref: str | None) -> Requirement:
    data = raw if isinstance(raw, dict) else {}
    criteria: list[AcceptanceCriterion] = []
    for item in data.get("acceptance_criteria", []) or []:
        if isinstance(item, dict) and item.get("text"):
            criteria.append(
                AcceptanceCriterion(
                    text=str(item["text"]).strip(),
                    testable=bool(item.get("testable", True)),
                    rationale=str(item.get("rationale", "")),
                )
            )
        elif isinstance(item, str) and item.strip():
            criteria.append(AcceptanceCriterion(text=item.strip()))

    def strings(key: str) -> list[str]:
        value = data.get(key) or []
        if isinstance(value, str):
            return [value]
        return [str(v).strip() for v in value if str(v).strip()]

    title = str(data.get("title") or "").strip() or _title_from(raw_input)
    return Requirement(
        raw_input=raw_input[:20000],
        title=title,
        summary=str(data.get("summary", "")).strip(),
        feature_area=str(data.get("feature_area", "")).strip() or title,
        actors=strings("actors"),
        preconditions=strings("preconditions"),
        acceptance_criteria=criteria or _heuristic_criteria(raw_input),
        business_rules=strings("business_rules"),
        data_requirements=strings("data_requirements"),
        out_of_scope=strings("out_of_scope"),
        open_questions=strings("open_questions"),
        ambiguity_score=_as_float(data.get("ambiguity_score"), default=0.4),
        source=source,  # type: ignore[arg-type]
        source_ref=source_ref,
    )


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


_NOISE = re.compile(
    r"(?i)^\s*(please\s+)?(automate|create|write|generate|add|build|make)\s+(tests?\s+for\s+|test\s+cases?\s+for\s+|the\s+)?"
)


def _title_from(text: str) -> str:
    cleaned = _NOISE.sub("", (text or "").strip().splitlines()[0] if text.strip() else "")
    cleaned = re.sub(r"(?i)\s+(functionality|feature|module|flow|screen|page)\s*$", "", cleaned).strip(" .")
    if not cleaned:
        return "Untitled feature"
    return cleaned[:120].strip().capitalize() if cleaned.islower() else cleaned[:120]


def _heuristic_criteria(text: str) -> list[AcceptanceCriterion]:
    subject = _title_from(text)
    return [
        AcceptanceCriterion(text=f"{subject} completes successfully with valid input"),
        AcceptanceCriterion(text=f"{subject} rejects invalid or missing mandatory input with a clear message"),
        AcceptanceCriterion(text=f"The result of {subject} is persisted and retrievable"),
    ]


def _heuristic_requirement(text: str) -> dict[str, Any]:
    """Deterministic fallback so the pipeline never stalls on a model hiccup."""
    subject = _title_from(text)
    return {
        "title": subject,
        "summary": f"Automate verification of {subject}.",
        "feature_area": subject,
        "actors": ["Authenticated user"],
        "preconditions": ["The application under test is reachable", "A valid test user exists"],
        "acceptance_criteria": [
            {"text": c.text, "testable": True, "rationale": "derived heuristically"}
            for c in _heuristic_criteria(text)
        ],
        "business_rules": [],
        "data_requirements": ["Valid dataset", "Invalid/boundary dataset"],
        "out_of_scope": [],
        "open_questions": ["Which fields are mandatory?", "Which user role performs this action?"],
        "ambiguity_score": 0.6,
    }
