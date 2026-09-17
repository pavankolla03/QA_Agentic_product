"""LangGraph orchestrator.

The pipeline expressed as a real `StateGraph`: named nodes, declared conditional
edges, and hard loop caps. What LangGraph actually buys here is a topology that
cannot silently drift from the documentation — the edges are declared, validated
at compile time, and `get_graph().draw_mermaid()` generates the architecture
diagram from the code.

**Durability is ours, not LangGraph's.** A checkpointer is deliberately *not*
used: the graph state holds a live :class:`AgentContext` (router, tool registry,
open handles) which is not serializable, and more importantly this platform must
survive a QA engineer approving a diff the next morning from a different
process. That requires the `runs` row, which the engine already owns. LangGraph
sequences the work within one execution; the database remembers it across them.

The node bodies are the same agents the built-in orchestrator drives, so the two
backends are behaviourally identical and either can be selected at runtime.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypedDict

from agents.base import AgentContext
from agents.orchestrator.graph import (
    END,
    GraphResult,
    Node,
    Orchestrator,
    _after_commit,
    _after_execution,
    _after_failure_analysis,
    _after_self_healing,
    _after_standards,
    _after_test_design,
    _input_summary,
    build_default_nodes,
    terminal_status,
)
from packages.agent_protocol import ApprovalRequired
from packages.aiqa_types.enums import RunMode, RunStatus

log = logging.getLogger("aiqa.langgraph")

try:  # pragma: no cover - exercised by the availability test
    from langgraph.graph import END as LG_END
    from langgraph.graph import StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover
    LANGGRAPH_AVAILABLE = False
    StateGraph = None  # type: ignore[assignment]
    LG_END = "__end__"


#: The real topology. Every node may additionally jump to END when the run
#: suspends for approval or fails, which the edge builder adds automatically.
REACHABLE: dict[str, tuple[str, ...]] = {
    "requirement": ("repository",),
    "repository": ("exploration", "execution"),          # execute_only/heal_only skip exploration
    "exploration": ("test_design",),
    "test_design": ("code_generation", "reporting"),     # plan_only stops here
    "code_generation": ("step_coverage",),
    "step_coverage": ("standards",),
    "standards": ("execution", "reporting"),
    "execution": ("failure_analysis", "commit", "reporting"),
    "failure_analysis": ("self_healing", "reporting"),
    "self_healing": ("failure_analysis", "commit", "reporting"),   # bounded loop back
    "commit": ("reporting",),
    "reporting": (),
}


def _last(_current: Any, incoming: Any) -> Any:
    """Reducer: last write wins. State here is a cursor, not an accumulator."""
    return incoming


class QAState(TypedDict, total=False):
    """The graph's own state.

    Deliberately thin. The artifacts (requirement, plan, bundle, execution) live
    on the :class:`AgentContext` and are persisted to the database by the engine;
    duplicating them here would create two sources of truth and a serialization
    problem, since the context holds live objects like the router and tools.
    """

    run_id: Annotated[str, _last]
    ctx: Annotated[Any, _last]              # the live AgentContext
    visited: Annotated[list[str], lambda a, b: (a or []) + [n for n in (b or []) if n]]
    suspended_at: Annotated[str, _last]
    approval_id: Annotated[str, _last]
    error: Annotated[str, _last]
    status: Annotated[str, _last]
    corrections: Annotated[int, _last]
    heals: Annotated[int, _last]
    reruns: Annotated[int, _last]


class LangGraphOrchestrator(Orchestrator):
    """Drives the same agents through a compiled LangGraph `StateGraph`."""

    def __init__(self, nodes: dict[str, Node] | None = None, entry: str = "requirement") -> None:
        super().__init__(nodes=nodes, entry=entry)
        if not LANGGRAPH_AVAILABLE:
            raise RuntimeError(
                "langgraph is not installed. `pip install langgraph`, or use the built-in "
                "orchestrator, which is behaviourally identical."
            )
        self._compiled: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def _build(self, entry: str) -> Any:
        """Compile the graph. Cached per entry point."""
        if entry in self._compiled:
            return self._compiled[entry]

        graph = StateGraph(QAState)

        for name, node in self.nodes.items():
            graph.add_node(name, self._make_node(name, node))

        graph.set_entry_point(entry)

        # Conditional edges mirror the built-in router functions exactly, so the
        # two backends cannot diverge.
        routers: dict[str, Any] = {
            "requirement": lambda _s: "repository",
            "repository": self._route_repository,
            "exploration": lambda _s: "test_design",
            "test_design": lambda s: _after_test_design(s["ctx"]),
            "code_generation": lambda _s: "step_coverage",
            "step_coverage": lambda _s: "standards",
            "standards": lambda s: _after_standards(s["ctx"]),
            "execution": lambda s: _after_execution(s["ctx"]),
            "failure_analysis": lambda s: _after_failure_analysis(s["ctx"]),
            "self_healing": lambda s: _after_self_healing(s["ctx"]),
            "commit": lambda s: _after_commit(s["ctx"]),
            "reporting": lambda _s: END,
        }

        # Declare the *reachable* targets per node, not "anything". This keeps
        # the rendered diagram honest and lets LangGraph validate the topology.
        for name, router in routers.items():
            targets = {target: target for target in REACHABLE[name]}
            targets[END] = LG_END          # every node can end early (suspend/fail)
            graph.add_conditional_edges(name, self._guarded(name, router), targets)

        # No checkpointer: the state carries a live AgentContext, and the
        # authoritative record of a run lives in the database (see the module
        # docstring). Compiling without one also keeps the graph state free of
        # any serialization constraint.
        compiled = graph.compile()
        self._compiled[entry] = compiled
        return compiled

    # ------------------------------------------------------------------ #
    def _make_node(self, name: str, node: Node):
        """Wrap one agent as a LangGraph node function."""

        async def _run_node(state: QAState) -> dict[str, Any]:
            ctx: AgentContext = state["ctx"]
            agent = node.agent

            skip = agent.skip_reason(ctx)
            if skip:
                ctx.note(f"skipping {name}: {skip}")
                if ctx.tracker:
                    ctx.tracker.emit("agent_skipped", f"{name} skipped: {skip}", agent=agent.name)
                return {"visited": [name]}

            try:
                if ctx.tracker:
                    with ctx.tracker.agent_span(
                        agent.name, input_summary=_input_summary(ctx, name), progress=agent.progress(ctx)
                    ) as trace:
                        await agent.run(ctx)
                        if not trace.output_summary:
                            trace.output_summary = f"{name} completed"
                else:
                    await agent.run(ctx)

            except ApprovalRequired as pause:
                ctx.note(f"waiting for human approval: {pause.request.title}")
                return {
                    "visited": [name],
                    "suspended_at": name,
                    "approval_id": pause.request.id,
                    "status": RunStatus.WAITING_APPROVAL.value,
                }

            except Exception as exc:  # noqa: BLE001
                log.exception("agent %s failed", name)
                message = f"{type(exc).__name__}: {exc}"
                if node.critical:
                    ctx.warn(f"{name} failed: {message}")
                    return {"visited": [name], "error": message, "status": RunStatus.FAILED.value}
                ctx.warn(f"{name} failed but is optional, continuing: {message}")

            return {"visited": [name]}

        return _run_node

    def _guarded(self, name: str, router: Any):
        """Stop routing once the graph has suspended or failed."""

        def _route(state: QAState) -> str:
            if state.get("suspended_at") or state.get("status") in (
                RunStatus.WAITING_APPROVAL.value,
                RunStatus.FAILED.value,
            ):
                return END
            # Loop protection lives on the edges, where it is visible.
            ctx: AgentContext = state["ctx"]
            if name == "self_healing" and ctx.iteration >= ctx.budget.max_healing_attempts:
                ctx.note(f"healing attempt cap reached ({ctx.iteration}); ending the loop")
                return "reporting"
            return router(state)

        return _route

    @staticmethod
    def _route_repository(state: QAState) -> str:
        ctx: AgentContext = state["ctx"]
        return "exploration" if ctx.mode not in (RunMode.EXECUTE_ONLY, RunMode.HEAL_ONLY) else "execution"

    # ------------------------------------------------------------------ #
    async def run(self, ctx: AgentContext, start_at: str = "") -> GraphResult:
        entry = start_at or self.entry_for(ctx.mode)
        compiled = self._build(entry)

        state: QAState = {
            "run_id": ctx.run_id, "ctx": ctx, "visited": [],
            "suspended_at": "", "approval_id": "", "error": "", "status": "",
            "corrections": 0, "heals": ctx.iteration, "reruns": 0,
        }
        config = {
            # A hard ceiling on graph steps: LangGraph raises rather than
            # looping, which is exactly the failure mode we want to be loud.
            "recursion_limit": len(self.nodes) * (ctx.max_iterations + 2) + 8,
        }

        try:
            final = await compiled.ainvoke(state, config=config)
        except Exception as exc:  # noqa: BLE001 - includes GraphRecursionError
            log.exception("langgraph run failed")
            return GraphResult(
                status=RunStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                visited=list(state.get("visited") or []),
            )

        visited = list(final.get("visited") or [])
        if final.get("suspended_at"):
            return GraphResult(
                status=RunStatus.WAITING_APPROVAL,
                suspended_at=final["suspended_at"],
                approval_id=final.get("approval_id", ""),
                visited=visited,
            )
        if final.get("status") == RunStatus.FAILED.value:
            return GraphResult(status=RunStatus.FAILED, error=final.get("error", ""), visited=visited)

        # Shared with the built-in graph rather than restated here. The two
        # orchestrators must reach the same verdict from the same context, and
        # the only way to guarantee that is for there to be one rule.
        return GraphResult(status=terminal_status(ctx), visited=visited)

    # ------------------------------------------------------------------ #
    def mermaid(self) -> str:
        """Render the graph as Mermaid, so docs cannot drift from the code."""
        try:
            return self._build(self.entry).get_graph().draw_mermaid()
        except Exception as exc:  # noqa: BLE001 - diagram rendering is best-effort
            return f"%% could not render: {exc}"


def build_orchestrator(prefer_langgraph: bool = True) -> Orchestrator:
    """Return the LangGraph orchestrator when available, else the built-in one.

    Both drive the same agents and produce the same :class:`GraphResult`; the
    built-in graph exists so the platform has no hard dependency on LangGraph
    and so durable resume is provably ours.
    """
    if prefer_langgraph and LANGGRAPH_AVAILABLE:
        try:
            return LangGraphOrchestrator(build_default_nodes())
        except Exception as exc:  # noqa: BLE001
            log.warning("falling back to the built-in orchestrator: %s", exc)
    return Orchestrator(build_default_nodes())
