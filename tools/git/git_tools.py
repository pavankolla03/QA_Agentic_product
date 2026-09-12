"""Git integration.

Read operations (status/diff/log) are free. Mutating operations are gated by
:class:`GitGuard`: protected branches are refused outright, and pushing is
disabled unless an operator explicitly enables it *and* a human approves.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from packages.aiqa_types.enums import ToolCategory
from packages.security.guard import GitGuard, WorkspaceGuard
from packages.security.redaction import redact
from tools.base import Tool, ToolResult
from tools.shell.shell_tools import sanitized_env


def _git(root: str, *args: str, timeout: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=sanitized_env(keep=("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP")),
        shell=False,
    )
    return proc.returncode, redact(proc.stdout or ""), redact(proc.stderr or "")


class GitStatusTool(Tool):
    name = "git.status"
    category = ToolCategory.GIT
    description = "Current branch, dirty state and changed files."

    def _run(self, **_: Any) -> ToolResult:
        root = self.ctx.project_root
        if not (Path(root) / ".git").exists():
            return ToolResult.success(
                {"is_repo": False, "branch": "", "dirty": False, "files": []},
                is_repo=False,
            )
        code, branch, _err = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            branch = ""
        _c, porcelain, _e = _git(root, "status", "--porcelain")
        files = [
            {"status": line[:2].strip(), "path": line[3:].strip()}
            for line in porcelain.splitlines()
            if line.strip()
        ]
        _c2, ahead, _e2 = _git(root, "rev-list", "--count", "--left-right", "@{u}...HEAD")
        return ToolResult.success(
            {
                "is_repo": True,
                "branch": branch.strip(),
                "dirty": bool(files),
                "files": files,
                "tracking": ahead.strip(),
                "protected": GitGuard().is_protected(branch.strip()),
            }
        )


class GitDiffTool(Tool):
    name = "git.diff"
    category = ToolCategory.GIT
    description = "Unified diff of working-tree changes (what the human reviews before approving)."
    schema = {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "context": {"type": "integer"},
        },
    }

    def _run(self, staged: bool = False, paths: list[str] | None = None, context: int = 3, **_: Any) -> ToolResult:
        root = self.ctx.project_root
        args = ["diff", f"-U{context}"]
        if staged:
            args.append("--cached")
        if paths:
            guard = WorkspaceGuard(root)
            args.append("--")
            for p in paths:
                guard.resolve_read(p)      # refuse paths outside the workspace
                args.append(p)
        code, out, err = _git(root, *args)
        if code != 0:
            return ToolResult.failure(err or "git diff failed")
        # Include untracked files so newly generated tests show up in review.
        _c, untracked, _e = _git(root, "ls-files", "--others", "--exclude-standard")
        return ToolResult.success(out, untracked=[u for u in untracked.splitlines() if u.strip()])


class GitBranchTool(Tool):
    name = "git.branch"
    category = ToolCategory.GIT
    description = "Create and switch to a feature branch for generated tests."
    mutating = True
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "create": {"type": "boolean"}}}

    def _run(self, name: str = "", create: bool = True, slug: str = "", **_: Any) -> ToolResult:
        guard = GitGuard()
        branch = name or guard.suggested_branch(slug or "generated-tests")
        if guard.is_protected(branch):
            return ToolResult.failure(f"refusing to work directly on protected branch '{branch}'", rule="git.protected_branch")
        if self.ctx.dry_run:
            return ToolResult.success(branch, dry_run=True)

        root = self.ctx.project_root
        code, out, err = _git(root, "checkout", "-b", branch) if create else _git(root, "checkout", branch)
        if code != 0 and "already exists" in (err or ""):
            code, out, err = _git(root, "checkout", branch)
        if code != 0:
            return ToolResult.failure(err or out or "git checkout failed")
        return ToolResult.success(branch, branch=branch)


class GitCommitTool(Tool):
    name = "git.commit"
    category = ToolCategory.GIT
    description = "Stage and commit generated test assets. Requires human approval."
    mutating = True
    schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["message"],
    }

    def _run(self, message: str, paths: list[str] | None = None, **_: Any) -> ToolResult:
        root = self.ctx.project_root
        guard = GitGuard()
        code, branch, _e = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch.strip() if code == 0 else ""
        guard.check_commit(branch)          # raises PolicyViolation on protected branches

        if self.ctx.dry_run:
            return ToolResult.success({"branch": branch, "message": message}, dry_run=True)

        workspace = WorkspaceGuard(root)
        if paths:
            for p in paths:
                workspace.resolve_write(p)  # only test assets may be staged
            code, out, err = _git(root, "add", "--", *paths)
        else:
            code, out, err = _git(root, "add", "-A")
        if code != 0:
            return ToolResult.failure(err or "git add failed")

        code, out, err = _git(root, "commit", "-m", message)
        if code != 0:
            if "nothing to commit" in (out + err).lower():
                return ToolResult.failure("nothing to commit")
            return ToolResult.failure(err or out or "git commit failed")

        _c, sha, _e = _git(root, "rev-parse", "--short", "HEAD")
        if self.ctx.tracker is not None:
            self.ctx.tracker.audit("git_commit", sha.strip(), "allowed", message[:300])
        return ToolResult.success({"sha": sha.strip(), "branch": branch, "message": message})


class GitPushTool(Tool):
    name = "git.push"
    category = ToolCategory.GIT
    description = "Push the working branch. Disabled unless policy allows it AND a human approves."
    mutating = True

    def _run(self, remote: str = "origin", branch: str = "", **_: Any) -> ToolResult:
        root = self.ctx.project_root
        guard = GitGuard()
        if not branch:
            code, branch, _e = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
            branch = branch.strip() if code == 0 else ""
        guard.check_push(branch)            # raises unless explicitly enabled

        if self.ctx.dry_run:
            return ToolResult.success({"remote": remote, "branch": branch}, dry_run=True)

        code, out, err = _git(root, "push", "--set-upstream", remote, branch, timeout=300)
        if code != 0:
            return ToolResult.failure(err or out or "git push failed")
        if self.ctx.tracker is not None:
            self.ctx.tracker.audit("git_push", f"{remote}/{branch}", "allowed", "")
        return ToolResult.success({"remote": remote, "branch": branch, "output": out})


class GitLogTool(Tool):
    name = "git.log"
    category = ToolCategory.GIT
    description = "Recent commit history (used to learn repository conventions)."
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}, "path": {"type": "string"}}}

    def _run(self, limit: int = 20, path: str = "", **_: Any) -> ToolResult:
        args = ["log", f"-{max(1, min(limit, 200))}", "--pretty=format:%h|%an|%ad|%s", "--date=short"]
        if path:
            WorkspaceGuard(self.ctx.project_root).resolve_read(path)
            args += ["--", path]
        code, out, err = _git(self.ctx.project_root, *args)
        if code != 0:
            return ToolResult.failure(err or "git log failed")
        commits = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
        return ToolResult.success(commits, count=len(commits))


class GitRevertTool(Tool):
    """Undo an applied self-heal that failed verification."""

    name = "git.checkout_file"
    category = ToolCategory.GIT
    description = "Restore a file to its committed state (used to revert a failed self-heal)."
    mutating = True
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def _run(self, path: str, **_: Any) -> ToolResult:
        WorkspaceGuard(self.ctx.project_root).resolve_write(path)
        if self.ctx.dry_run:
            return ToolResult.success(path, dry_run=True)
        code, out, err = _git(self.ctx.project_root, "checkout", "--", path)
        if code != 0:
            return ToolResult.failure(err or "git checkout -- failed")
        return ToolResult.success(path, reverted=True)


GIT_TOOLS = [GitStatusTool, GitDiffTool, GitBranchTool, GitCommitTool, GitPushTool, GitLogTool, GitRevertTool]
