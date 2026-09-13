"""Standards engine — the team's conventions, as data.

Three sources, merged in increasing order of specificity:

    COMPANY   configs/standards.yaml            the organization baseline
    PROJECT   <repo>/.aiqa/config.yaml          this repository's rules
    MODULE    <repo>/.aiqa/modules/<name>.yaml  one area's overrides

Plus two things a YAML file cannot express well:

* **Prose standards** (`.aiqa/standards/*.md`) — how the team actually talks
  about its conventions. Parsed into structured rules once and cached by content
  hash, so the (cheap) model call happens on the first read and never again.
* **Example files** (`.aiqa/examples/`) — the team's own Page Object, feature and
  step files. These are analysed statically to learn naming, structure, locator
  strategy and assertion style. Examples are used as *patterns to follow*, not
  text to copy.

The output is a single merged standards document plus a compact "house style"
briefing that generation prompts embed. Because it is stable across runs, it is
also the prompt prefix we mark as cacheable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from configs.settings import load_default_standards
from services.knowledge_service.code_parser import parse_typescript

log = logging.getLogger("aiqa.standards")

AIQA_DIR = ".aiqa"
STANDARDS_SUBDIR = "standards"
EXAMPLES_SUBDIR = "examples"
MODULES_SUBDIR = "modules"

#: Prose files the scaffolder creates, in the order they are presented to a model.
STANDARD_DOCS = (
    "architecture.md", "playwright.md", "gherkin.md", "pom.md",
    "locators.md", "fixtures.md", "assertions.md", "naming.md", "security.md",
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}


# =========================================================================== #
# Learned house style
# =========================================================================== #
@dataclass
class HouseStyle:
    """What the team's own example files reveal about their conventions."""

    page_object_base_class: str = ""
    page_object_suffix: str = "Page"
    locator_visibility: str = "private"          # private | public
    locator_accessor: str = "getter"             # getter | field | method
    preferred_locators: list[str] = field(default_factory=list)
    method_style: str = "async"
    assertion_style: str = ""                    # e.g. "expect(...).toBeVisible()"
    assertions_in_page_objects: bool = False
    import_style: list[str] = field(default_factory=list)
    step_parameter_style: str = "{string}"
    gherkin_style: str = ""
    fixture_style: str = ""
    examples_analysed: list[str] = field(default_factory=list)

    def briefing(self) -> str:
        """Compact prose for a generation prompt — facts only, no filler."""
        lines: list[str] = []
        if self.page_object_base_class:
            lines.append(f"Page Objects extend `{self.page_object_base_class}`.")
        if self.page_object_suffix:
            lines.append(f"Page Object classes are named `<Feature>{self.page_object_suffix}`.")
        if self.locator_accessor == "getter":
            lines.append(f"Locators are exposed as {self.locator_visibility} getters, never inline in methods.")
        elif self.locator_accessor:
            lines.append(f"Locators are {self.locator_visibility} {self.locator_accessor}s.")
        if self.preferred_locators:
            lines.append("Observed locator strategies, most used first: " + ", ".join(self.preferred_locators[:5]) + ".")
        if self.method_style == "async":
            lines.append("Action methods are `async` and return `Promise<void>` unless they return a value.")
        if self.assertion_style:
            lines.append(f"Assertions look like: `{self.assertion_style}`.")
        lines.append(
            "Assertions live in Page Objects."
            if self.assertions_in_page_objects
            else "Assertions live in steps/tests, not in Page Objects."
        )
        if self.import_style:
            lines.append("Typical imports: " + "; ".join(self.import_style[:3]) + ".")
        if self.step_parameter_style:
            lines.append(f"Step parameters use `{self.step_parameter_style}`.")
        if self.gherkin_style:
            lines.append(f"Gherkin style: {self.gherkin_style}")
        if self.examples_analysed:
            lines.append("Learned from: " + ", ".join(self.examples_analysed[:6]) + ".")
        return "\n".join(f"- {line}" for line in lines)


def learn_house_style(example_dir: Path) -> HouseStyle:
    """Derive conventions from the team's own example files.

    Static analysis, not a model call: the team supplied real code, and reading
    it is both free and more reliable than asking an LLM to describe it.
    """
    style = HouseStyle()
    if not example_dir.is_dir():
        return style

    locator_counts: dict[str, int] = {}
    assertion_samples: list[str] = []
    imports: list[str] = []

    for path in sorted(example_dir.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        style.examples_analysed.append(path.name)

        if suffix == ".feature":
            style.gherkin_style = _learn_gherkin(text) or style.gherkin_style
            continue
        if suffix not in (".ts", ".tsx", ".js"):
            continue

        parsed = parse_typescript(path.name, text)

        # Base class + naming, from real page objects.
        for symbol in parsed.symbols:
            if symbol.kind == "page_object" and "extends" in symbol.signature:
                style.page_object_base_class = symbol.signature.split("extends")[-1].strip()
            if symbol.kind == "page_object" and symbol.name.endswith("Page"):
                style.page_object_suffix = "Page"

        # Locator strategy frequency.
        for match in re.finditer(r"\b(getBy[A-Z]\w*|locator)\s*\(", text):
            key = match.group(1)
            locator_counts[key] = locator_counts.get(key, 0) + 1

        # Are locators private getters?
        if re.search(r"^\s*private\s+get\s+\w+\s*\(\s*\)\s*\{", text, re.MULTILINE):
            style.locator_visibility, style.locator_accessor = "private", "getter"
        elif re.search(r"^\s*(readonly|private)\s+\w+\s*=", text, re.MULTILINE):
            style.locator_accessor = "field"

        # Assertion shape and placement.
        for match in re.finditer(r"(await\s+expect\([^)]*\)\.\w+\([^)]*\))", text):
            assertion_samples.append(match.group(1).strip())
        if "Page" in path.stem and "expect(" in text:
            style.assertions_in_page_objects = True

        for match in re.finditer(r"^\s*import\s+.+$", text, re.MULTILINE):
            line = match.group(0).strip()
            if line not in imports:
                imports.append(line)

        if re.search(r"\{(string|int|float|word)\}", text):
            style.step_parameter_style = "{string}"

        if re.search(r"async\s+\w+\s*\([^)]*\)\s*:\s*Promise<", text):
            style.method_style = "async"

    style.preferred_locators = [
        name for name, _count in sorted(locator_counts.items(), key=lambda kv: -kv[1])
    ]
    if assertion_samples:
        style.assertion_style = assertion_samples[0][:120]
    style.import_style = imports[:5]
    return style


def _learn_gherkin(text: str) -> str:
    """Describe the team's Gherkin habits from one of their feature files."""
    traits: list[str] = []
    if re.search(r"^\s*@\w+", text, re.MULTILINE):
        tags = sorted(set(re.findall(r"@[\w-]+", text)))[:6]
        traits.append("tags used: " + ", ".join(tags))
    if re.search(r"^\s*Background:", text, re.MULTILINE):
        traits.append("Background blocks are used for shared preconditions")
    if re.search(r"^\s*Scenario Outline:", text, re.MULTILINE):
        traits.append("Scenario Outline + Examples used for data-driven cases")
    if re.search(r"Scenario:\s*[A-Z]+-[A-Z0-9]+-\d+", text):
        traits.append("scenario titles are prefixed with a test id")
    declarative = len(re.findall(r"^\s*(When|Then)\s+I\s+\w+", text, re.MULTILINE))
    technical = len(re.findall(r"(click|css=|#\w+|xpath)", text, re.IGNORECASE))
    if declarative > technical:
        traits.append("step text is declarative (business language, no selectors)")
    return "; ".join(traits)


# =========================================================================== #
# Free-text standards -> structured rules
# =========================================================================== #
#: Patterns that turn common prose conventions into deterministic rules with no
#: model call at all. Only what these miss is escalated to an LLM.
_PROSE_PATTERNS: list[tuple[re.Pattern[str], dict[str, Any]]] = [
    (
        re.compile(r"(?i)\b(all\s+)?locators?\s+(should|must)\s+use\s+(?P<strategy>getBy\w+|data-testid)"),
        {"kind": "locator_preference", "severity": "warning"},
    ),
    (
        re.compile(r"(?i)steps?\s+(cannot|must not|should not)\s+contain\s+locators?"),
        {"id": "STD-004", "kind": "regex", "applies_to": ["steps"], "severity": "error"},
    ),
    (
        re.compile(r"(?i)assertions?\s+(must|should)\s+(remain\s+)?outside\s+(the\s+)?page\s*objects?"),
        {"id": "USR-ASSERT-OUTSIDE-POM", "kind": "semantic", "check": "no_assertions_in_page_objects",
         "applies_to": ["pages"], "severity": "error"},
    ),
    (
        re.compile(r"(?i)page\s*object\s+(must|should)\s+contain\s+all\s+(the\s+)?ui\s+interactions?"),
        {"id": "STD-004", "kind": "regex", "applies_to": ["steps"], "severity": "error"},
    ),
    (
        re.compile(r"(?i)feature\s+files?\s+(must|should)\s+use\s+(?P<tags>(@\w+[,\s and]*)+)\s*tags?"),
        {"id": "USR-REQUIRED-TAGS", "kind": "semantic", "check": "scenario_tagged", "severity": "warning"},
    ),
    (
        re.compile(r"(?i)\b(no|never use|avoid)\s+(hard\s*)?(waits?|sleeps?|waitForTimeout)"),
        {"id": "STD-001", "kind": "regex", "severity": "error"},
    ),
    (
        re.compile(r"(?i)\b(no|never use|avoid)\s+(absolute\s+)?xpath"),
        {"id": "STD-002", "kind": "regex", "severity": "error"},
    ),
    (
        re.compile(r"(?i)(every|each|all)\s+scenarios?\s+(must|should)\s+(have|be)\s+tagg?ed"),
        {"id": "STD-005", "kind": "semantic", "check": "scenario_tagged", "severity": "error"},
    ),
    (
        re.compile(r"(?i)page\s*objects?\s+(must|should)\s+extend\s+(?P<base>\w+)"),
        {"id": "USR-EXTENDS-BASE", "kind": "semantic", "check": "extends_base_page",
         "applies_to": ["pages"], "severity": "error"},
    ),
]


def parse_freeform_standards(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert pasted prose into rules. Returns ``(rules, unparsed_lines)``.

    Deterministic first: the common conventions teams write down map onto rules
    the engine already implements. Only genuinely novel statements are handed to
    a model, which keeps this feature effectively free.
    """
    rules: list[dict[str, Any]] = []
    unparsed: list[str] = []

    for raw_line in (text or "").splitlines():
        line = raw_line.strip().strip("-*•").strip().strip('"')
        if not line or len(line) < 8:
            continue

        matched = False
        for pattern, template in _PROSE_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            rule = dict(template)
            rule.setdefault("id", f"USR-{_hash(line).upper()[:6]}")
            rule["title"] = line[:120]
            rule["message"] = line[:200]
            rule["source"] = "user_prose"
            groups = match.groupdict()
            if groups.get("strategy"):
                rule["preferred_strategy"] = groups["strategy"]
            if groups.get("base"):
                rule["base_class"] = groups["base"]
            if groups.get("tags"):
                rule["required_tags"] = re.findall(r"@\w+", groups["tags"])
            rules.append(rule)
            matched = True
            break

        if not matched:
            unparsed.append(line)

    return rules, unparsed


# =========================================================================== #
# The merged standards document
# =========================================================================== #
@dataclass
class ResolvedStandards:
    """Company + project + module, merged, plus learned house style."""

    document: dict[str, Any] = field(default_factory=dict)
    house_style: HouseStyle = field(default_factory=HouseStyle)
    prose_docs: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    module: str = ""
    unparsed_prose: list[str] = field(default_factory=list)

    @property
    def rules(self) -> list[dict[str, Any]]:
        return self.document.get("rules", []) or []

    def prefix(self, max_chars: int = 6000) -> str:
        """The stable prompt prefix. Identical across runs, so worth caching."""
        parts: list[str] = ["## House standards (authoritative)"]
        framework = self.document.get("framework", {}) or {}
        if framework:
            parts.append(
                f"Framework: {framework.get('language', '')} + {framework.get('runner', '')}"
                + (" + BDD" if framework.get("bdd") else "")
                + f", {framework.get('pattern', '')}"
            )
        layout = self.document.get("layout", {}) or {}
        if layout:
            parts.append("Layout: " + ", ".join(f"{k.replace('_dir','')}={v}" for k, v in sorted(layout.items())))
        locators = (self.document.get("locators", {}) or {}).get("strategy_priority") or []
        if locators:
            parts.append("Locator priority: " + " > ".join(locators))

        style = self.house_style.briefing()
        if style:
            parts += ["", "## Learned from this team's own example files", style]

        blocking = [
            f"- [{r.get('id')}] {r.get('title') or r.get('message', '')}"
            for r in self.rules
            if str(r.get("severity")) in ("error", "critical")
        ]
        if blocking:
            parts += ["", "## Blocking rules", *blocking[:25]]

        for name, body in list(self.prose_docs.items())[:4]:
            parts += ["", f"## {name}", body.strip()[:1200]]

        text = "\n".join(parts)
        return text[:max_chars]

    def fingerprint(self) -> str:
        return _hash(json.dumps(self.document, sort_keys=True, default=str) + self.house_style.briefing())


class StandardsEngine:
    """Loads, merges and caches the standards for one repository."""

    def __init__(self, repo_root: str | Path) -> None:
        self.root = Path(repo_root)
        self.aiqa = self.root / AIQA_DIR

    # ------------------------------------------------------------------ #
    def resolve(self, module: str = "") -> ResolvedStandards:
        sources: list[str] = []

        # 1. COMPANY baseline
        document: dict[str, Any] = dict(load_default_standards())
        sources.append("configs/standards.yaml (company)")

        # 2. PROJECT — both the v1 standards.yaml and the v2 config.yaml
        for candidate in (self.aiqa / "standards.yaml", self.aiqa / "config.yaml"):
            overlay = _read_yaml(candidate)
            if overlay:
                document = _merge(document, _normalise_config(overlay))
                sources.append(f"{AIQA_DIR}/{candidate.name} (project)")

        # 3. MODULE — most specific wins
        if module:
            module_file = self.aiqa / MODULES_SUBDIR / f"{module}.yaml"
            overlay = _read_yaml(module_file)
            if overlay:
                document = _merge(document, _normalise_config(overlay))
                sources.append(f"{AIQA_DIR}/{MODULES_SUBDIR}/{module}.yaml (module)")

        # 4. Prose standards -> extra rules
        prose_docs: dict[str, str] = {}
        unparsed: list[str] = []
        standards_dir = self.aiqa / STANDARDS_SUBDIR
        if standards_dir.is_dir():
            for path in sorted(standards_dir.glob("*.md")):
                try:
                    body = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                prose_docs[path.stem] = body
                derived, leftover = parse_freeform_standards(body)
                unparsed.extend(leftover)
                if derived:
                    document = _merge(document, {"rules": derived})
            if prose_docs:
                sources.append(f"{AIQA_DIR}/{STANDARDS_SUBDIR}/*.md ({len(prose_docs)} docs)")

        # 5. Example-based learning
        house_style = learn_house_style(self.aiqa / EXAMPLES_SUBDIR)
        if house_style.examples_analysed:
            sources.append(f"{AIQA_DIR}/{EXAMPLES_SUBDIR}/ ({len(house_style.examples_analysed)} files)")
            # A learned base class is a fact about the repository; promote it.
            if house_style.page_object_base_class:
                document.setdefault("naming", {})["page_object_base_class"] = house_style.page_object_base_class

        return ResolvedStandards(
            document=document,
            house_style=house_style,
            prose_docs=prose_docs,
            sources=sources,
            module=module,
            unparsed_prose=unparsed[:20],
        )

    # ------------------------------------------------------------------ #
    def scaffold(self, force: bool = False) -> list[str]:
        """Create a starter `.aiqa/` for a repository that has none."""
        created: list[str] = []
        for directory in (self.aiqa, self.aiqa / STANDARDS_SUBDIR, self.aiqa / EXAMPLES_SUBDIR, self.aiqa / MODULES_SUBDIR):
            directory.mkdir(parents=True, exist_ok=True)

        config = self.aiqa / "config.yaml"
        if force or not config.exists():
            config.write_text(_DEFAULT_CONFIG, encoding="utf-8", newline="\n")
            created.append(str(config.relative_to(self.root)))

        for name in STANDARD_DOCS:
            path = self.aiqa / STANDARDS_SUBDIR / name
            if force or not path.exists():
                path.write_text(_DEFAULT_DOCS.get(name, f"# {name[:-3].title()}\n\n_Describe your conventions here._\n"),
                                encoding="utf-8", newline="\n")
                created.append(str(path.relative_to(self.root)))

        readme = self.aiqa / "README.md"
        if force or not readme.exists():
            readme.write_text(_AIQA_README, encoding="utf-8", newline="\n")
            created.append(str(readme.relative_to(self.root)))
        return created


# --------------------------------------------------------------------------- #
def _normalise_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept the `.aiqa/config.yaml` shape and map it onto the standards schema."""
    out: dict[str, Any] = {}

    framework = raw.get("framework") or {}
    if framework:
        out["framework"] = {
            "language": framework.get("language", framework.get("lang", "typescript")),
            "runner": framework.get("automation", framework.get("runner", "playwright")),
            "bdd": str(framework.get("methodology", "")).lower() == "bdd" or bool(framework.get("bdd")),
            "pattern": framework.get("architecture", framework.get("pattern", "page-object-model")),
        }

    folders = raw.get("folders") or {}
    if folders:
        out["layout"] = {f"{key}_dir": value for key, value in folders.items()}

    locators = raw.get("locators") or {}
    if locators:
        out["locators"] = {
            "strategy_priority": locators.get("preferred", locators.get("strategy_priority", [])),
            "forbid_absolute_xpath": "absolute_xpath" in (locators.get("prohibited") or []),
        }

    # `rules:` in config.yaml is a flag map, not the rule list.
    flags = raw.get("rules")
    derived: list[dict[str, Any]] = []
    if isinstance(flags, dict):
        if flags.get("locators_in_steps") is False:
            derived.append({"id": "STD-004", "severity": "error", "source": "project_config"})
        if flags.get("assertions_in_page_objects") is False:
            derived.append(
                {"id": "USR-ASSERT-OUTSIDE-POM", "kind": "semantic",
                 "check": "no_assertions_in_page_objects", "applies_to": ["pages"],
                 "severity": "error", "title": "Assertions must not live in Page Objects",
                 "message": "Assertions belong in steps/tests, not Page Objects.", "source": "project_config"}
            )
        if flags.get("page_objects_required") is True:
            derived.append({"id": "STD-004", "severity": "error", "source": "project_config"})
    elif isinstance(flags, list):
        derived.extend(r for r in flags if isinstance(r, dict))
    if derived:
        out["rules"] = derived

    for passthrough in ("naming", "review", "organization", "version"):
        if passthrough in raw:
            out[passthrough] = raw[passthrough]
    if "standards" in raw and isinstance(raw["standards"], dict):
        out = _merge(out, raw["standards"])
    return out


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep merge, with rules merged by id so an override edits rather than replaces."""
    merged = dict(base)
    for key, value in overlay.items():
        if key == "rules":
            by_id = {r["id"]: dict(r) for r in merged.get("rules", []) if isinstance(r, dict) and "id" in r}
            for rule in value or []:
                if isinstance(rule, dict) and rule.get("id"):
                    by_id[rule["id"]] = {**by_id.get(rule["id"], {}), **rule}
            merged["rules"] = list(by_id.values())
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
_DEFAULT_CONFIG = """# AI QA Engineer — project configuration.
# Merged on top of the organization baseline. Module files in .aiqa/modules/
# override this in turn.

framework:
  automation: playwright
  language: typescript
  methodology: bdd
  architecture: pom

folders:
  features: tests/features
  pages: tests/pages
  steps: tests/steps
  fixtures: tests/fixtures
  utils: tests/utils
  data: tests/data

locators:
  preferred:
    - getByRole
    - getByLabel
    - getByPlaceholder
    - getByText
    - data-testid
  prohibited:
    - absolute_xpath

rules:
  page_objects_required: true
  locators_in_steps: false
  assertions_in_page_objects: false
  fixtures_for_shared_data: true

naming:
  test_id_prefix: "TC-"
"""

_AIQA_README = """# `.aiqa/` — AI QA Engineer project knowledge

| Path | Purpose |
|---|---|
| `config.yaml` | Machine-readable project standards. Merged over the org baseline. |
| `standards/*.md` | Your conventions in prose. Parsed into rules; anything not recognised is reported rather than ignored. |
| `examples/` | Real files of yours (a Page Object, a feature, a steps file). The platform learns naming, structure and locator style from them. |
| `modules/<name>.yaml` | Overrides for one area of the suite. Highest priority. |
| `repository_map.json` | Generated. Incremental index of the repository — safe to commit, makes CI start warm. |
| `application_map.json` | Generated. Cached pages and locators, so the app is explored once rather than per scenario. |

Precedence: **module > project > company**.

The two generated maps are what keep cost down. Committing them is recommended;
deleting them only costs one cold run.
"""

_DEFAULT_DOCS: dict[str, str] = {
    "locators.md": """# Locator standards

Write the rules the way you would say them. Recognised phrasings become
enforced rules automatically.

- All locators should use getByRole where an accessible name exists.
- Never use absolute xpath.
- Locators must live in Page Objects, not in steps.
""",
    "pom.md": """# Page Object standards

- Page Objects must extend BasePage.
- The Page Object must contain all UI interactions.
- Assertions must remain outside Page Objects.
""",
    "gherkin.md": """# Gherkin standards

- Every scenario must be tagged.
- Feature files must use @smoke and @regression tags.
- Step text is business language; no selectors, URLs or technical detail.
""",
    "assertions.md": """# Assertion standards

- Assertions must be unconditional.
- Prefer web-first assertions (`await expect(locator).toBeVisible()`).
- No hard waits.
""",
    "security.md": """# Security standards

- Credentials come from environment variables, never literals.
- Never commit tokens, keys or `.env` files.
""",
}
