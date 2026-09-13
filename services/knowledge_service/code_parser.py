"""Lightweight, dependency-free source analysis.

A full TypeScript compiler would be more precise, but it would also mean
shipping a Node toolchain just to *read* a repository. Regex extraction gets us
the things the agents actually need — which Page Objects exist, what methods
they expose, which fixtures are already defined — with zero install cost, and
degrades to "found nothing" rather than crashing on unusual syntax.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from packages.aiqa_types.models import RepoSymbol

# --------------------------------------------------------------------------- #
# TypeScript / JavaScript
# --------------------------------------------------------------------------- #
TS_CLASS_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"
    r"(?:\s+extends\s+(?P<base>[A-Za-z_$][\w$.]*))?",
    re.MULTILINE,
)
TS_FUNCTION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<args>[^)]*)\)",
    re.MULTILINE,
)
TS_ARROW_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:const|let)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?\(",
    re.MULTILINE,
)
TS_CONST_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=",
    re.MULTILINE,
)
TS_TYPE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:type|interface)\s+(?P<name>[A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
TS_METHOD_RE = re.compile(
    r"^\s{2,}(?P<modifiers>(?:public|private|protected|readonly|static|async|get|set)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<args>[^)]*)\)\s*(?::\s*(?P<ret>[^{;]+))?\s*\{",
    re.MULTILINE,
)
TS_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:(?P<names>[^'\"]+?)\s+from\s+)?['\"](?P<module>[^'\"]+)['\"]",
    re.MULTILINE,
)
TS_FIXTURE_RE = re.compile(r"\bbase\.extend\s*<\s*(?P<type>[\w\s|,<>\[\]]*)\s*>\s*\(", re.MULTILINE)
TS_FIXTURE_KEY_RE = re.compile(r"^\s{2}(?P<name>[A-Za-z_$][\w$]*)\s*:\s*async\s*\(", re.MULTILINE)
LOCATOR_RE = re.compile(r"(getBy[A-Z]\w*\(|page\.locator\(|\$\(['\"]~)")

# Cucumber / BDD
STEP_DEF_RE = re.compile(
    r"^\s*(?P<keyword>Given|When|Then|And|But|defineStep)\s*\(\s*(?P<quote>['\"`])(?P<text>.+?)(?P=quote)",
    re.MULTILINE,
)

# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #
PY_CLASS_RE = re.compile(r"^class\s+(?P<name>\w+)\s*(?:\((?P<base>[^)]*)\))?\s*:", re.MULTILINE)
PY_FUNC_RE = re.compile(r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>\w+)\s*\((?P<args>[^)]*)\)", re.MULTILINE)
PY_FIXTURE_RE = re.compile(r"@(?:pytest\.)?fixture[^\n]*\n\s*(?:async\s+)?def\s+(?P<name>\w+)", re.MULTILINE)

# Gherkin
FEATURE_RE = re.compile(r"^\s*Feature:\s*(?P<name>.+)$", re.MULTILINE)
SCENARIO_RE = re.compile(r"^\s*(?P<kind>Scenario Outline|Scenario|Example):\s*(?P<name>.+)$", re.MULTILINE)
TAG_LINE_RE = re.compile(r"^\s*(@[\w\-@\s]+)$", re.MULTILINE)
GHERKIN_STEP_RE = re.compile(r"^\s*(?P<keyword>Given|When|Then|And|But|\*)\s+(?P<text>.+)$", re.MULTILINE)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _classify_ts(rel_path: str, class_name: str, base: str | None) -> str:
    lowered = rel_path.lower()
    if "page" in lowered or class_name.endswith("Page") or (base or "").endswith("Page"):
        return "page_object"
    if "component" in lowered or class_name.endswith("Component"):
        return "component"
    if "fixture" in lowered:
        return "fixture"
    if "util" in lowered or "helper" in lowered:
        return "util"
    return "component"


@dataclass
class ParsedFile:
    path: str
    language: str
    symbols: list[RepoSymbol]
    imports: list[str]
    step_texts: list[str]
    feature_names: list[str]
    scenario_names: list[str]
    locator_count: int
    lines: int


def parse_typescript(rel_path: str, text: str) -> ParsedFile:
    symbols: list[RepoSymbol] = []

    for match in TS_CLASS_RE.finditer(text):
        name = match.group("name")
        base = match.group("base")
        kind = _classify_ts(rel_path, name, base)
        body = _class_body(text, match.end())
        members = [
            m.group("name")
            for m in TS_METHOD_RE.finditer(body)
            if m.group("name") not in ("constructor", "if", "for", "while", "switch", "catch", "function")
        ]
        symbols.append(
            RepoSymbol(
                name=name,
                kind=kind,
                file_path=rel_path,
                line=_line_of(text, match.start()),
                signature=f"class {name}" + (f" extends {base}" if base else ""),
                exported="export" in match.group(0),
                members=sorted(set(members))[:40],
                summary=f"{kind.replace('_', ' ')} with {len(set(members))} method(s)"
                + (f", extends {base}" if base else ""),
            )
        )

    for regex, kind in ((TS_FUNCTION_RE, "util"), (TS_ARROW_EXPORT_RE, "util")):
        for match in regex.finditer(text):
            name = match.group("name")
            if any(s.name == name for s in symbols):
                continue
            symbols.append(
                RepoSymbol(
                    name=name,
                    kind="fixture" if "fixture" in rel_path.lower() else kind,
                    file_path=rel_path,
                    line=_line_of(text, match.start()),
                    signature=match.group(0).strip()[:160],
                    exported="export" in match.group(0),
                )
            )

    for match in TS_TYPE_RE.finditer(text):
        symbols.append(
            RepoSymbol(
                name=match.group("name"),
                kind="type",
                file_path=rel_path,
                line=_line_of(text, match.start()),
                signature=match.group(0).strip()[:120],
                exported="export" in match.group(0),
            )
        )

    # Playwright fixtures declared via base.extend({...})
    if TS_FIXTURE_RE.search(text):
        for match in TS_FIXTURE_KEY_RE.finditer(text):
            name = match.group("name")
            symbols.append(
                RepoSymbol(
                    name=name,
                    kind="fixture",
                    file_path=rel_path,
                    line=_line_of(text, match.start()),
                    signature=f"fixture {name}",
                    summary="Playwright test fixture — reuse instead of constructing pages manually.",
                )
            )

    step_texts: list[str] = []
    for match in STEP_DEF_RE.finditer(text):
        step_texts.append(f"{match.group('keyword')} {match.group('text')}")
        symbols.append(
            RepoSymbol(
                name=match.group("text")[:120],
                kind="step",
                file_path=rel_path,
                line=_line_of(text, match.start()),
                signature=f"{match.group('keyword')}('{match.group('text')[:90]}')",
            )
        )

    imports = [m.group("module") for m in TS_IMPORT_RE.finditer(text)]
    return ParsedFile(
        path=rel_path,
        language="typescript",
        symbols=symbols,
        imports=imports,
        step_texts=step_texts,
        feature_names=[],
        scenario_names=[],
        locator_count=len(LOCATOR_RE.findall(text)),
        lines=text.count("\n") + 1,
    )


def _class_body(text: str, start: int) -> str:
    """Return the braced body that begins at/after ``start``."""
    open_index = text.find("{", start)
    if open_index == -1:
        return ""
    depth = 0
    for idx in range(open_index, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index : idx + 1]
    return text[open_index:]


def parse_python(rel_path: str, text: str) -> ParsedFile:
    symbols: list[RepoSymbol] = []
    for match in PY_CLASS_RE.finditer(text):
        name = match.group("name")
        kind = "page_object" if name.endswith("Page") or "page" in rel_path.lower() else "component"
        symbols.append(
            RepoSymbol(
                name=name, kind=kind, file_path=rel_path, line=_line_of(text, match.start()),
                signature=match.group(0).strip().rstrip(":"),
                members=[m.group("name") for m in PY_FUNC_RE.finditer(text) if m.group("indent")][:40],
            )
        )
    for match in PY_FIXTURE_RE.finditer(text):
        symbols.append(
            RepoSymbol(
                name=match.group("name"), kind="fixture", file_path=rel_path,
                line=_line_of(text, match.start()), signature=f"@fixture {match.group('name')}",
            )
        )
    for match in PY_FUNC_RE.finditer(text):
        if match.group("indent"):
            continue
        name = match.group("name")
        if name.startswith("_") or any(s.name == name for s in symbols):
            continue
        symbols.append(
            RepoSymbol(
                name=name, kind="util", file_path=rel_path, line=_line_of(text, match.start()),
                signature=match.group(0).strip(),
            )
        )
    return ParsedFile(
        path=rel_path, language="python", symbols=symbols,
        imports=re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, re.MULTILINE),
        step_texts=[], feature_names=[], scenario_names=[],
        locator_count=len(LOCATOR_RE.findall(text)), lines=text.count("\n") + 1,
    )


def parse_feature(rel_path: str, text: str) -> ParsedFile:
    features = [m.group("name").strip() for m in FEATURE_RE.finditer(text)]
    scenarios = [m.group("name").strip() for m in SCENARIO_RE.finditer(text)]
    steps = [f"{m.group('keyword')} {m.group('text').strip()}" for m in GHERKIN_STEP_RE.finditer(text)]
    symbols = [
        RepoSymbol(
            name=name, kind="step", file_path=rel_path, line=0,
            signature="scenario", summary="existing scenario — check for overlap before adding new coverage",
        )
        for name in scenarios
    ]
    return ParsedFile(
        path=rel_path, language="gherkin", symbols=symbols, imports=[],
        step_texts=steps, feature_names=features, scenario_names=scenarios,
        locator_count=0, lines=text.count("\n") + 1,
    )


def parse_file(rel_path: str, text: str) -> ParsedFile:
    suffix = Path(rel_path).suffix.lower()
    if suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return parse_typescript(rel_path, text)
    if suffix == ".py":
        return parse_python(rel_path, text)
    if suffix == ".feature":
        return parse_feature(rel_path, text)
    return ParsedFile(
        path=rel_path, language=suffix.lstrip("."), symbols=[], imports=[],
        step_texts=[], feature_names=[], scenario_names=[], locator_count=0,
        lines=text.count("\n") + 1,
    )


# --------------------------------------------------------------------------- #
# Chunking for retrieval
# --------------------------------------------------------------------------- #
def chunk_text(text: str, max_lines: int = 80, overlap: int = 10) -> Iterable[tuple[int, int, str]]:
    """Yield ``(start_line, end_line, content)`` windows.

    Chunks break on blank lines near the target size so a Page Object method is
    rarely split across two chunks.
    """
    lines = text.splitlines()
    if not lines:
        return
    position = 0
    total = len(lines)
    while position < total:
        end = min(position + max_lines, total)
        if end < total:
            # Nudge the boundary to the nearest blank line within 15 lines.
            for probe in range(end, max(position + max_lines - 15, position + 1), -1):
                if probe < total and not lines[probe - 1].strip():
                    end = probe
                    break
        yield position + 1, end, "\n".join(lines[position:end])
        if end >= total:
            return
        position = max(end - overlap, position + 1)
