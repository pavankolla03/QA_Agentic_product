"""Orchestrator — a resumable agent state graph.

Shaped like LangGraph (named nodes, conditional edges, an interrupt that suspends
execution) but implemented directly so the platform has no heavyweight graph
dependency and, more importantly, so a suspended run is **durable**: the graph
state lives in the database, not in a Python generator. A QA engineer can close
VS Code, approve a diff the next morning, and the run continues from the exact
node that asked.

Control flow:

    requirement → repository → exploration → test_design ─(approve)→
    code_generation → standards → execution ─(failures?)→
    failure_analysis → self_healing → execution (retry, bounded) → commit → reporting
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents.base import AgentContext, BaseAgent
from agents.code_generation.agent import CodeGenerationAgent
from agents.execution.agent import CommitAgent, ExecutionAgent
from agents.exploration.agent import ExplorationAgent
from agents.failure_analysis.agent import FailureAnalysisAgent
from agents.reporting.agent import ReportingAgent
from agents.repository.agent import RepositoryAgent
from agents.requirement.agent import RequirementAgent
from agents.self_healing.agent import SelfHealingAgent
from agents.standards.agent import StandardsAgent
from agents.test_design.agent import TestDesignAgent
from packages.agent_protocol import ApprovalRequired
from packages.aiqa_types.enums import AgentName, RunMode, RunStatus

log = logging.getLogger("aiqa.orchestrator")

END = "__end__"


@dataclass
class Node:
    """One step in the graph."""

    name: str
    agent: BaseAgent
    #: Returns the next node name, or END.
    route: Callable[[AgentContext], str]
    #: Nodes whose failure should abort the run rather than be recorded and skipped.
    critical: bool = True


@dataclass
class GraphResult:
    status: RunStatus
    suspended_at: str = ""
    approval_id: str = ""
    error: str = ""
    visited: list[str] = field(default_factory=list)


# =========================================================================== #
# Routing predicates
# =========================================================================== #
def _after_test_design(ctx: AgentContext) -> str:
    if ctx.mode == RunMode.PLAN_ONLY:
        return "reporting"
    return "code_generation"


def _after_standards(ctx: AgentContext) -> str:
    report = ctx.standards_report
    if report and not report.passed and not ctx.metadata.get("standards_acknowledged"):
        # Not a dead end: the execution node raises the high-risk approval that
        # lets a human accept the violations explicitly.
        ctx.warn(
            f"{report.error_count} standards error(s) — a human must acknowledge them before the files are written"
        )
    # GENERATE still visits `execution`: that node owns applying the approved
    # diff to the workspace, and stops itself before running the suite.
    return "execution"


def _after_execution(ctx: AgentContext) -> str:
    execution = ctx.execution
    if execution is None or ctx.metadata.get("execution_blocked"):
        return "reporting"
    if execution.failures:
        return "failure_analysis"
    if ctx.metadata.get("changes_applied") and ctx.mode in (RunMode.FULL, RunMode.AUTONOMOUS):
        return "commit"
    return "reporting"


def _after_failure_analysis(ctx: AgentContext) -> str:
    if ctx.mode in (RunMode.PLAN_ONLY, RunMode.GENERATE):
        return "reporting"
    if any(a.healable for a in ctx.analyses) and ctx.iteration < ctx.max_iterations:
        return "self_healing"
    return "reporting"


def _after_self_healing(ctx: AgentContext) -> str:
    """Loop back only while there is something left to gain."""
    ctx.iteration += 1
    execution = ctx.execution

    if execution is None:
        return "reporting"
    if not execution.failures:
        return "commit" if ctx.metadata.get("changes_applied") else "reporting"
    if ctx.iteration >= ctx.max_iterations:
        ctx.note(f"stopping the heal loop after {ctx.iteration} iteration(s) with {execution.failed} failure(s) left")
        return "reporting"

    # Only re-analyse if the previous pass actually changed something; otherwise
    # we would loop producing the same rejected proposals.
    if any(h.verified for h in ctx.heals):
        return "failure_analysis"
    ctx.note("no repair was verified in this iteration — ending the heal loop")
    return "reporting"


def _after_commit(ctx: AgentContext) -> str:
    return "reporting"


# =========================================================================== #
# Graph
# =========================================================================== #
class Orchestrator:
    """Builds and drives the agent graph for one run."""

    def __init__(self, nodes: dict[str, Node] | None = None, entry: str = "requirement") -> None:
        self.nodes = nodes or build_default_nodes()
        self.entry = entry

    # ------------------------------------------------------------------ #
    def plan_for(self, mode: RunMode) -> list[str]:
        """Static preview of the nodes a mode will visit (used by the UI)."""
        if mode == RunMode.PLAN_ONLY:
            return ["requirement", "repository", "exploration", "test_design", "reporting"]
        if mode == RunMode.GENERATE:
            return ["requirement", "repository", "exploration", "test_design",
                    "code_generation", "standards", "execution", "reporting"]
        if mode == RunMode.EXECUTE_ONLY:
            return ["repository", "execution", "failure_analysis", "reporting"]
        if mode == RunMode.HEAL_ONLY:
            return ["repository", "execution", "failure_analysis", "self_healing", "reporting"]
        return ["requirement", "repository", "exploration", "test_design", "code_generation",
                "standards", "execution", "failure_analysis", "self_healing", "commit", "reporting"]

    def entry_for(self, mode: RunMode) -> str:
        if mode in (RunMode.EXECUTE_ONLY, RunMode.HEAL_ONLY):
            return "repository"
        return self.entry

    # ------------------------------------------------------------------ #
    async def run(self, ctx: AgentContext, start_at: str = "") -> GraphResult:
        """Execute the graph until it completes, fails, or needs a human.

        ``start_at`` resumes a suspended run at the node that requested approval.
        """
        current = start_at or self.entry_for(ctx.mode)
        visited: list[str] = []
        guard = 0
        max_steps = len(self.nodes) * (ctx.max_iterations + 2) + 8

        while current and current != END:
            guard += 1
            if guard > max_steps:
                ctx.warn(f"orchestrator step limit reached at '{current}' — stopping to avoid a loop")
                return GraphResult(status=RunStatus.FAILED, error="graph step limit exceeded", visited=visited)

            node = self.nodes.get(current)
            if node is None:
                return GraphResult(status=RunStatus.FAILED, error=f"unknown node '{current}'", visited=visited)

            visited.append(current)
            agent = node.agent

            skip = agent.skip_reason(ctx)
            if skip:
                ctx.note(f"skipping {current}: {skip}")
                if ctx.tracker:
                    ctx.tracker.emit("agent_skipped", f"{current} skipped: {skip}", agent=agent.name)
                current = node.route(ctx)
                continue

            try:
                if ctx.tracker:
                    with ctx.tracker.agent_span(
                        agent.name, input_summary=_input_summary(ctx, current), progress=agent.progress(ctx)
                    ) as trace:
                        await agent.run(ctx)
                        if not trace.output_summary:
                            trace.output_summary = f"{current} completed"
                else:
                    await agent.run(ctx)

            except ApprovalRequired as pause:
                ctx.note(f"waiting for human approval: {pause.request.title}")
                return GraphResult(
                    status=RunStatus.WAITING_APPROVAL,
                    suspended_at=current,
                    approval_id=pause.request.id,
                    visited=visited,
                )

            except Exception as exc:  # noqa: BLE001 - one agent must not lose the whole run
                log.exception("agent %s failed", current)
                message = f"{type(exc).__name__}: {exc}"
                if node.critical:
                    ctx.warn(f"{current} failed: {message}")
                    return GraphResult(status=RunStatus.FAILED, error=message, visited=visited)
                ctx.warn(f"{current} failed but is optional, continuing: {message}")

            current = node.route(ctx)

        # Nothing left to do.
        failed = bool(ctx.execution and ctx.execution.failures)
        blocked = bool(ctx.metadata.get("execution_blocked"))
        status = RunStatus.SUCCEEDED
        if failed or blocked:
            # A run that correctly reported failing tests did its job; the run
            # itself only "fails" when the platform could not complete its work.
            status = RunStatus.SUCCEEDED if ctx.report else RunStatus.FAILED
        return GraphResult(status=status, visited=visited)


def _input_summary(ctx: AgentContext, node: str) -> str:
    bits = [f"node={node}", f"mode={ctx.mode.value}", f"iteration={ctx.iteration}"]
    if ctx.requirement:
        bits.append(f"requirement={ctx.requirement.title[:60]}")
    if ctx.test_plan:
        bits.append(f"scenarios={ctx.test_plan.scenario_count}")
    if ctx.execution:
        bits.append(f"last_run={ctx.execution.passed}/{ctx.execution.total}")
    return "; ".join(bits)


def build_default_nodes() -> dict[str, Node]:
    return {
        "requirement": Node(
            "requirement", RequirementAgent(), lambda ctx: "repository", critical=True
        ),
        "repository": Node(
            "repository", RepositoryAgent(),
            lambda ctx: "exploration" if ctx.mode not in (RunMode.EXECUTE_ONLY, RunMode.HEAL_ONLY) else "execution",
            critical=True,
        ),
        "exploration": Node(
            "exploration", ExplorationAgent(), lambda ctx: "test_design", critical=False
        ),
        "test_design": Node("test_design", TestDesignAgent(), _after_test_design, critical=True),
        "code_generation": Node("code_generation", CodeGenerationAgent(), lambda ctx: "standards", critical=True),
        "standards": Node("standards", StandardsAgent(), _after_standards, critical=False),
        "execution": Node("execution", ExecutionAgent(), _after_execution, critical=True),
        "failure_analysis": Node("failure_analysis", FailureAnalysisAgent(), _after_failure_analysis, critical=False),
        "self_healing": Node("self_healing", SelfHealingAgent(), _after_self_healing, critical=False),
        "commit": Node("commit", CommitAgent(), _after_commit, critical=False),
        "reporting": Node("reporting", ReportingAgent(), lambda ctx: END, critical=False),
    }


AGENT_CATALOG: dict[str, BaseAgent] = {
    AgentName.REQUIREMENT.value: RequirementAgent(),
    AgentName.REPOSITORY.value: RepositoryAgent(),
    AgentName.EXPLORATION.value: ExplorationAgent(),
    AgentName.TEST_DESIGN.value: TestDesignAgent(),
    AgentName.CODE_GENERATION.value: CodeGenerationAgent(),
    AgentName.STANDARDS.value: StandardsAgent(),
    AgentName.EXECUTION.value: ExecutionAgent(),
    AgentName.FAILURE_ANALYSIS.value: FailureAnalysisAgent(),
    AgentName.SELF_HEALING.value: SelfHealingAgent(),
    AgentName.REPORTING.value: ReportingAgent(),
}


def describe_agents() -> list[dict[str, Any]]:
    """Agent roster for the control plane / extension UI."""
    return [
        {
            "name": name,
            "capability": agent.capability.value,
            "description": agent.description,
            "optional": agent.optional,
        }
        for name, agent in AGENT_CATALOG.items()
    ]
