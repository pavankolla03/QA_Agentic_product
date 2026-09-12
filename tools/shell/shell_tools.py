"""Guarded command execution.

Nothing runs unless its executable is on the allowlist in
``configs/security.yaml`` and its full command line is free of forbidden
patterns. Output is redacted before it is stored or shown to a model.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from packages.aiqa_types.enums import ToolCategory
from packages.security.guard import CommandGuard, PolicyViolation, WorkspaceGuard
from packages.security.redaction import redact
from tools.base import Tool, ToolResult

# Environment variables that must never reach a child process started by an agent.
_SENSITIVE_ENV_HINTS = (
    "SECRET", "PASSWORD", "PASSWD", "TOKEN", "API_KEY", "APIKEY", "PRIVATE_KEY",
    "CREDENTIAL", "AWS_", "AZURE_", "GCP_", "OPENAI", "ANTHROPIC", "GEMINI", "OPENROUTER",
)


def sanitized_env(extra: dict[str, str] | None = None, keep: tuple[str, ...] = ()) -> dict[str, str]:
    """Child-process environment with platform/provider secrets stripped."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in keep:
            env[key] = value
            continue
        if any(hint in upper for hint in _SENSITIVE_ENV_HINTS):
            continue
        env[key] = value
    env["CI"] = env.get("CI", "1")
    env["AIQA_MANAGED"] = "1"
    if extra:
        env.update(extra)
    return env


class RunCommandTool(Tool):
    name = "shell.run"
    category = ToolCategory.SHELL
    description = "Run an allowlisted command inside the project workspace and capture its output."
    mutating = True
    schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Full command line"},
            "cwd": {"type": "string", "description": "Repo-relative working directory"},
            "timeout": {"type": "integer"},
            "env": {"type": "object"},
        },
        "required": ["command"],
    }

    def _run(
        self,
        command: str | list[str],
        cwd: str = ".",
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
        **_: Any,
    ) -> ToolResult:
        guard = CommandGuard()
        full = guard.check(command)          # raises PolicyViolation when refused

        workspace = WorkspaceGuard(self.ctx.project_root)
        workdir = workspace.resolve_read(cwd)
        if not workdir.exists():
            return ToolResult.failure(f"working directory not found: {cwd}")

        if self.ctx.dry_run:
            return ToolResult.success(
                {"stdout": "", "stderr": "", "exit_code": 0},
                command=full, dry_run=True,
            )

        if self.ctx.tracker is not None:
            self.ctx.tracker.audit("command_exec", full, "allowed", f"cwd={cwd}")

        args = command if isinstance(command, list) else shlex.split(command, posix=(os.name != "nt"))
        started = time.perf_counter()
        try:
            proc = subprocess.run(  # noqa: S603 - command is allowlist-checked above
                args,
                cwd=str(workdir),
                capture_output=capture_output,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or guard.timeout_seconds,
                env=sanitized_env(env),
                shell=False,
            )
        except FileNotFoundError:
            return ToolResult.failure(f"executable not found on PATH: {args[0] if args else command}")
        except subprocess.TimeoutExpired as exc:
            return ToolResult.failure(
                f"command timed out after {exc.timeout}s: {full}",
                meta={"timeout": True},
            )

        duration = int((time.perf_counter() - started) * 1000)
        stdout = redact(proc.stdout or "")
        stderr = redact(proc.stderr or "")
        payload = {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": full,
            "duration_ms": duration,
        }
        if proc.returncode == 0:
            return ToolResult.success(payload, exit_code=0, duration_ms=duration)
        return ToolResult(
            ok=False,
            data=payload,
            error=f"exit code {proc.returncode}: {(stderr or stdout).strip()[:500]}",
            meta={"exit_code": proc.returncode, "duration_ms": duration},
        )


class WhichTool(Tool):
    """Capability probe — lets agents degrade gracefully when tooling is missing."""

    name = "shell.which"
    category = ToolCategory.SHELL
    description = "Check whether an executable is available on PATH."
    schema = {"type": "object", "properties": {"executable": {"type": "string"}}, "required": ["executable"]}

    def _run(self, executable: str, **_: Any) -> ToolResult:
        import shutil

        found = shutil.which(executable)
        if not found and os.name == "nt":
            for ext in (".cmd", ".exe", ".bat"):
                found = shutil.which(executable + ext)
                if found:
                    break
        return ToolResult.success(bool(found), path=found or "", executable=executable)


def probe_toolchain(project_root: str) -> dict[str, Any]:
    """One-shot environment capability report used at run start."""
    import shutil

    def has(exe: str) -> bool:
        if shutil.which(exe):
            return True
        return bool(os.name == "nt" and any(shutil.which(exe + e) for e in (".cmd", ".exe", ".bat")))

    root = Path(project_root)
    return {
        "node": has("node"),
        "npm": has("npm"),
        "npx": has("npx"),
        "git": has("git"),
        "python": has("python") or has("python3") or bool(sys.executable),
        "has_package_json": (root / "package.json").exists(),
        "has_playwright_config": any(
            (root / f"playwright.config{ext}").exists() for ext in (".ts", ".js", ".mjs", ".cjs")
        ),
        "has_node_modules": (root / "node_modules").exists(),
        "playwright_installed": (root / "node_modules" / "@playwright" / "test").exists(),
    }


SHELL_TOOLS = [RunCommandTool, WhichTool]
