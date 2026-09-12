"""Deterministic policy enforcement.

The LLM *reasons*; these guards *decide*. No agent can write a file, run a
command, or touch Git without passing through here first. Every denial is
raised as :class:`PolicyViolation` and recorded in the audit log by the caller.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from configs.settings import load_security_config
from packages.aiqa_types.enums import RiskLevel


class PolicyViolation(RuntimeError):
    """Raised when an agent action is refused by policy."""

    def __init__(self, rule: str, message: str, resource: str = "") -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message
        self.resource = resource

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "message": self.message, "resource": self.resource}


# --------------------------------------------------------------------------- #
# Workspace confinement
# --------------------------------------------------------------------------- #
@dataclass
class WorkspaceGuard:
    """Confines all filesystem activity to one project root."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        cfg = load_security_config().get("workspace", {}) or {}
        self.enforce_root: bool = bool(cfg.get("enforce_root_confinement", True))
        self.follow_symlinks: bool = bool(cfg.get("follow_symlinks", False))
        self.max_file_bytes: int = int(cfg.get("max_file_bytes", 2_097_152))
        self.max_write_bytes: int = int(cfg.get("max_write_bytes", 524_288))
        self.deny_paths: list[str] = list(cfg.get("deny_paths", []) or [])
        self.write_allow_globs: list[str] = list(cfg.get("write_allow_globs", []) or [])

    # ------------------------------------------------------------------ #
    def _relative(self, path: str | Path) -> PurePosixPath:
        p = Path(path)
        abs_p = (self.root / p).resolve() if not p.is_absolute() else p.resolve()

        if self.enforce_root:
            try:
                rel = abs_p.relative_to(self.root)
            except ValueError as exc:
                raise PolicyViolation(
                    "workspace.root_confinement",
                    f"Path escapes the project workspace: {abs_p}",
                    str(path),
                ) from exc
        else:  # pragma: no cover - non-default
            rel = abs_p

        if not self.follow_symlinks and abs_p.is_symlink():
            raise PolicyViolation("workspace.symlink", f"Symlinks are not permitted: {path}", str(path))

        return PurePosixPath(rel.as_posix())

    def _denied(self, rel: PurePosixPath) -> str | None:
        text = str(rel)
        name = rel.name
        for pattern in self.deny_paths:
            if fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(name, pattern):
                return pattern
            # `**/x` should also match a bare top-level `x`
            if pattern.startswith("**/") and fnmatch.fnmatch(text, pattern[3:]):
                return pattern
        return None

    # ------------------------------------------------------------------ #
    def resolve_read(self, path: str | Path) -> Path:
        rel = self._relative(path)
        denied = self._denied(rel)
        if denied:
            raise PolicyViolation(
                "workspace.deny_path",
                f"Reading '{rel}' is blocked by policy pattern '{denied}' (secret-bearing file).",
                str(rel),
            )
        return self.root / rel

    def resolve_write(self, path: str | Path, size: int = 0) -> Path:
        rel = self._relative(path)
        denied = self._denied(rel)
        if denied:
            raise PolicyViolation(
                "workspace.deny_path",
                f"Writing '{rel}' is blocked by policy pattern '{denied}'.",
                str(rel),
            )
        if self.write_allow_globs and not any(
            fnmatch.fnmatch(str(rel), g) or fnmatch.fnmatch(str(rel), g.rstrip("/") + "/**")
            for g in self.write_allow_globs
        ):
            raise PolicyViolation(
                "workspace.write_allowlist",
                f"Writing '{rel}' is outside the allowed test directories "
                f"({', '.join(self.write_allow_globs)}). Agents may only modify test assets.",
                str(rel),
            )
        if size and size > self.max_write_bytes:
            raise PolicyViolation(
                "workspace.write_size",
                f"Write of {size} bytes exceeds the {self.max_write_bytes}-byte limit.",
                str(rel),
            )
        return self.root / rel

    def is_writable(self, path: str | Path) -> bool:
        try:
            self.resolve_write(path)
            return True
        except PolicyViolation:
            return False


# --------------------------------------------------------------------------- #
# Command allowlist
# --------------------------------------------------------------------------- #
@dataclass
class CommandGuard:
    """Allowlists the executables and rejects dangerous argument patterns."""

    def __post_init__(self) -> None:
        cfg = load_security_config().get("commands", {}) or {}
        self.allow: set[str] = {c.lower() for c in cfg.get("allow", []) or []}
        self.deny_patterns: list[re.Pattern[str]] = []
        for raw in cfg.get("deny_args_patterns", []) or []:
            try:
                self.deny_patterns.append(re.compile(raw, re.IGNORECASE))
            except re.error:
                continue
        self.timeout_seconds: int = int(cfg.get("timeout_seconds", 900))

    @staticmethod
    def _executable(command: str | list[str]) -> tuple[str, str]:
        if isinstance(command, list):
            parts = command
            full = " ".join(command)
        else:
            full = command
            try:
                parts = shlex.split(command, posix=False)
            except ValueError:
                parts = command.split()
        exe = Path(parts[0].strip('"')).name.lower() if parts else ""
        for suffix in (".exe", ".cmd", ".bat", ".ps1"):
            if exe.endswith(suffix):
                exe = exe[: -len(suffix)]
        return exe, full

    def check(self, command: str | list[str]) -> str:
        exe, full = self._executable(command)
        if not exe:
            raise PolicyViolation("command.empty", "Empty command rejected.", "")
        if self.allow and exe not in self.allow:
            raise PolicyViolation(
                "command.allowlist",
                f"Executable '{exe}' is not on the allowlist ({', '.join(sorted(self.allow))}).",
                full,
            )
        for pat in self.deny_patterns:
            if pat.search(full):
                raise PolicyViolation(
                    "command.dangerous_args",
                    f"Command matches a forbidden pattern ({pat.pattern}).",
                    full,
                )
        return full

    def is_allowed(self, command: str | list[str]) -> bool:
        try:
            self.check(command)
            return True
        except PolicyViolation:
            return False

    def risk_of(self, command: str | list[str]) -> RiskLevel:
        _, full = self._executable(command)
        lowered = full.lower()
        if any(k in lowered for k in ("push", "publish", "deploy", "delete", "drop ")):
            return RiskLevel.HIGH
        if any(k in lowered for k in ("commit", "install", "migrate")):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW


# --------------------------------------------------------------------------- #
# Git policy
# --------------------------------------------------------------------------- #
@dataclass
class GitGuard:
    def __post_init__(self) -> None:
        cfg = load_security_config().get("git", {}) or {}
        self.allow_commit: bool = bool(cfg.get("allow_commit", True))
        self.allow_push: bool = bool(cfg.get("allow_push", False))
        self.protected: list[str] = list(cfg.get("protected_branches", []) or [])
        self.branch_prefix: str = str(cfg.get("branch_prefix", "aiqa/"))

    def is_protected(self, branch: str) -> bool:
        return any(fnmatch.fnmatch(branch, p) for p in self.protected)

    def check_commit(self, branch: str) -> None:
        if not self.allow_commit:
            raise PolicyViolation("git.commit_disabled", "Commits are disabled by policy.", branch)
        if self.is_protected(branch):
            raise PolicyViolation(
                "git.protected_branch",
                f"Branch '{branch}' is protected. Create a '{self.branch_prefix}*' branch instead.",
                branch,
            )

    def check_push(self, branch: str) -> None:
        if not self.allow_push:
            raise PolicyViolation(
                "git.push_disabled",
                "Pushing requires explicit human approval and is disabled by default policy.",
                branch,
            )
        if self.is_protected(branch):
            raise PolicyViolation("git.protected_branch", f"Cannot push to protected branch '{branch}'.", branch)

    def suggested_branch(self, slug: str) -> str:
        safe = re.sub(r"[^a-z0-9\-]+", "-", slug.lower()).strip("-")[:48] or "tests"
        return f"{self.branch_prefix}{safe}"


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
class RBAC:
    def __init__(self) -> None:
        self.roles: dict[str, list[str]] = (load_security_config().get("rbac", {}) or {}).get("roles", {}) or {}

    def permissions(self, role: str) -> list[str]:
        return list(self.roles.get(role, []))

    def can(self, role: str, permission: str) -> bool:
        perms = self.permissions(role)
        if "*" in perms:
            return True
        if permission in perms:
            return True
        resource = permission.split(":", 1)[0]
        return f"{resource}:*" in perms

    def require(self, role: str, permission: str) -> None:
        if not self.can(role, permission):
            raise PolicyViolation(
                "rbac.denied",
                f"Role '{role}' lacks permission '{permission}'.",
                permission,
            )


_rbac: RBAC | None = None


def get_rbac() -> RBAC:
    global _rbac
    if _rbac is None:
        _rbac = RBAC()
    return _rbac
