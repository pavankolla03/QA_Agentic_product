"""Which routes exploration decides to crawl.

Asked to automate "the registration form at /residents/new", exploration
crawled `/registration`, `/registrations` and `/resident/new` — inflections of
the *words* in the sentence — and never visited the path in it. The one piece
of certain information available was the one thing ignored.

These tests also exist because the first version of that fix read
`requirement.description`, a field `Requirement` does not have. Exploration
raised AttributeError, the orchestrator correctly treated it as optional and
carried on, and the run generated scaffolding from an empty locator catalogue
while reporting success. A typo cost a whole run and nothing failed.
"""

from __future__ import annotations

from agents.exploration.agent import ExplorationAgent, _explicit_paths, _requirement_text
from packages.aiqa_types.models import AcceptanceCriterion, Requirement
from services.knowledge_service.application_map import ApplicationMap


class _Ctx:
    """The handful of attributes `_candidate_routes` actually reads."""

    def __init__(self, instruction: str, requirement: Requirement | None = None) -> None:
        self.instruction = instruction
        self.requirement = requirement


def _requirement(raw: str, **kwargs: object) -> Requirement:
    return Requirement(raw_input=raw, title="Resident registration", **kwargs)  # type: ignore[arg-type]


def test_a_path_in_the_request_is_crawled_first() -> None:
    ctx = _Ctx(
        "Automate the resident registration form at /residents/new",
        _requirement("Automate the resident registration form at /residents/new"),
    )
    routes = ExplorationAgent()._candidate_routes(ctx, ApplicationMap(project_id="p"))
    assert routes[0] == "/residents/new", routes
    assert "/" in routes, "the root is still worth a look"


def test_every_field_of_a_real_requirement_is_readable() -> None:
    """The regression that made this file necessary.

    `Requirement` has `raw_input`, `summary`, `preconditions` and
    `acceptance_criteria` — not `description`. Reading a field that does not
    exist raised inside an agent the orchestrator treats as optional, so the
    run continued with no locators at all and still reported success.
    """
    requirement = _requirement(
        "Automate the form",
        summary="Cover /residents/new end to end",
        preconditions=["the admin is signed in at /login"],
        acceptance_criteria=[AcceptanceCriterion(text="posting to /api/residents returns 201")],
    )
    text = _requirement_text(_Ctx("Automate the form", requirement))
    assert "/residents/new" in text
    assert "/login" in text
    assert "/api/residents" in text


def test_no_requirement_yet_is_not_an_error() -> None:
    """Exploration can run before requirement analysis has produced anything."""
    routes = ExplorationAgent()._candidate_routes(
        _Ctx("Automate /admin/users"), ApplicationMap(project_id="p")
    )
    assert routes[0] == "/admin/users"


def test_a_fraction_is_not_a_route() -> None:
    assert _explicit_paths("split the list 3/4 of the way down") == []
    assert _explicit_paths("read docs/setup.md first") == []


def test_trailing_punctuation_is_not_part_of_the_path() -> None:
    assert _explicit_paths("check /reports, then /admin.") == ["/reports", "/admin"]
