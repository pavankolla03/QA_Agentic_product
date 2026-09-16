"""Agent contract and shared context.

Each agent is a small, single-purpose unit: it reads the shared
:class:`AgentContext`, does one job, writes its result back, and records a
trace. Agents never call each other — the orchestrator owns control flow, which
keeps the pipeline inspectable and makes any single agent testable alone.
"""

from __future__ import annotations

import abc
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from packages.agent_protocol import AgentFailure, ApprovalRequired  # noqa: F401
from packages.agent_protocol.permissions import TOOL_CAPABILITY, permissions_for
from packages.aiqa_types.enums import (
    AgentName,
    ApprovalKind,
    Capability,
    RiskLevel,
    RunMode,
    Severity,
)
from packages.aiqa_types.models import (
    ApprovalRequest,
    CodeBundle,
    ExecutionResult,
    ExplorationResult,
    FailureAnalysis,
    HealProposal,
    Project,
    RepoProfile,
    Requirement,
    RunReport,
    StandardsReport,
    TestPlan,
)
from packages.llm_provider.base import ChatMessage, LLMResponse
from services.model_router.router import ModelRouter, RouterBudget
from tools.base import ToolRegistry, ToolResult

log = logging.getLogger("aiqa.agents")


# Control-flow signals live in packages.agent_protocol so the observability layer
# can distinguish "waiting for a human" from "failed" without importing agents.


# =========================================================================== #
# Shared state
# =========================================================================== #
@dataclass
class AgentContext:
    """The blackboard every agent reads from and writes to."""

    run_id: str
    project: Project
    instruction: str
    mode: RunMode = RunMode.FULL

    # Infrastructure
    router: ModelRouter = field(default=None)          # type: ignore[assignment]
    tools: ToolRegistry = field(default=None)          # type: ignore[assignment]
    tracker: Any = None
    budget: RouterBudget = field(default_factory=RouterBudget)
    standards: dict[str, Any] = field(default_factory=dict)

    # Accumulated artifacts
    requirement: Requirement | None = None
    repo_profile: RepoProfile | None = None
    exploration: ExplorationResult | None = None

    # Knowledge layer (live objects — never placed in `metadata`, which is
    # JSON-serialized onto the run row).
    application_map: Any = None
    repository_map: Any = None
    test_knowledge: Any = None
    knowledge_graph: Any = None
    index_delta: Any = None
    test_plan: TestPlan | None = None
    code_bundle: CodeBundle | None = None
    standards_report: StandardsReport | None = None
    execution: ExecutionResult | None = None
    analyses: list[FailureAnalysis] = field(default_factory=list)
    heals: list[HealProposal] = field(default_factory=list)
    report: RunReport | None = None

    # Control
    approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    granted: set[str] = field(default_factory=set)     # approval kinds already granted
    auto_approve: bool = False
    iteration: int = 0
    max_iterations: int = 3
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retrieved_context: str = ""
    toolchain: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @property
    def project_root(self) -> str:
        return self.project.repository_path

    def note(self, text: str) -> None:
        self.notes.append(text)
        if self.tracker:
            self.tracker.log(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        if self.tracker:
            self.tracker.log(text, level=Severity.WARNING)

    def is_granted(self, kind: ApprovalKind) -> bool:
        return self.auto_approve or kind.value in self.granted

    def grant(self, kind: ApprovalKind) -> None:
        self.granted.add(kind.value)

    def requires_approval(self, kind: ApprovalKind) -> bool:
        """Consult the project's standards policy for this gate."""
        required = (self.standards.get("review", {}) or {}).get("require_human_approval_for", [])
        return kind.value in required


# =========================================================================== #
# Base agent
# =========================================================================== #
#: Upper bound for a retry that widens the output ceiling. Free models advertise
#: large contexts but are slow, and an unbounded retry turns one bad reply into
#: a multi-minute stall.
#:
#: 12,000 was too tight to be useful: code generation asks for 10,000, so the
#: retry it triggers got 20% more room and truncated again — two slow calls to
#: arrive at the deterministic scaffold anyway. A retry that cannot plausibly
#: fit the answer is worse than no retry at all.
_MAX_OUTPUT_TOKENS = 24000

#: How long one agent may spend *retrying* before it settles for the fallback.
#: Not a cap on a single call — a slow model finishing its answer is work, and
#: the request timeout already scales with how much output was asked for. This
#: bounds the multiplication: retries x a growing timeout is otherwise ten
#: minutes on one step with nothing on screen.
_AGENT_WALL_CLOCK_BUDGET = 360


class BaseAgent(abc.ABC):
    """One responsibility, one agent."""

    name: AgentName = AgentName.ORCHESTRATOR
    capability: Capability = Capability.FAST
    description: str = ""
    #: Agents marked optional are skipped (with a warning) when they fail.
    optional: bool = False

    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    async def run(self, ctx: AgentContext) -> None:
        """Do the work, mutating ``ctx`` in place."""

    def skip_reason(self, ctx: AgentContext) -> str:
        """Return a non-empty string to skip this agent for this run."""
        return ""

    def progress(self, ctx: AgentContext) -> float | None:
        return None

    # -- LLM helpers ---------------------------------------------------- #
    async def ask(
        self,
        ctx: AgentContext,
        system: str,
        user: str,
        *,
        task: str,
        json_mode: bool = False,
        capability: Capability | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        cacheable_prefix_chars: int = 0,
        retry: int = 0,
    ) -> LLMResponse:
        """Single LLM turn, routed by tier and charged to the run budget.

        ``cacheable_prefix_chars`` marks how much of the system prompt is stable
        across runs (standards, conventions, the application map). Providers that
        support prompt caching then bill that prefix once instead of every call.
        """
        messages: Sequence[ChatMessage] = [ChatMessage.system(system), ChatMessage.user(user)]
        return await ctx.router.complete(
            messages,
            capability=capability or self.capability,
            task=task,
            agent=self.name,
            json_mode=json_mode,
            max_tokens=max_tokens,
            temperature=temperature,
            budget=ctx.budget,
            run_id=ctx.run_id,
            cacheable_prefix_chars=cacheable_prefix_chars,
            retry=retry,
        )

    async def ask_json(
        self,
        ctx: AgentContext,
        system: str,
        user: str,
        *,
        task: str,
        fallback: Any = None,
        capability: Capability | None = None,
        max_tokens: int | None = None,
        retries: int = 1,
        cacheable_prefix_chars: int = 0,
    ) -> Any:
        """Ask for JSON and *guarantee* a usable Python object.

        Models drift from schemas. Rather than fail a whole QA run on a stray
        comma, we retry with an explicit repair instruction and then fall back to
        ``fallback`` so downstream agents always have something valid.

        The two ways this fails are not the same, and a live run against free
        models made that obvious:

        * **Malformed.** The model wrote prose around the JSON, or fenced it.
          Telling it to stop works.
        * **Truncated.** The reply hit the output ceiling mid-structure. Telling
          it to write valid JSON is useless — it will write the same long answer
          and be cut off in the same place. What it needs is more room, so the
          ceiling is raised for the retry and the model is asked to be terser.

        Conflating the two is how a run ends up silently falling back to the
        deterministic scaffold while looking like it succeeded.
        """
        attempt_system = system
        attempt_tokens = max_tokens
        last_text = ""
        deadline = time.monotonic() + _AGENT_WALL_CLOCK_BUDGET
        for attempt in range(retries + 1):
            if attempt and time.monotonic() > deadline:
                # Retries multiply: three attempts at a timeout that scales with
                # the requested output is ten minutes on one agent, and a chat
                # that shows nothing for ten minutes is a chat that is broken as
                # far as anyone using it is concerned. The fallback is worse
                # output, available now, and clearly labelled — which beats
                # better output nobody waited for.
                ctx.warn(
                    f"{self.name.value}: gave up after "
                    f"{_AGENT_WALL_CLOCK_BUDGET // 60} minute(s) of retries; using the fallback"
                )
                break
            response = await self.ask(
                ctx, attempt_system, user, task=task, json_mode=True,
                capability=capability, max_tokens=attempt_tokens,
                # Only the first attempt can hit the cache; a repair attempt
                # appends to the system prompt and changes the prefix.
                cacheable_prefix_chars=cacheable_prefix_chars if attempt == 0 else 0,
                retry=attempt,
            )
            last_text = response.text
            parsed = response.json()
            if parsed is not None:
                if response.finish_reason == "length":
                    # Parsed despite being cut off: real but incomplete content,
                    # so say so rather than pretending the plan is whole.
                    ctx.warn(
                        f"{self.name.value}: the model's reply hit the output limit and may be "
                        f"incomplete (task {task})"
                    )
                return parsed

            if attempt < retries:
                truncated = response.finish_reason == "length"
                if truncated:
                    ceiling = attempt_tokens or ctx.router.default_max_tokens
                    attempt_tokens = min(int(ceiling * 1.75), _MAX_OUTPUT_TOKENS)
                    attempt_system = (
                        system
                        + "\n\nYour previous reply was cut off before it finished. Reply again, "
                        "more concisely: fewer items, shorter strings, no repetition. It must be "
                        "a single complete JSON object."
                    )
                    ctx.warn(
                        f"{self.name.value}: reply truncated at the output limit; "
                        f"retrying with room for {attempt_tokens:,} tokens"
                    )
                else:
                    attempt_system = (
                        system
                        + "\n\nCRITICAL: your previous reply was not valid JSON. "
                        "Reply with a single JSON object and nothing else — no prose, no code fences."
                    )
                    ctx.warn(f"{self.name.value}: model returned non-JSON, retrying once")
        ctx.warn(
            f"{self.name.value}: could not obtain valid JSON after {retries + 1} attempt(s); "
            f"using deterministic fallback"
        )
        log.debug("unparseable model output: %s", last_text[:500])
        return fallback

    # -- tool helpers --------------------------------------------------- #
    def tool(self, ctx: AgentContext, name: str, **kwargs: Any) -> ToolResult:
        """Invoke a tool, subject to this agent's permission grant.

        Enforced here rather than trusted to the prompt: an agent reads
        untrusted content (repository files, DOM text, failure output), and a
        capability check is the only thing that cannot be talked out of.
        """
        if ctx.tools is None:
            return ToolResult.failure("no tool registry bound to this run")

        permissions = permissions_for(self.name.value)
        if not permissions.allows_tool(name):
            required = TOOL_CAPABILITY.get(name)
            message = (
                f"agent '{self.name.value}' is not permitted to use '{name}'"
                + (f" (requires {required.value})" if required else " (unknown tool)")
            )
            ctx.warn(message)
            if ctx.tracker:
                ctx.tracker.audit("policy_violation", name, "denied", message)
            return ToolResult.failure(message, rule="agent.permission")

        return ctx.tools.invoke(name, **kwargs)

    # -- approval helper ------------------------------------------------ #
    def request_approval(
        self,
        ctx: AgentContext,
        kind: ApprovalKind,
        title: str,
        description: str = "",
        risk: RiskLevel = RiskLevel.MEDIUM,
        payload: dict[str, Any] | None = None,
        diff_preview: str = "",
    ) -> None:
        """Raise :class:`ApprovalRequired` unless this gate is already satisfied.

        Auto-approval is allowed only when the risk is at or below the project's
        ``auto_approve_below_risk`` threshold — an explicit policy decision, not
        an agent's judgement call.
        """
        if ctx.is_granted(kind):
            return

        threshold = str((ctx.standards.get("review", {}) or {}).get("auto_approve_below_risk", "low"))
        try:
            threshold_rank = RiskLevel(threshold).rank
        except ValueError:
            threshold_rank = RiskLevel.LOW.rank

        if not ctx.requires_approval(kind) and risk.rank <= threshold_rank:
            ctx.note(f"auto-approved {kind.value} (risk={risk.value} ≤ policy threshold {threshold})")
            ctx.grant(kind)
            return

        request = ApprovalRequest(
            run_id=ctx.run_id,
            project_id=ctx.project.id,
            kind=kind,
            title=title,
            description=description,
            risk=risk,
            payload=payload or {},
            diff_preview=diff_preview[:120_000],
            requested_by=self.name.value,
        )
        ctx.approvals[request.id] = request
        raise ApprovalRequired(request)


# --------------------------------------------------------------------------- #
def json_block(value: Any, limit: int = 8000) -> str:
    """Compact JSON for prompt embedding."""
    try:
        text = json.dumps(value, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + "\n... (truncated)"
    return text
