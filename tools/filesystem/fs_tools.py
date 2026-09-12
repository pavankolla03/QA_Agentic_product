"""Filesystem tools — every read and write is confined to the project root.

These are the only way an agent touches the user's workspace. ``.env`` files,
keys and anything outside the registered repository are unreachable by
construction, not by convention.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Iterable

from packages.aiqa_types.enums import ToolCategory
from packages.security.guard import PolicyViolation, WorkspaceGuard
from tools.base import Tool, ToolResult

DEFAULT_IGNORES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "out",
    ".next", ".nuxt", "coverage", "test-results", "playwright-report", ".pytest_cache",
    ".idea", ".vscode", ".mypy_cache", ".ruff_cache", "target", ".gradle",
}

TEXT_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".java", ".kt", ".rb", ".go",
    ".feature", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt",
    ".html", ".css", ".scss", ".xml", ".csv", ".sql", ".sh", ".ps1", ".properties",
}


def _guard(root: str) -> WorkspaceGuard:
    return WorkspaceGuard(root)


def make_diff(path: str, old: str, new: str) -> str:
    """Unified diff, the same format the VS Code extension renders."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(diff)


class ReadFileTool(Tool):
    name = "fs.read_file"
    category = ToolCategory.FILESYSTEM
    description = "Read a UTF-8 text file from inside the project workspace."
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative path"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        "required": ["path"],
    }

    def _run(self, path: str, start_line: int = 0, end_line: int = 0, **_: Any) -> ToolResult:
        guard = _guard(self.ctx.project_root)
        resolved = guard.resolve_read(path)
        if not resolved.exists():
            return ToolResult.failure(f"file not found: {path}")
        if not resolved.is_file():
            return ToolResult.failure(f"not a file: {path}")
        size = resolved.stat().st_size
        if size > guard.max_file_bytes:
            return ToolResult.failure(f"file too large ({size} bytes > {guard.max_file_bytes})")

        text = resolved.read_text(encoding="utf-8", errors="replace")
        total = text.count("\n") + 1
        if start_line or end_line:
            lines = text.splitlines()
            lo = max(0, (start_line or 1) - 1)
            hi = min(len(lines), end_line or len(lines))
            text = "\n".join(lines[lo:hi])
        return ToolResult.success(text, path=path, size=size, lines=total)


class WriteFileTool(Tool):
    name = "fs.write_file"
    category = ToolCategory.FILESYSTEM
    description = "Create or overwrite a test asset. Restricted to the allowed test directories."
    mutating = True
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "create_dirs": {"type": "boolean"},
        },
        "required": ["path", "content"],
    }

    def _run(self, path: str, content: str, create_dirs: bool = True, **_: Any) -> ToolResult:
        guard = _guard(self.ctx.project_root)
        resolved = guard.resolve_write(path, size=len(content.encode("utf-8")))

        existed = resolved.exists()
        old = resolved.read_text(encoding="utf-8", errors="replace") if existed else ""
        diff = make_diff(path, old, content)

        if self.ctx.dry_run:
            return ToolResult.success(
                diff, path=path, dry_run=True, existed=existed, bytes=len(content.encode("utf-8"))
            )

        if create_dirs:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8", newline="\n")

        if self.ctx.tracker is not None:
            self.ctx.tracker.audit(
                "file_write", path, "allowed", f"{'modified' if existed else 'created'} ({len(content)} chars)"
            )
        return ToolResult.success(
            diff, path=path, existed=existed, bytes=len(content.encode("utf-8")), written=True
        )


class DeleteFileTool(Tool):
    name = "fs.delete_file"
    category = ToolCategory.FILESYSTEM
    description = "Delete a generated test asset (allowed test directories only)."
    mutating = True
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def _run(self, path: str, **_: Any) -> ToolResult:
        guard = _guard(self.ctx.project_root)
        resolved = guard.resolve_write(path)
        if not resolved.exists():
            return ToolResult.failure(f"file not found: {path}")
        if self.ctx.dry_run:
            return ToolResult.success(None, path=path, dry_run=True)
        resolved.unlink()
        if self.ctx.tracker is not None:
            self.ctx.tracker.audit("file_write", path, "allowed", "deleted")
        return ToolResult.success(None, path=path, deleted=True)


class ListDirTool(Tool):
    name = "fs.list_dir"
    category = ToolCategory.FILESYSTEM
    description = "List files and folders under a workspace directory."
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
            "max_entries": {"type": "integer"},
        },
    }

    def _run(self, path: str = ".", recursive: bool = False, max_entries: int = 500, **_: Any) -> ToolResult:
        guard = _guard(self.ctx.project_root)
        resolved = guard.resolve_read(path)
        if not resolved.exists():
            return ToolResult.failure(f"directory not found: {path}")

        entries: list[dict[str, Any]] = []
        root = Path(self.ctx.project_root).resolve()

        if recursive:
            for dirpath, dirnames, filenames in os.walk(resolved):
                dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORES and not d.startswith(".")]
                for fname in filenames:
                    full = Path(dirpath) / fname
                    try:
                        rel = full.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    entries.append({"path": rel, "type": "file", "size": full.stat().st_size})
                    if len(entries) >= max_entries:
                        return ToolResult.success(entries, truncated=True, count=len(entries))
        else:
            for child in sorted(resolved.iterdir()):
                if child.name in DEFAULT_IGNORES:
                    continue
                rel = child.relative_to(root).as_posix()
                entries.append(
                    {
                        "path": rel,
                        "type": "dir" if child.is_dir() else "file",
                        "size": child.stat().st_size if child.is_file() else 0,
                    }
                )
                if len(entries) >= max_entries:
                    break
        return ToolResult.success(entries, count=len(entries))


class SearchTool(Tool):
    name = "fs.search"
    category = ToolCategory.FILESYSTEM
    description = "Regex search across workspace source files (ripgrep-like)."
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "glob": {"type": "string"},
            "max_results": {"type": "integer"},
            "ignore_case": {"type": "boolean"},
        },
        "required": ["pattern"],
    }

    def _run(
        self,
        pattern: str,
        glob: str = "**/*",
        max_results: int = 100,
        ignore_case: bool = True,
        **_: Any,
    ) -> ToolResult:
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return ToolResult.failure(f"invalid regex: {exc}")

        root = Path(self.ctx.project_root).resolve()
        hits: list[dict[str, Any]] = []

        for file in iter_source_files(root):
            rel = file.relative_to(root).as_posix()
            if glob not in ("**/*", "") and not fnmatch.fnmatch(rel, glob):
                continue
            try:
                text = file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append({"path": rel, "line": lineno, "text": line.strip()[:300]})
                    if len(hits) >= max_results:
                        return ToolResult.success(hits, truncated=True, count=len(hits))
        return ToolResult.success(hits, count=len(hits))


class ApplyChangesTool(Tool):
    """Applies an approved :class:`CodeBundle` atomically.

    If any single write is refused by policy, everything already written is
    rolled back — a half-applied change set is worse than none.
    """

    name = "fs.apply_changes"
    category = ToolCategory.FILESYSTEM
    description = "Apply an approved set of file changes atomically, with rollback on failure."
    mutating = True

    def _run(self, changes: list[dict[str, Any]] | None = None, **_: Any) -> ToolResult:
        changes = changes or []
        guard = _guard(self.ctx.project_root)
        backups: list[tuple[Path, str | None]] = []
        applied: list[str] = []

        try:
            for change in changes:
                path = change["path"]
                content = change.get("content", "")
                change_type = change.get("change_type", "create")
                resolved = guard.resolve_write(path, size=len(content.encode("utf-8")))

                backups.append((resolved, resolved.read_text(encoding="utf-8") if resolved.exists() else None))

                if self.ctx.dry_run:
                    applied.append(path)
                    continue

                if change_type == "delete":
                    if resolved.exists():
                        resolved.unlink()
                else:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    resolved.write_text(content, encoding="utf-8", newline="\n")
                applied.append(path)

            if self.ctx.tracker is not None and not self.ctx.dry_run:
                self.ctx.tracker.audit("file_write", f"{len(applied)} files", "allowed", ", ".join(applied[:20]))
            return ToolResult.success(applied, count=len(applied), dry_run=self.ctx.dry_run)

        except (PolicyViolation, OSError) as exc:
            # Roll back everything we touched.
            for resolved, original in reversed(backups):
                try:
                    if original is None:
                        if resolved.exists():
                            resolved.unlink()
                    else:
                        resolved.write_text(original, encoding="utf-8", newline="\n")
                except OSError:
                    continue
            if isinstance(exc, PolicyViolation):
                raise
            return ToolResult.failure(f"apply failed and was rolled back: {exc}")


class FileExistsTool(Tool):
    name = "fs.exists"
    category = ToolCategory.FILESYSTEM
    description = "Check whether a workspace path exists."
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def _run(self, path: str, **_: Any) -> ToolResult:
        try:
            resolved = _guard(self.ctx.project_root).resolve_read(path)
        except PolicyViolation:
            return ToolResult.success(False, path=path, blocked=True)
        return ToolResult.success(resolved.exists(), path=path, is_dir=resolved.is_dir() if resolved.exists() else False)


# --------------------------------------------------------------------------- #
def iter_source_files(root: Path, suffixes: Iterable[str] | None = None, limit: int = 20000) -> Iterable[Path]:
    """Walk the repository yielding text source files, skipping noise directories."""
    allowed = set(suffixes) if suffixes else TEXT_SUFFIXES
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORES and not d.startswith(".")]
        for fname in filenames:
            if Path(fname).suffix.lower() in allowed:
                yield Path(dirpath) / fname
                count += 1
                if count >= limit:
                    return


FILESYSTEM_TOOLS = [
    ReadFileTool, WriteFileTool, DeleteFileTool, ListDirTool,
    SearchTool, ApplyChangesTool, FileExistsTool,
]
