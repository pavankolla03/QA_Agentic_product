"""Repository Map — persistent, incrementally-maintained repository knowledge.

The naive design re-reads and re-embeds the whole repository on every request.
That is the dominant cost in an LLM-backed QA tool and it buys nothing: the
repository has usually not changed.

This module keeps a durable map (`.aiqa/repository_map.json`, mirrored in the
database) keyed by **content hash per file**. On each run:

1. Compare the Git HEAD and working-tree dirtiness. Unchanged and clean → the
   cached map is returned with **zero** file reads and **zero** embeddings.
2. Otherwise hash every candidate file and diff against the map. Only added and
   modified files are re-parsed; only their chunks are re-embedded; deleted
   files are dropped.

The map is written into the repository on purpose: it is reviewable, diffable
and can be committed, so a CI runner starts warm instead of cold.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from packages.aiqa_types.models import RepoProfile, RepoSymbol
from services.knowledge_service.code_parser import ParsedFile, parse_file
from services.knowledge_service.indexer import (
    INDEXABLE_SUFFIXES,
    _chunk_kind,
    detect_framework,
    detect_layout,
    detect_naming,
    summarize_conventions,
)
from tools.filesystem.fs_tools import iter_source_files

log = logging.getLogger("aiqa.repomap")

MAP_VERSION = 2
MAP_RELATIVE_PATH = Path(".aiqa") / "repository_map.json"

#: Files whose content is mostly noise for test generation.
SKIP_NAME_PATTERNS = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".min.js", ".d.ts", "CHANGELOG")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Git helpers — cheap change detection before any file is opened
# --------------------------------------------------------------------------- #
def git_head(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=15, shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


#: Working-tree changes that cannot affect the index. `.aiqa/` matters most:
#: the map writes itself into the repository, and counting that as a change
#: would make every run look dirty and permanently defeat the cache.
_IRRELEVANT_DIRTY_PREFIXES = (".aiqa/", "node_modules/", "test-results/", "playwright-report/", "dist/", "coverage/")


def relevant_dirty(dirty: set[str]) -> set[str]:
    """Narrow working-tree changes to files that would actually change the index."""
    out: set[str] = set()
    for path in dirty:
        normalised = path.replace("\\", "/")
        if any(normalised.startswith(prefix) for prefix in _IRRELEVANT_DIRTY_PREFIXES):
            continue
        if Path(normalised).suffix.lower() not in INDEXABLE_SUFFIXES:
            continue
        if any(pattern in Path(normalised).name for pattern in SKIP_NAME_PATTERNS):
            continue
        out.add(normalised)
    return out


def git_dirty_files(root: Path) -> set[str]:
    """Paths modified in the working tree. Empty set also means 'not a repo'."""
    if not (root / ".git").exists():
        return set()
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"],
            cwd=str(root), capture_output=True, text=True, timeout=20, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()

    dirty: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        # Renames appear as "old -> new"; both sides matter.
        if " -> " in path:
            before, after = path.split(" -> ", 1)
            dirty.update({before.strip(), after.strip()})
        else:
            dirty.add(path)
    return dirty


# --------------------------------------------------------------------------- #
@dataclass
class FileEntry:
    """One indexed file. `hash` is what makes incremental indexing possible."""

    path: str
    hash: str
    size: int
    kind: str = "code"
    language: str = ""
    lines: int = 0
    symbols: list[dict[str, Any]] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)
    indexed_at: float = 0.0


@dataclass
class IndexDelta:
    """What actually changed — reported to the user and used for cost accounting."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    reused_from_cache: bool = False
    embeddings_computed: int = 0
    embeddings_skipped: int = 0

    @property
    def changed(self) -> list[str]:
        return [*self.added, *self.modified]

    @property
    def any_change(self) -> bool:
        return bool(self.added or self.modified or self.removed)

    def summary(self) -> str:
        if self.reused_from_cache:
            return f"repository unchanged - reused cached map ({self.unchanged} files, 0 re-indexed)"
        return (
            f"{len(self.added)} added, {len(self.modified)} modified, {len(self.removed)} removed, "
            f"{self.unchanged} unchanged; {self.embeddings_computed} chunk(s) embedded, "
            f"{self.embeddings_skipped} reused"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": self.added, "modified": self.modified, "removed": self.removed,
            "unchanged": self.unchanged, "reused_from_cache": self.reused_from_cache,
            "embeddings_computed": self.embeddings_computed,
            "embeddings_skipped": self.embeddings_skipped,
        }


@dataclass
class RepositoryMap:
    """The persisted structural knowledge of one repository."""

    version: int = MAP_VERSION
    project_id: str = ""
    root: str = ""
    git_commit: str = ""
    dirty_when_indexed: bool = False
    indexed_at: float = 0.0

    framework: str = "playwright"
    language: str = "typescript"
    bdd: bool = False
    bdd_runnable: bool = False
    bdd_runner: str = ""
    pom: bool = False
    package_manager: str = "npm"
    frameworks: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    layout: dict[str, str] = field(default_factory=dict)
    naming: dict[str, str] = field(default_factory=dict)
    test_commands: list[str] = field(default_factory=list)
    conventions_summary: str = ""

    #: Symbol indexes by kind, so retrieval never scans everything.
    pages: list[dict[str, Any]] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    fixtures: list[dict[str, Any]] = field(default_factory=list)
    utilities: list[dict[str, Any]] = field(default_factory=list)
    apis: list[dict[str, Any]] = field(default_factory=list)
    database: list[dict[str, Any]] = field(default_factory=list)

    files: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepositoryMap | None:
        if not isinstance(data, dict) or int(data.get("version", 0)) != MAP_VERSION:
            return None          # a schema change invalidates the cache
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load(cls, root: Path) -> RepositoryMap | None:
        path = root / MAP_RELATIVE_PATH
        if not path.exists():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, root: Path) -> None:
        path = root / MAP_RELATIVE_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.to_json(), encoding="utf-8", newline="\n")
        except OSError as exc:  # pragma: no cover - read-only checkout
            log.warning("could not persist the repository map: %s", exc)

    # ------------------------------------------------------------------ #
    def all_symbols(self) -> list[RepoSymbol]:
        out: list[RepoSymbol] = []
        for entry in self.files.values():
            for raw in entry.get("symbols", []) or []:
                try:
                    out.append(RepoSymbol(**raw))
                except Exception:  # noqa: BLE001 - tolerate an older shape
                    continue
        return out

    def to_profile(self) -> RepoProfile:
        profile = RepoProfile(
            project_id=self.project_id,
            root=self.root,
            language=self.language,
            test_runner=self.framework,
            bdd=self.bdd,
            bdd_runnable=self.bdd_runnable,
            bdd_runner=self.bdd_runner,
            package_manager=self.package_manager,
            detected_layout=dict(self.layout),
            frameworks=list(self.frameworks),
            config_files=list(self.config_files),
            symbols=self.all_symbols(),
            existing_features=list(self.features),
            naming_conventions=dict(self.naming),
            file_count=len(self.files),
        )
        profile.conventions_summary = self.conventions_summary
        return profile

    def symbols_of(self, kind: str) -> list[RepoSymbol]:
        return [s for s in self.all_symbols() if s.kind == kind]

    def stats(self) -> dict[str, Any]:
        return {
            "files": len(self.files),
            "pages": len(self.pages),
            "steps": len(self.steps),
            "fixtures": len(self.fixtures),
            "utilities": len(self.utilities),
            "features": len(self.features),
            "git_commit": self.git_commit[:12],
            "indexed_at": self.indexed_at,
        }


# --------------------------------------------------------------------------- #
class RepositoryMapper:
    """Builds and incrementally maintains a :class:`RepositoryMap`."""

    def __init__(self, project_id: str, root: str | Path, max_files: int = 5000) -> None:
        self.project_id = project_id
        self.root = Path(root).resolve()
        self.max_files = max_files

    # ------------------------------------------------------------------ #
    def _candidate_files(self) -> list[Path]:
        return [
            path
            for path in iter_source_files(self.root, INDEXABLE_SUFFIXES, limit=self.max_files)
            if not any(pattern in path.name for pattern in SKIP_NAME_PATTERNS)
        ]

    @staticmethod
    def _read(path: Path) -> str | None:
        try:
            if path.stat().st_size > 2_097_152:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # ------------------------------------------------------------------ #
    def build(self, previous: RepositoryMap | None = None, force: bool = False) -> tuple[RepositoryMap, IndexDelta]:
        """Return an up-to-date map plus what changed since ``previous``.

        The fast path — an unchanged, clean Git checkout — returns immediately
        without opening a single file.
        """
        previous = previous or RepositoryMap.load(self.root)
        head = git_head(self.root)
        dirty = relevant_dirty(git_dirty_files(self.root))
        delta = IndexDelta()

        # ---- fast path: nothing can have changed ---------------------- #
        if (
            not force
            and previous is not None
            and previous.git_commit
            and head
            and previous.git_commit == head
            and not dirty
            and not previous.dirty_when_indexed
            and previous.files
        ):
            delta.reused_from_cache = True
            delta.unchanged = len(previous.files)
            log.info("repository map cache hit at %s (%d files)", head[:12], len(previous.files))
            return previous, delta

        # ---- incremental path ----------------------------------------- #
        old_files: dict[str, dict[str, Any]] = dict(previous.files) if previous else {}
        new_files: dict[str, dict[str, Any]] = {}
        parsed_changed: list[ParsedFile] = []
        all_parsed: list[ParsedFile] = []
        now = time.time()

        for path in self._candidate_files():
            try:
                rel = path.relative_to(self.root).as_posix()
            except ValueError:
                continue
            text = self._read(path)
            if text is None:
                continue

            digest = _hash(text)
            existing = old_files.get(rel)

            if existing and existing.get("hash") == digest and not force:
                # Unchanged: reuse the parse result verbatim, no work at all.
                new_files[rel] = existing
                delta.unchanged += 1
                if path.suffix.lower() not in (".json", ".yaml", ".yml", ".md"):
                    all_parsed.append(_parsed_from_entry(rel, existing))
                continue

            if path.suffix.lower() in (".json", ".yaml", ".yml", ".md"):
                entry = FileEntry(
                    path=rel, hash=digest, size=len(text), kind=_chunk_kind(rel),
                    language=path.suffix.lstrip("."), lines=text.count("\n") + 1, indexed_at=now,
                )
                new_files[rel] = asdict(entry)
            else:
                parsed = parse_file(rel, text)
                all_parsed.append(parsed)
                parsed_changed.append(parsed)
                entry = FileEntry(
                    path=rel, hash=digest, size=len(text), kind=_chunk_kind(rel),
                    language=parsed.language, lines=parsed.lines,
                    symbols=[s.model_dump(mode="json") for s in parsed.symbols],
                    indexed_at=now,
                )
                new_files[rel] = asdict(entry)

            (delta.modified if existing else delta.added).append(rel)

        delta.removed = [path for path in old_files if path not in new_files]

        # ---- structural facts ----------------------------------------- #
        framework = detect_framework(self.root)
        layout = detect_layout(self.root, all_parsed)
        naming = detect_naming(all_parsed)

        current = RepositoryMap(
            project_id=self.project_id,
            root=str(self.root),
            git_commit=head,
            dirty_when_indexed=bool(dirty),
            indexed_at=now,
            framework=framework["test_runner"],
            language=framework["language"],
            bdd=bool(framework["bdd"]),
            bdd_runnable=bool(framework.get("bdd_runnable")),
            bdd_runner=str(framework.get("bdd_runner", "")),
            pom=any("page" in key for key in layout),
            package_manager=framework["package_manager"],
            frameworks=list(framework["frameworks"]),
            config_files=list(framework["config_files"]),
            layout=layout,
            naming=naming,
            test_commands=_detect_test_commands(self.root),
            files=new_files,
        )
        _populate_symbol_indexes(current, all_parsed)
        current.conventions_summary = summarize_conventions(current.to_profile(), all_parsed)
        return current, delta


def _parsed_from_entry(rel: str, entry: dict[str, Any]) -> ParsedFile:
    """Reconstruct just enough of a ParsedFile from the cache to redo layout detection."""
    symbols: list[RepoSymbol] = []
    for raw in entry.get("symbols", []) or []:
        try:
            symbols.append(RepoSymbol(**raw))
        except Exception:  # noqa: BLE001
            continue
    return ParsedFile(
        path=rel,
        language=entry.get("language", ""),
        symbols=symbols,
        imports=[],
        step_texts=[s.name for s in symbols if s.kind == "step"],
        feature_names=list(entry.get("feature_names", []) or []),
        scenario_names=[s.name for s in symbols if s.kind == "step" and s.signature == "scenario"],
        locator_count=0,
        lines=int(entry.get("lines", 0)),
    )


def _populate_symbol_indexes(repo_map: RepositoryMap, parsed: list[ParsedFile]) -> None:
    """Group symbols by kind so retrieval can target one slice."""
    def brief(symbol: RepoSymbol) -> dict[str, Any]:
        return {
            "name": symbol.name,
            "file": symbol.file_path,
            "line": symbol.line,
            "signature": symbol.signature,
            "members": symbol.members[:20],
        }

    symbols = [s for p in parsed for s in p.symbols]
    repo_map.pages = [brief(s) for s in symbols if s.kind == "page_object"]
    repo_map.fixtures = [brief(s) for s in symbols if s.kind == "fixture"]
    repo_map.utilities = [brief(s) for s in symbols if s.kind == "util"]
    repo_map.steps = [brief(s) for s in symbols if s.kind == "step"]
    repo_map.features = sorted({name for p in parsed for name in p.feature_names})
    repo_map.apis = [
        brief(s) for s in symbols if "api" in s.file_path.lower() or "client" in s.name.lower()
    ]
    repo_map.database = [
        brief(s) for s in symbols if any(k in s.file_path.lower() for k in ("db", "database", "sql"))
    ]


def _detect_test_commands(root: Path) -> list[str]:
    """Read the real commands out of package.json rather than guessing."""
    package = root / "package.json"
    commands: list[str] = []
    if package.exists():
        try:
            scripts = (json.loads(package.read_text(encoding="utf-8")) or {}).get("scripts", {}) or {}
        except (OSError, json.JSONDecodeError):
            scripts = {}
        for name, body in scripts.items():
            if any(hint in f"{name} {body}".lower() for hint in ("test", "e2e", "playwright", "cucumber")):
                commands.append(f"npm run {name}")
    if not commands and (root / "playwright.config.ts").exists():
        commands.append("npx playwright test")
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        commands.append("pytest")
    return commands[:10]
