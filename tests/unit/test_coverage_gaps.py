"""Coverage-gap analysis.

This is the report a QA lead takes into a release meeting, so the only thing
that matters is that the numbers are not flattering. An early version counted a
route as covered if exploration had merely *visited* it, which made one run of
one feature look like 86% route coverage. Most of these tests exist to keep
that class of lie out.
"""

from __future__ import annotations

from services.knowledge_service.application_map import ApplicationMap, PageKnowledge
from services.knowledge_service.coverage import analyse_coverage
from services.knowledge_service.test_knowledge import (
    QAKnowledgeGraph,
    TestKnowledge,
    TestKnowledgeStore,
)


def _app_map() -> ApplicationMap:
    return ApplicationMap(
        pages={
            "/login": PageKnowledge(
                route="/login",
                forms=[{"action": "/session", "method": "post"}],
                elements=[{"name": "User", "role": "textbox", "locator": "getByTestId('u')"}],
            ).__dict__,
            "/residents/new": PageKnowledge(
                route="/residents/new",
                forms=[{"action": "/residents", "method": "post"}],
                elements=[{"name": "Name", "role": "textbox", "locator": "getByTestId('n')"}],
            ).__dict__,
            "/reports": PageKnowledge(route="/reports").__dict__,
        },
        components={"NavBar": {"shared": True}, "Footer": {"shared": False}},
    )


class _Store:
    """A store stub: `analyse_coverage` only ever calls `all()`."""

    def __init__(self, items: list[TestKnowledge]) -> None:
        self._items = items

    def all(self) -> list[TestKnowledge]:
        return self._items


def _test(**kwargs) -> TestKnowledge:
    return TestKnowledge(test_id=kwargs.pop("test_id", "TC-1"), name="a test", **kwargs)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def test_an_untouched_route_is_a_gap() -> None:
    report = analyse_coverage(_app_map(), _Store([_test(routes=["/residents/new"])]))
    assert report.routes_total == 3
    assert report.routes_covered == 1
    untested = {gap.key for gap in report.gaps if gap.kind == "route"}
    assert untested == {"/login", "/reports"}


def test_a_route_with_a_form_outranks_one_without() -> None:
    """An untested page that can change data is the more urgent gap."""
    report = analyse_coverage(_app_map(), _Store([]))
    severity = {gap.key: gap.severity for gap in report.gaps if gap.kind == "route"}
    assert severity["/login"] == "high"
    assert severity["/residents/new"] == "high"
    assert severity["/reports"] == "low"


def test_route_matching_ignores_a_trailing_slash() -> None:
    report = analyse_coverage(_app_map(), _Store([_test(routes=["/residents/new/"])]))
    assert report.routes_covered == 1


def test_worst_gaps_come_first() -> None:
    report = analyse_coverage(_app_map(), _Store([]))
    severities = [gap.severity for gap in report.gaps]
    assert severities == sorted(severities, key=["high", "medium", "low"].index)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def test_an_untested_write_endpoint_is_high_severity() -> None:
    report = analyse_coverage(_app_map(), _Store([]))
    endpoints = {gap.key: gap.severity for gap in report.gaps if gap.kind == "endpoint"}
    assert endpoints == {"POST /session": "high", "POST /residents": "high"}


def test_endpoint_matching_is_case_and_slash_insensitive() -> None:
    report = analyse_coverage(_app_map(), _Store([_test(apis=["post /residents/"])]))
    assert report.endpoints_covered == 1


# --------------------------------------------------------------------------- #
# Components and requirements
# --------------------------------------------------------------------------- #
def test_only_shared_components_are_expected_to_have_coverage() -> None:
    report = analyse_coverage(_app_map(), _Store([]))
    assert report.components_total == 1, "Footer is not shared, so it is not counted"
    assert {gap.key for gap in report.gaps if gap.kind == "component"} == {"NavBar"}


def test_a_requirement_with_no_test_is_a_gap() -> None:
    store = TestKnowledgeStore("prj_cov")
    graph = QAKnowledgeGraph("prj_cov")
    graph.add_node("requirement", "Resident registration", "Resident registration")
    graph.save()

    report = analyse_coverage(None, store, graph)
    assert report.requirements_total == 1
    assert report.requirements_covered == 0
    assert any(gap.kind == "requirement" and gap.severity == "high" for gap in report.gaps)


# --------------------------------------------------------------------------- #
# Happy-path-only detection
# --------------------------------------------------------------------------- #
def test_a_route_tested_only_on_the_happy_path_is_flagged() -> None:
    report = analyse_coverage(
        _app_map(),
        _Store([_test(routes=["/residents/new"], tags=["@smoke", "@P1"])]),
    )
    assert any(gap.kind == "negative" and gap.key == "/residents/new" for gap in report.gaps)


def test_a_route_with_a_negative_scenario_is_not_flagged() -> None:
    report = analyse_coverage(
        _app_map(),
        _Store(
            [
                _test(test_id="TC-1", routes=["/residents/new"], tags=["@smoke"]),
                _test(test_id="TC-2", routes=["/residents/new"], tags=["@negative"]),
            ]
        ),
    )
    assert not any(gap.kind == "negative" for gap in report.gaps)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_a_fully_covered_project_reports_no_gaps() -> None:
    covered = _Store(
        [
            _test(test_id="TC-1", routes=["/login"], apis=["POST /session"], tags=["@negative"]),
            _test(
                test_id="TC-2",
                routes=["/residents/new", "/reports"],
                apis=["POST /residents"],
                components=["NavBar"],
                tags=["@negative"],
            ),
        ]
    )
    report = analyse_coverage(_app_map(), covered)
    assert report.gaps == []
    assert report.route_pct == 100.0
    assert "No coverage gaps" in report.summary()


def test_the_summary_never_claims_a_route_is_well_tested() -> None:
    report = analyse_coverage(_app_map(), _Store([_test(routes=["/login"], tags=["@negative"])]))
    assert "touched" in report.summary()
    assert "well tested" not in report.summary()


def test_the_report_serialises_for_the_api() -> None:
    payload = analyse_coverage(_app_map(), _Store([])).to_dict()
    assert payload["routes"]["total"] == 3
    assert payload["gap_count"] == len(payload["gaps"])
    assert all({"kind", "key", "severity", "suggested_instruction"} <= set(g) for g in payload["gaps"])


def test_every_gap_carries_something_to_run() -> None:
    """A gap report nobody can act on is just a list of reasons to feel bad."""
    report = analyse_coverage(_app_map(), _Store([]))
    assert report.gaps
    assert all(gap.suggested_instruction.strip() for gap in report.gaps)


def test_no_knowledge_at_all_is_not_a_crash() -> None:
    report = analyse_coverage(None, None, None)
    assert report.gaps == []
    assert report.route_pct == 0.0
