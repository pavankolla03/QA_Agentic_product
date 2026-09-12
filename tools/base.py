"""Tool contract.

The LLM reasons; tools act. Every side effect in the platform happens inside a
:class:`Tool`, which means every side effect is (a) policy-checked, (b) traced,
and (c) auditable. Tools return :class:`ToolResult` rather than raising, so an
agent can reason about a failure instead of crashing the run.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

from packages.aiqa_types.enums import ToolCategory
from packages.aiqa_types.models import ToolCallTrace
from packages.security.guard import PolicyViolation
from packages.security.redaction import redact


@dataclass
class ToolResult:
    ok: bool = True
    data: Any = None
    error: str = ""
    rule: str = ""                       # policy rule id when denied
    latency_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def success(data: Any = None, **meta: Any) -> ToolResult:
        return ToolResult(ok=True, data=data, meta=meta)

    @staticmethod
    def failure(error: str, rule: str = "", **meta: Any) -> ToolResult:
        return ToolResult(ok=False, error=error, rule=rule, meta=meta)

    @staticmethod
    def denied(violation: PolicyViolation) -> ToolResult:
        return ToolResult(ok=False, error=violation.message, rule=violation.rule,
                          meta={"resource": violation.resource, "denied": True})

    def unwrap(self, default: Any = None) -> Any:
        return self.data if self.ok else default

    def summary(self, limit: int = 300) -> str:
        if not self.ok:
            return f"ERROR: {self.error}"[:limit]
        if self.data is None:
            return "ok"
        text = self.data if isinstance(self.data, str) else repr(self.data)
        return redact(text)[:limit]


@dataclass
class ToolContext:
    """Ambient state every tool needs: where it may act and who is watching."""

    project_root: str = "."
    project_id: str = ""
    run_id: str = ""
    user_id: str = ""
    tracker: Any = None                  # packages.observability_sdk.Tracker
    dry_run: bool = False
    env: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(abc.ABC):
    """Base class for every capability the agents can invoke."""

    name: str = "tool"
    category: ToolCategory = ToolCategory.FILESYSTEM
    description: str = ""
    mutating: bool = False               # True ⇒ may require approval
    schema: dict[str, Any] = {}

    def __init__(self, ctx: ToolContext | None = None) -> None:
        self.ctx = ctx or ToolContext()

    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    def _run(self, **kwargs: Any) -> ToolResult:
        """Implementation. Should return a ToolResult, may raise PolicyViolation."""

    def run(self, **kwargs: Any) -> ToolResult:
        """Traced, policy-safe invocation. Never raises."""
        started = time.perf_counter()
        tracker = self.ctx.tracker
        trace = ToolCallTrace(
            run_id=self.ctx.run_id,
            category=self.category,
            tool=self.name,
            arguments_preview=redact(_short_args(kwargs))[:400],
        )
        try:
            result = self._run(**kwargs)
        except PolicyViolation as exc:
            result = ToolResult.denied(exc)
            trace.status = "denied"
            trace.error = exc.message[:1000]
            if tracker is not None:
                tracker.audit("policy_violation", exc.resource, "denied", exc.message)
        except Exception as exc:  # noqa: BLE001 - tools must degrade, not explode
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")
            trace.status = "failed"
            trace.error = str(exc)[:1000]
        else:
            trace.status = "succeeded" if result.ok else "failed"
            trace.error = result.error[:1000]

        result.latency_ms = trace.latency_ms = int((time.perf_counter() - started) * 1000)
        trace.result_preview = result.summary()
        if tracker is not None:
            try:
                tracker.record_tool_call(trace)
            except Exception:  # noqa: BLE001
                pass
        return result

    __call__ = run

    # ------------------------------------------------------------------ #
    def spec(self) -> dict[str, Any]:
        """Machine-readable descriptor (for tool-calling models / the UI)."""
        return {
            "name": self.name,
            "category": str(self.category.value),
            "description": self.description,
            "mutating": self.mutating,
            "parameters": self.schema or {"type": "object", "properties": {}},
        }


def _short_args(kwargs: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in kwargs.items():
        text = value if isinstance(value, str) else repr(value)
        if len(text) > 120:
            text = text[:117] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class ToolRegistry:
    """Holds the tool instances available to a run, bound to one context."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        tool.ctx = self.ctx
        self._tools[tool.name] = tool
        return tool

    def register_all(self, *tools: Tool) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"tool '{name}' is not registered")
        return tool

    def invoke(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(f"unknown tool '{name}'")
        return tool.run(**kwargs)

    def by_category(self, category: ToolCategory) -> list[Tool]:
        return [t for t in self._tools.values() if t.category == category]

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)
