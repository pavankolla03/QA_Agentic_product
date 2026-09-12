"""Tool execution layer — every side effect the agents can cause.

``build_registry`` is the single place tools are wired up, so a deployment can
restrict the surface area (for example: no Git, no DB) by passing ``exclude``.
"""

from __future__ import annotations

from typing import Iterable

from tools.api.api_tools import API_TOOLS
from tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from tools.database.db_tools import DATABASE_TOOLS
from tools.filesystem.fs_tools import FILESYSTEM_TOOLS
from tools.git.git_tools import GIT_TOOLS
from tools.mobile.mobile_tools import MOBILE_TOOLS
from tools.playwright.pw_tools import PLAYWRIGHT_TOOLS
from tools.shell.shell_tools import SHELL_TOOLS
from tools.slack.notify_tools import NOTIFY_TOOLS

ALL_TOOL_CLASSES: list[type[Tool]] = [
    *FILESYSTEM_TOOLS,
    *SHELL_TOOLS,
    *GIT_TOOLS,
    *PLAYWRIGHT_TOOLS,
    *API_TOOLS,
    *DATABASE_TOOLS,
    *MOBILE_TOOLS,
    *NOTIFY_TOOLS,
]


def build_registry(ctx: ToolContext, exclude: Iterable[str] = ()) -> ToolRegistry:
    """Instantiate every tool against one run context."""
    excluded = set(exclude)
    registry = ToolRegistry(ctx)
    for cls in ALL_TOOL_CLASSES:
        if cls.name in excluded or str(cls.category.value) in excluded:
            continue
        registry.register(cls(ctx))
    return registry


__all__ = [
    "ALL_TOOL_CLASSES",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
]
