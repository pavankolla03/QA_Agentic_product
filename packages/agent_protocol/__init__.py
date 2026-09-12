"""Agent protocol — the control-flow vocabulary shared across layers.

These live outside ``agents/`` on purpose. The observability layer needs to tell
"this agent failed" apart from "this agent is waiting for a human", and it must
do that without importing the agent layer (which imports observability). Putting
the control-flow signals in a leaf package keeps the dependency graph acyclic.
"""

from __future__ import annotations

from typing import Any


class AgentSignal(Exception):
    """Base class for non-error control flow raised by an agent."""


class ApprovalRequired(AgentSignal):
    """Suspend the run until a human responds.

    This is **not** a failure. The orchestrator catches it, persists the request,
    and returns; the run later resumes at the same node.
    """

    def __init__(self, request: Any) -> None:
        super().__init__(getattr(request, "title", "") or "approval required")
        self.request = request

    @property
    def kind(self) -> str:
        kind = getattr(self.request, "kind", "")
        return getattr(kind, "value", str(kind))


class AgentFailure(Exception):
    """A genuine, non-recoverable failure inside one agent."""


def is_control_signal(exc: BaseException) -> bool:
    """True when an exception is agent control flow rather than an error."""
    return isinstance(exc, AgentSignal)


__all__ = ["AgentSignal", "ApprovalRequired", "AgentFailure", "is_control_signal"]
