"""Repository indexing + retrieval — the Knowledge System.

Two products come out of an index pass:

1. A :class:`RepoProfile` — the structural facts (layout, framework, reusable
   symbols, naming conventions) that the Code Generation Agent must honour so
   generated tests look like the team wrote them.
2. A set of embedded chunks for semantic retrieval, so prompts carry the *few*
   relevant existing files instead of the whole repository.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from packages.aiqa_types.models import RepoProfile, RepoSymbol, new_id
from packages.security.guard import WorkspaceGuard
from services.knowledge_service.code_parser import ParsedFile, chunk_text, parse_file
from services.observability.db import session_scope
from services.observability.models import KnowledgeChunkRow
from tools.filesystem.fs_tools import iter_source_files

log = logging.getLogger("aiqa.knowledge")

INDEXABLE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".py", ".feature", ".json", ".yaml", ".yml", ".md"}
# Files whose content is mostly noise for test generation.
SKIP_NAME_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".min.js", ".d.ts", "CHANGELOG",
)

LAYOUT_HINTS: dict[str, tuple[str, ...]] = {
    "features_dir": ("features", "feature"),
    "steps_dir": ("steps", "step-definitions", "step_definitions", "stepdefinitions"),
    "pages_dir": ("pages", "page-objects", "pageobjects", "po", "screens"),
    "fixtures_dir": ("fixtures", "fixture"),
    "utils_dir": ("utils", "helpers", "support", "common"),
    "data_dir": ("data", "testdata", "test-data"),
    "api_dir": ("api", "apis", "services"),
}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32]


def _should_skip(name: str) -> bool:
    return any(pattern in name for pattern in SKIP_NAME_PATTERNS)


# =========================================================================== #
# Framework / layout detection
# =========================================================================== #
def detect_framework(root: Path) -> dict[str, Any]:
    """Work out what this repository actually uses before generating anything."""
    info: dict[str, Any] = {
        "language": "typescript",
        "test_runner": "unknown",
        "bdd": False,
        # Having a BDD library installed is not the same as being able to run a
        # feature file, and the difference is not cosmetic: a repository with
        # `@cucumber/cucumber` in devDependencies and no runner wiring accepts
        # every .feature the platform writes and executes none of them. The
        # files look finished, the compile gate passes, and nothing ever runs.
        "bdd_runnable": False,
        "bdd_runner": "",
        "package_manager": "npm",
        "frameworks": [],
        "config_files": [],
    }

    pkg_path = root / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            pkg = {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        info["config_files"].append("package.json")
        detected: list[str] = []
        if any(k.startswith("@playwright") or k == "playwright" for k in deps):
            info["test_runner"] = "playwright"
            detected.append("playwright")
        elif "cypress" in deps:
            info["test_runner"] = "cypress"
            detected.append("cypress")
        elif "@wdio/cli" in deps or any(k.startswith("@wdio") for k in deps):
            info["test_runner"] = "webdriverio"
            detected.append("webdriverio")
        elif "jest" in deps:
            info["test_runner"] = "jest"
            detected.append("jest")
        if any("cucumber" in k for k in deps) or "playwright-bdd" in deps:
            info["bdd"] = True
            detected.append("cucumber-bdd")
        if "typescript" in deps:
            info["language"] = "typescript"
        elif info["test_runner"] != "unknown":
            info["language"] = "javascript"
        if "@appium" in " ".join(deps) or "appium" in deps:
            detected.append("appium")
        info["frameworks"] = detected
        if (root / "pnpm-lock.yaml").exists():
            info["package_manager"] = "pnpm"
        elif (root / "yarn.lock").exists():
            info["package_manager"] = "yarn"

    for candidate in ("playwright.config.ts", "playwright.config.js", "cypress.config.ts",
                      "wdio.conf.ts", "cucumber.js", "cucumber.cjs", "cucumber.mjs", "cucumber.json",
                      "cucumber.yaml", "cucumber.yml", "tsconfig.json",
                      "pytest.ini", "pyproject.toml", "conftest.py", "pom.xml", "build.gradle"):
        if (root / candidate).exists():
            info["config_files"].append(candidate)

    if info["bdd"]:
        runner = _bdd_runner(root)
        info["bdd_runnable"] = bool(runner)
        info["bdd_runner"] = runner

    if info["test_runner"] == "unknown":
        if (root / "conftest.py").exists() or (root / "pytest.ini").exists():
            info["language"], info["test_runner"] = "python", "pytest"
            if (root / "features").exists():
                info["bdd"] = True
        elif (root / "pom.xml").exists():
            info["language"], info["test_runner"] = "java", "testng"

    return info


def _bdd_runner(root: Path) -> str:
    """Which runner, if any, would actually execute this repository's features.

    Returns the runner's name, or "" when the feature files are inert. Only
    configuration counts -- a dependency in package.json proves nothing about
    whether anything reads the .feature directory.
    """
    for name in ("cucumber.js", "cucumber.cjs", "cucumber.mjs", "cucumber.json",
                 "cucumber.yaml", "cucumber.yml", ".cucumberrc.json", ".cucumber-rc.json"):
        if (root / name).exists():
            return "cucumber-js"

    package = root / "package.json"
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8", errors="replace")) or {}
        except json.JSONDecodeError:
            data = {}
        if data.get("cucumber"):
            return "cucumber-js"

    # playwright-bdd is wired inside the Playwright config, not beside it.
    for name in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs"):
        config = root / name
        if config.exists() and "defineBddConfig" in config.read_text(encoding="utf-8", errors="replace"):
            return "playwright-bdd"

    # Python BDD keeps its steps next to the features by convention.
    if (root / "features" / "steps").is_dir():
        return "behave"
    return ""


def detect_layout(root: Path, parsed: list[ParsedFile]) -> dict[str, str]:
    """Infer where each kind of asset lives, from what is actually on disk."""
    layout: dict[str, str] = {}
    directories = {str(Path(p.path).parent).replace("\\", "/") for p in parsed}

    for key, hints in LAYOUT_HINTS.items():
        best, best_score = "", -1
        for directory in directories:
            segments = [s.lower() for s in directory.split("/")]
            if not any(seg in hints for seg in segments):
                continue
            # Prefer the directory with the most files and the shallowest path.
            count = sum(1 for p in parsed if str(Path(p.path).parent).replace("\\", "/") == directory)
            score = count * 10 - len(segments)
            if score > best_score:
                best, best_score = directory, score
        if best:
            layout[key] = best

    # Fall back to whatever kind of symbol dominates a directory.
    if "pages_dir" not in layout:
        page_dirs = Counter(
            str(Path(s.file_path).parent).replace("\\", "/")
            for p in parsed
            for s in p.symbols
            if s.kind == "page_object"
        )
        if page_dirs:
            layout["pages_dir"] = page_dirs.most_common(1)[0][0]
    if "steps_dir" not in layout:
        step_dirs = Counter(
            str(Path(s.file_path).parent).replace("\\", "/")
            for p in parsed
            for s in p.symbols
            if s.kind == "step"
        )
        if step_dirs:
            layout["steps_dir"] = step_dirs.most_common(1)[0][0]
    return layout


#: Words common enough in feature filenames that seeing one glued to another is
#: good evidence the team writes them run-together.
_FILENAME_WORDS = (
    "page", "form", "list", "view", "edit", "create", "delete", "search",
    "login", "logout", "user", "admin", "report", "detail", "registration",
    "management", "profile", "settings", "checkout", "order", "invoice",
)


def _looks_multiword(stem: str) -> bool:
    """Is this stem two words run together, rather than a single word?"""
    lowered = stem.lower()
    if len(lowered) < 9:
        return False
    hits = sum(1 for word in _FILENAME_WORDS if word in lowered)
    return hits >= 2


def detect_naming(parsed: list[ParsedFile]) -> dict[str, str]:
    """Learn the team's naming habits so generated files blend in."""
    conventions: dict[str, str] = {}

    page_files = [
        Path(s.file_path).name for p in parsed for s in p.symbols if s.kind == "page_object"
    ]
    if page_files:
        sample = page_files[0]
        stem = Path(sample).stem
        if re.fullmatch(r"[A-Z][A-Za-z0-9]*", stem):
            conventions["page_object_file"] = "PascalCase" + Path(sample).suffix
        elif "-" in stem:
            conventions["page_object_file"] = "kebab-case" + Path(sample).suffix
        elif "_" in stem:
            conventions["page_object_file"] = "snake_case" + Path(sample).suffix
        if any(s.name.endswith("Page") for p in parsed for s in p.symbols if s.kind == "page_object"):
            conventions["page_object_class_suffix"] = "Page"

    step_files = [Path(p.path).name for p in parsed if any(s.kind == "step" for s in p.symbols) and p.path.endswith((".ts", ".js"))]
    if step_files:
        conventions["step_file"] = "*.steps.ts" if any(".steps." in f for f in step_files) else Path(step_files[0]).name

    feature_files = [Path(p.path).name for p in parsed if p.path.endswith(".feature")]
    if feature_files:
        stems = [Path(name).stem for name in feature_files]
        if any("-" in stem for stem in stems):
            conventions["feature_file"] = "kebab-case.feature"
        elif any("_" in stem for stem in stems):
            conventions["feature_file"] = "snake_case.feature"
        elif any(_looks_multiword(stem) for stem in stems):
            # Only a run-together multi-word name is evidence *for* lowercase.
            # `login.feature` is one word and is equally consistent with every
            # convention, so concluding "lowercase" from it would silently
            # strip the separators out of every generated filename.
            conventions["feature_file"] = "lowercase.feature"
        else:
            conventions["feature_file"] = "kebab-case.feature"

    # Test-id prefix, e.g. TC-AUTH-001
    ids = [
        m.group(1)
        for p in parsed
        for name in p.scenario_names
        for m in [re.match(r"^([A-Z]+-)[A-Z0-9]+-\d+", name)]
        if m
    ]
    if ids:
        conventions["test_id_prefix"] = Counter(ids).most_common(1)[0][0]

    # Dominant base class for page objects
    bases = Counter(
        s.signature.split("extends")[-1].strip()
        for p in parsed
        for s in p.symbols
        if s.kind == "page_object" and "extends" in s.signature
    )
    if bases:
        conventions["page_object_base_class"] = bases.most_common(1)[0][0]

    return conventions


# =========================================================================== #
# Indexer
# =========================================================================== #
class RepositoryIndexer:
    """Builds a :class:`RepoProfile` and (optionally) an embedded chunk index."""

    def __init__(self, project_id: str, project_root: str, max_files: int = 4000) -> None:
        self.project_id = project_id
        self.root = Path(project_root).resolve()
        self.max_files = max_files
        self.guard = WorkspaceGuard(self.root)

    # ------------------------------------------------------------------ #
    def scan(self) -> tuple[RepoProfile, list[ParsedFile]]:
        """Structural pass — no LLM, no embeddings, fast."""
        framework = detect_framework(self.root)
        parsed: list[ParsedFile] = []
        file_count = 0

        for path in iter_source_files(self.root, INDEXABLE_SUFFIXES, limit=self.max_files):
            if _should_skip(path.name):
                continue
            try:
                rel = path.relative_to(self.root).as_posix()
            except ValueError:
                continue
            try:
                if path.stat().st_size > self.guard.max_file_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_count += 1
            if path.suffix.lower() in (".json", ".yaml", ".yml", ".md"):
                continue      # counted, but not symbol-parsed
            parsed.append(parse_file(rel, text))

        symbols: list[RepoSymbol] = [s for p in parsed for s in p.symbols]
        layout = detect_layout(self.root, parsed)
        naming = detect_naming(parsed)

        profile = RepoProfile(
            project_id=self.project_id,
            root=str(self.root),
            language=framework["language"],
            test_runner=framework["test_runner"],
            bdd=framework["bdd"],
            package_manager=framework["package_manager"],
            detected_layout=layout,
            frameworks=framework["frameworks"],
            config_files=framework["config_files"],
            symbols=symbols,
            existing_features=sorted({name for p in parsed for name in p.feature_names}),
            naming_conventions=naming,
            file_count=file_count,
        )
        profile.conventions_summary = summarize_conventions(profile, parsed)
        return profile, parsed

    # ------------------------------------------------------------------ #
    async def index(self, router: Any = None, scope: str = "repository", replace: bool = True) -> RepoProfile:
        """Full pass: scan, chunk, embed and persist for retrieval."""
        profile, parsed = self.scan()

        chunks: list[dict[str, Any]] = []
        for parsed_file in parsed:
            path = self.root / parsed_file.path
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            primary = next((s.name for s in parsed_file.symbols), "")
            for start, end, content in chunk_text(text):
                if not content.strip():
                    continue
                chunks.append(
                    {
                        "file_path": parsed_file.path,
                        "symbol": primary,
                        "start_line": start,
                        "end_line": end,
                        "content": content,
                        "kind": _chunk_kind(parsed_file.path),
                        "summary": f"{parsed_file.path}:{start}-{end}",
                    }
                )

        vectors: list[list[float]] = []
        model_name = "none"
        if chunks and router is not None:
            texts = [f"{c['file_path']}\n{c['content']}" for c in chunks]
            try:
                vectors = await router.embed(texts)
                resolved = await router.resolve("embedding")
                model_name = resolved.model if resolved else "unknown"
            except Exception as exc:  # noqa: BLE001 - indexing must not fail a run
                log.warning("embedding failed, storing chunks without vectors: %s", exc)
                vectors = []

        with session_scope() as session:
            if replace:
                session.execute(
                    delete(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.project_id == self.project_id,
                        KnowledgeChunkRow.scope == scope,
                    )
                )
            for index, chunk in enumerate(chunks):
                session.add(
                    KnowledgeChunkRow(
                        id=new_id("chk"),
                        project_id=self.project_id,
                        scope=scope,
                        kind=chunk["kind"],
                        file_path=chunk["file_path"],
                        symbol=chunk["symbol"],
                        start_line=chunk["start_line"],
                        end_line=chunk["end_line"],
                        content=chunk["content"],
                        summary=chunk["summary"],
                        content_hash=_hash(chunk["content"]),
                        embedding=vectors[index] if index < len(vectors) else [],
                        embedding_model=model_name,
                        tokens=max(1, len(chunk["content"]) // 4),
                    )
                )

        profile.indexed_chunks = len(chunks)
        return profile


def _chunk_kind(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".feature"):
        return "feature"
    if "page" in lowered:
        return "page_object"
    if "step" in lowered:
        return "step"
    if "fixture" in lowered:
        return "fixture"
    if "util" in lowered or "helper" in lowered:
        return "util"
    if lowered.endswith((".md",)):
        return "doc"
    if "config" in lowered:
        return "config"
    return "code"


def summarize_conventions(profile: RepoProfile, parsed: list[ParsedFile]) -> str:
    """A compact, human-readable description injected into generation prompts."""
    pages = [s for s in profile.symbols if s.kind == "page_object"]
    fixtures = [s for s in profile.symbols if s.kind == "fixture"]
    steps = [s for s in profile.symbols if s.kind == "step"]
    utils = [s for s in profile.symbols if s.kind == "util"]

    lines: list[str] = [
        f"Repository uses {profile.language} with {profile.test_runner}"
        + (" and Cucumber BDD" if profile.bdd else "")
        + f" ({profile.package_manager}).",
    ]
    if profile.detected_layout:
        lines.append(
            "Layout: " + ", ".join(f"{k.replace('_dir', '')}={v}" for k, v in sorted(profile.detected_layout.items()))
        )
    if pages:
        base = profile.naming_conventions.get("page_object_base_class")
        lines.append(
            f"{len(pages)} existing Page Object(s): {', '.join(p.name for p in pages[:12])}"
            + (f". They extend {base}." if base else ".")
        )
        exemplar = max(pages, key=lambda p: len(p.members), default=None)
        if exemplar and exemplar.members:
            lines.append(
                f"Exemplar {exemplar.name} exposes: {', '.join(exemplar.members[:10])}."
            )
    if fixtures:
        lines.append(f"Reusable fixtures: {', '.join(f.name for f in fixtures[:12])} — use these, do not redefine them.")
    if utils:
        lines.append(f"Utilities available: {', '.join(sorted({u.name for u in utils})[:12])}.")
    if steps:
        lines.append(f"{len(steps)} step definition(s)/scenario(s) already exist — reuse matching step text verbatim.")
    if profile.naming_conventions:
        lines.append(
            "Naming: " + ", ".join(f"{k}={v}" for k, v in sorted(profile.naming_conventions.items()))
        )
    total_locators = sum(p.locator_count for p in parsed)
    if total_locators:
        lines.append(f"{total_locators} locator call(s) found; keep locators inside Page Objects.")
    return "\n".join(lines)


# =========================================================================== #
# Retrieval
# =========================================================================== #
def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def keyword_score(query: str, content: str) -> float:
    """Simple lexical overlap — catches exact identifier matches embeddings miss."""
    q = {t.lower() for t in _TOKEN_RE.findall(query)}
    if not q:
        return 0.0
    c = Counter(t.lower() for t in _TOKEN_RE.findall(content))
    if not c:
        return 0.0
    hits = sum(1 for token in q if token in c)
    return hits / len(q)


class KnowledgeRetriever:
    """Hybrid semantic + lexical retrieval over the indexed repository."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    async def search(
        self,
        query: str,
        router: Any = None,
        limit: int = 8,
        kinds: Iterable[str] | None = None,
        scope: str = "repository",
    ) -> list[dict[str, Any]]:
        query_vector: list[float] = []
        if router is not None:
            try:
                vectors = await router.embed([query])
                query_vector = vectors[0] if vectors else []
            except Exception:  # noqa: BLE001
                query_vector = []

        with session_scope() as session:
            stmt = select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.project_id == self.project_id,
                KnowledgeChunkRow.scope == scope,
            )
            if kinds:
                stmt = stmt.where(KnowledgeChunkRow.kind.in_(list(kinds)))
            rows = list(session.execute(stmt).scalars())

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            semantic = cosine(query_vector, row.embedding or []) if query_vector else 0.0
            lexical = keyword_score(query, f"{row.file_path} {row.content}")
            # Weighted hybrid: semantics lead, lexical breaks ties and rescues
            # exact identifier lookups.
            score = (0.65 * semantic + 0.35 * lexical) if query_vector else lexical
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "file_path": row.file_path,
                        "symbol": row.symbol,
                        "kind": row.kind,
                        "start_line": row.start_line,
                        "end_line": row.end_line,
                        "content": row.content,
                        "score": round(score, 4),
                        "semantic": round(semantic, 4),
                        "lexical": round(lexical, 4),
                    },
                )
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        return [payload for _score, payload in scored[:limit]]

    def context_block(self, results: list[dict[str, Any]], max_chars: int = 12000) -> str:
        """Render retrieved chunks as a prompt section, budget-aware."""
        parts: list[str] = []
        used = 0
        for item in results:
            header = f"--- {item['file_path']} (lines {item['start_line']}-{item['end_line']}) ---"
            body = item["content"]
            block = f"{header}\n{body}\n"
            if used + len(block) > max_chars:
                remaining = max_chars - used - len(header) - 20
                if remaining > 200:
                    parts.append(f"{header}\n{body[:remaining]}\n... (truncated)\n")
                break
            parts.append(block)
            used += len(block)
        return "\n".join(parts)
