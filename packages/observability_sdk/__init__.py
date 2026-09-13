"""Thin client-side observability contract used by agents and tools.

Agents receive a tracker through their context rather than importing the
service, which keeps the agent layer testable in isolation.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Tracker(Protocol):
    """The subset of RunTracker that agents and tools rely on."""

    run_id: str
    project_id: str

    def emit(self, type: str, message: str = "", **kwargs: Any) -> Any: ...
    def log(self, message: str, *args: Any, **kwargs: Any) -> Any: ...
    def audit(self, action: Any, resource: str = "", outcome: str = "allowed", detail: str = "", user_id: str = "") -> None: ...
    def record_tool_call(self, trace: Any) -> None: ...
    def tool_span(self, category: Any, tool: str, arguments: Any = "") -> Any: ...
    def agent_span(self, agent: Any, input_summary: str = "", progress: float | None = None) -> Any: ...


class NullTracker:
    """No-op tracker for unit tests and dry runs."""

    run_id = "run_null"
    project_id = ""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, type: str, message: str = "", **kwargs: Any) -> None:
        self.events.append((type, message))

    def log(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("log", message))

    def audit(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_tool_call(self, trace: Any) -> None:
        return None

    def tool_span(self, category: Any, tool: str, arguments: Any = "") -> Any:
        import contextlib

        from packages.aiqa_types.models import ToolCallTrace

        @contextlib.contextmanager
        def _span():
            yield ToolCallTrace(category=category, tool=tool)

        return _span()

    def agent_span(self, agent: Any, input_summary: str = "", progress: float | None = None) -> Any:
        import contextlib

        from packages.aiqa_types.models import AgentTrace

        @contextlib.contextmanager
        def _span():
            yield AgentTrace(agent=agent)

        return _span()


__all__ = ["Tracker", "NullTracker"]
