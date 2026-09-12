"""Deterministic offline "model".

This is more than a test double. It is the platform's **graceful-degradation
path**: when no Ollama daemon is running and no API key is configured, the
agents still produce structurally valid, contextually relevant artifacts using
heuristics derived from the prompt. A QA engineer can therefore install the
platform and see a complete end-to-end run — requirement → plan → Gherkin →
page objects → execution → report — before ever configuring a provider.

Every response is a pure function of the request, so runs are reproducible and
the test-suite is stable.
"""

from __future__ import annotations

import json
import re
from typing import Any

from packages.llm_provider.base import BaseProvider, LLMRequest, LLMResponse

_STOPWORDS = {
    "the", "a", "an", "for", "of", "to", "and", "or", "in", "on", "with", "please",
    "automate", "test", "tests", "testing", "create", "write", "generate", "add",
    "functionality", "feature", "module", "screen", "page", "flow", "scenario",
    "scenarios", "cases", "case", "i", "want", "need", "should", "must", "can",
}


def _last_user(req: LLMRequest) -> str:
    for m in reversed(req.messages):
        if m.to_dict()["role"] == "user":
            return m.content
    return req.prompt_text


# Markers the agents use to delimit the actual user instruction inside a prompt
# that also carries project context. Without this the heuristics would derive a
# feature name from the scaffolding ("Project: acme-web-e2e") instead of the ask.
_FOCUS_MARKERS = (
    "QA engineer's request:",
    "Feature to automate:",
    "## Requirement",
    "Requirement:",
    "Title:",
)


def _focus(prompt: str) -> str:
    """Narrow a context-heavy prompt down to the instruction it is about."""
    for marker in _FOCUS_MARKERS:
        index = prompt.find(marker)
        if index == -1:
            continue
        tail = prompt[index + len(marker):].strip()
        if marker == "## Requirement":
            for line in tail.splitlines():
                if line.lower().startswith("title:"):
                    return line.split(":", 1)[1].strip()
            tail = tail.lstrip()
        # Take the first non-empty, non-metadata line.
        for line in tail.splitlines():
            candidate = line.strip()
            if candidate and not candidate.startswith(("#", "-", "*", "[", "|")):
                return candidate
    return prompt


def _keywords(text: str, limit: int = 6) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        lw = w.lower()
        if lw in _STOPWORDS or len(lw) < 3 or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _feature_name(text: str) -> str:
    kws = _keywords(text, 3)
    if not kws:
        return "Application Feature"
    return " ".join(w.capitalize() for w in kws)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "feature"


def _pascal(text: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", text) if p) or "Feature"


def _abbrev(text: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", text) if p]
    return ("".join(p[0] for p in parts[:3]) or "TC").upper()


# --------------------------------------------------------------------------- #
# Task handlers
# --------------------------------------------------------------------------- #
def _requirement(prompt: str) -> dict[str, Any]:
    name = _feature_name(prompt)
    entity = _keywords(prompt, 1)
    entity_name = entity[0].capitalize() if entity else "Record"
    return {
        "title": f"{name}",
        "summary": f"Automate verification of the {name} capability described by the QA engineer.",
        "feature_area": name,
        "actors": ["Authenticated User", "Administrator"],
        "preconditions": [
            "The application under test is reachable",
            "A user account with sufficient permissions exists",
        ],
        "acceptance_criteria": [
            {"text": f"A valid {entity_name.lower()} can be created successfully", "testable": True},
            {"text": "Mandatory field validation is enforced on submit", "testable": True},
            {"text": f"A duplicate {entity_name.lower()} is rejected with a clear message", "testable": True},
            {"text": f"The created {entity_name.lower()} is visible in the listing/search results", "testable": True},
            {"text": "The operation is permission-controlled", "testable": True},
        ],
        "business_rules": [
            "Mandatory fields must be completed before submission",
            "Unique identifiers may not be reused",
        ],
        "data_requirements": [f"Valid {entity_name.lower()} dataset", "Boundary and invalid input dataset"],
        "out_of_scope": ["Performance and load characteristics", "Cross-browser matrix beyond the default project"],
        "open_questions": [
            "Which fields are mandatory in the current release?",
            "Is there a dedicated test environment with seed data?",
        ],
        "ambiguity_score": 0.35,
    }


def _test_plan(prompt: str) -> dict[str, Any]:
    name = _feature_name(prompt)
    slug = _slug(name)
    code = _abbrev(name)
    page = f"{_pascal(name)}Page"
    return {
        "title": f"Test plan — {name}",
        "strategy": (
            "Risk-based coverage: one happy path, mandatory-field validation, a duplicate/negative case, "
            "a data-driven boundary set, and a persistence check via the listing view. "
            "UI assertions are web-first; data is verified through the API where available."
        ),
        "features": [
            {
                "name": name,
                "file_name": f"{slug}.feature",
                "description": f"As a user I want to manage {name.lower()} so that records stay accurate.",
                "tags": ["@regression", f"@{slug}"],
                "background": [
                    {"keyword": "Given", "text": "I am logged in as a standard user"},
                    {"keyword": "And", "text": f"I navigate to the {name} page"},
                ],
                "scenarios": [
                    {
                        "test_id": f"TC-{code}-001",
                        "name": f"Create a new {name.lower()} with valid details",
                        "tags": ["@smoke", "@P1"],
                        "priority": "P1",
                        "negative": False,
                        "steps": [
                            {"keyword": "When", "text": f"I complete the {name.lower()} form with valid details"},
                            {"keyword": "And", "text": "I submit the form"},
                            {"keyword": "Then", "text": "a success confirmation is displayed"},
                            {"keyword": "And", "text": "the record appears in the results list"},
                        ],
                    },
                    {
                        "test_id": f"TC-{code}-002",
                        "name": "Mandatory field validation is enforced",
                        "tags": ["@regression", "@P1", "@negative"],
                        "priority": "P1",
                        "negative": True,
                        "steps": [
                            {"keyword": "When", "text": "I submit the form without completing mandatory fields"},
                            {"keyword": "Then", "text": "a validation message is shown for each mandatory field"},
                            {"keyword": "And", "text": "the record is not created"},
                        ],
                    },
                    {
                        "test_id": f"TC-{code}-003",
                        "name": "Duplicate records are rejected",
                        "tags": ["@regression", "@P2", "@negative"],
                        "priority": "P2",
                        "negative": True,
                        "steps": [
                            {"keyword": "Given", "text": "a record already exists with the same unique identifier"},
                            {"keyword": "When", "text": "I submit the form with that identifier"},
                            {"keyword": "Then", "text": "a duplicate error message is displayed"},
                        ],
                    },
                    {
                        "test_id": f"TC-{code}-004",
                        "name": "Field boundary validation",
                        "tags": ["@regression", "@P2", "@data-driven"],
                        "priority": "P2",
                        "data_driven": True,
                        "steps": [
                            {"keyword": "When", "text": 'I enter "<value>" into the "<field>" field'},
                            {"keyword": "And", "text": "I submit the form"},
                            {"keyword": "Then", "text": 'I should see "<outcome>"'},
                        ],
                        "examples": [
                            {"field": "name", "value": "", "outcome": "Name is required"},
                            {"field": "name", "value": "A", "outcome": "Name is too short"},
                            {"field": "email", "value": "not-an-email", "outcome": "Enter a valid email"},
                        ],
                    },
                    {
                        "test_id": f"TC-{code}-005",
                        "name": "Created record is retrievable via search",
                        "tags": ["@regression", "@P2"],
                        "priority": "P2",
                        "steps": [
                            {"keyword": "Given", "text": "a record has been created"},
                            {"keyword": "When", "text": "I search for it by its unique identifier"},
                            {"keyword": "Then", "text": "the matching record is displayed"},
                        ],
                    },
                ],
            }
        ],
        "page_objects_needed": [page, "SearchResultsPage"],
        "api_checks": [f"GET /api/{slug} returns the created record"],
        "db_checks": [f"A row exists in the {slug.replace('-', '_')} table with the expected identifier"],
        "risks": [
            "Locators may be unstable if the form lacks data-testid attributes",
            "Duplicate-detection rules may differ per environment",
        ],
        "coverage_notes": "Covers all stated acceptance criteria; permissions coverage deferred to the RBAC suite.",
    }


def _failure_analysis(prompt: str) -> dict[str, Any]:
    lowered = prompt.lower()

    def result(category: str, cause: str, strategy: str, confidence: float, defect: bool = False) -> dict[str, Any]:
        return {
            "category": category,
            "confidence": confidence,
            "root_cause": cause,
            "evidence": [line.strip() for line in prompt.splitlines() if "error" in line.lower()][:3],
            "suggested_strategy": strategy,
            "is_product_defect": defect,
            "recommended_action": (
                "Raise a defect with the QA lead" if defect else "Apply the proposed automated repair and re-run"
            ),
        }

    if any(k in lowered for k in ("strict mode", "resolved to 0 elements", "no element", "not visible", "locator")):
        return result(
            "locator_broken",
            "The locator no longer resolves — the element was renamed, moved, or re-rendered.",
            "relocate_selector",
            0.86,
        )
    if any(k in lowered for k in ("timeout", "timed out", "exceeded", "waiting for")):
        return result(
            "timing_flake",
            "The step raced the application: the assertion ran before the UI settled.",
            "add_explicit_wait",
            0.78,
        )
    if any(k in lowered for k in ("econnrefused", "enotfound", "502", "503", "504", "socket hang up")):
        return result(
            "environment",
            "The environment under test was unreachable or unhealthy during the run.",
            "retry_with_backoff",
            0.9,
        )
    if any(k in lowered for k in ("duplicate", "already exists", "constraint", "seed", "fixture data")):
        return result(
            "test_data",
            "Test data collided with existing state — the dataset was not isolated or reset.",
            "refresh_test_data",
            0.8,
        )
    if "expected" in lowered and "received" in lowered:
        return result(
            "assertion_mismatch",
            "The application produced a value that differs from the documented expectation.",
            "no_action",
            0.7,
            defect=True,
        )
    return result("unknown", "Insufficient signal in the failure output to classify confidently.", "no_action", 0.3)


def _heal(prompt: str) -> dict[str, Any]:
    return {
        "strategy": "relocate_selector",
        "explanation": (
            "Replaced the brittle selector with a role/test-id based locator discovered in the live DOM snapshot, "
            "and added a web-first visibility expectation before interaction."
        ),
        "confidence": 0.72,
        "risk": "low",
    }


def _workflows(prompt: str) -> dict[str, Any]:
    name = _feature_name(prompt)
    return {
        "workflows": [
            {
                "name": f"{name} — create",
                "description": f"Primary creation journey for {name.lower()}.",
                "confidence": 0.7,
                "steps": [
                    {"order": 1, "action": "navigate", "target": "/", "description": "Open the application"},
                    {"order": 2, "action": "fill", "target": "form", "description": "Complete the form"},
                    {"order": 3, "action": "click", "target": "submit", "description": "Submit"},
                    {"order": 4, "action": "assert", "target": "confirmation", "description": "Verify confirmation"},
                ],
            }
        ]
    }


def _repository_summary(prompt: str) -> str:
    return (
        "The repository follows a Playwright + Cucumber BDD layout with Page Objects under `tests/pages`, "
        "step definitions under `tests/steps` and feature files under `tests/features`. Page Objects expose "
        "async action methods and keep all locators private. New tests should reuse the existing fixtures "
        "rather than instantiating browser contexts directly."
    )


def _report(prompt: str) -> str:
    return (
        "## Run summary\n\n"
        "The requested functionality was analysed, a risk-based test plan was produced, and executable "
        "Playwright + BDD assets were generated against the repository's existing conventions.\n\n"
        "**Next actions**\n"
        "- Review the generated feature file and page objects in the diff view\n"
        "- Confirm locators against the live environment\n"
        "- Approve the change set to commit it to a feature branch\n"
    )


def _orchestrator(prompt: str) -> dict[str, Any]:
    return {
        "agents": [
            "requirement", "repository", "exploration", "test_design",
            "code_generation", "standards", "execution", "failure_analysis",
            "self_healing", "reporting",
        ],
        "reasoning": "Full pipeline selected: the instruction requests new automation for an untested feature.",
    }


_HANDLERS: dict[str, Any] = {
    "requirement.analyze": _requirement,
    "test_design.plan": _test_plan,
    "failure_analysis.classify": _failure_analysis,
    "self_healing.propose": _heal,
    "exploration.workflows": _workflows,
    "repository.summarize": _repository_summary,
    "reporting.summarize": _report,
    "orchestrator.route": _orchestrator,
}


class MockProvider(BaseProvider):
    """Offline, deterministic provider used for tests and no-credential installs."""

    name = "mock"
    requires_api_key = False
    supports_embeddings = False

    def __init__(self, default_model: str = "mock-fast") -> None:
        super().__init__(default_model=default_model)

    async def _chat(self, req: LLMRequest) -> LLMResponse:
        prompt = _focus(_last_user(req))
        handler = _HANDLERS.get(req.task)

        if handler is None:
            # Unknown task: echo a structurally safe answer.
            payload: Any = {"result": "ok", "task": req.task, "note": "offline deterministic provider"}
            text = json.dumps(payload, indent=2) if req.json_mode else _report(prompt)
        else:
            produced = handler(prompt)
            text = json.dumps(produced, indent=2) if isinstance(produced, (dict, list)) else str(produced)

        return LLMResponse(text=text, model=req.model or self.default_model, finish_reason="stop")

    async def health(self) -> bool:
        return True
