"""Autonomous exploratory testing.

With no requirement to check against, the only defensible findings are failures
that need no specification to recognise. The risk is the opposite of a missed
bug: a report full of plausible-sounding maybes that nobody reads, at which
point the feature is worse than not having it.

So these tests are mostly about what the oracle refuses to call a defect.
"""

from __future__ import annotations

import pytest

from services.execution_service.exploratory import (
    ExploratoryFinding,
    Probe,
    collapse,
    evaluate,
    plan_probes,
    rank,
    regression_instruction,
)
from services.knowledge_service.application_map import ApplicationMap, PageKnowledge


def _app_map() -> ApplicationMap:
    return ApplicationMap(
        pages={
            "/": PageKnowledge(
                route="/",
                navigations=["/dashboard", "/logout"],
                forms=[{"action": "/login", "method": "post"}],
                elements=[
                    {"name": "User", "role": "textbox", "locator": "getByTestId('u')", "required": True},
                    {"name": "Pass", "role": "textbox", "locator": "getByTestId('p')", "required": True},
                ],
            ).__dict__,
            "/dashboard": PageKnowledge(route="/dashboard").__dict__,
            "/residents/:id/edit": PageKnowledge(route="/residents/:id/edit").__dict__,
        }
    )


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def test_only_observed_routes_are_probed() -> None:
    """Guessing URLs would make this a scanner pointed at someone's servers."""
    routes = {p.route for p in plan_probes(_app_map())}
    assert "/admin" not in routes and "/.env" not in routes
    assert {"/", "/dashboard", "/residents/:id/edit"} <= routes


def test_a_link_matching_a_known_route_pattern_is_not_a_broken_link() -> None:
    """`/residents/1/edit` is an instance of `/residents/:id/edit`, not a dead end."""
    amap = _app_map()
    amap.pages["/residents"] = PageKnowledge(
        route="/residents", navigations=["/residents/1/edit", "/residents/2/edit"]
    ).__dict__
    broken = [p.route for p in plan_probes(amap) if p.kind == "broken_link"]
    assert broken == ["/logout"]


def test_a_form_with_required_fields_gets_an_empty_submission_probe() -> None:
    probes = [p for p in plan_probes(_app_map()) if p.kind == "missing_validation"]
    assert len(probes) == 1
    assert probes[0].route == "/login" and probes[0].method == "POST"
    assert probes[0].payload == {}


def test_a_form_with_no_required_fields_is_not_probed_for_validation() -> None:
    amap = ApplicationMap(
        pages={
            "/search": PageKnowledge(
                route="/search",
                forms=[{"action": "/search", "method": "post"}],
                elements=[{"name": "q", "role": "textbox", "locator": "getByTestId('q')"}],
            ).__dict__
        }
    )
    assert [p for p in plan_probes(amap) if p.kind == "missing_validation"] == []


def test_no_application_map_means_no_probes() -> None:
    assert plan_probes(None) == []


# --------------------------------------------------------------------------- #
# Judging a response
# --------------------------------------------------------------------------- #
def test_a_5xx_is_a_critical_finding() -> None:
    findings = evaluate(Probe(kind="reachable", route="/x"), 500, "oops")
    assert findings[0].kind == "server_error"
    assert findings[0].severity == "critical"


def test_a_stack_trace_in_the_body_is_a_finding_even_with_a_200() -> None:
    body = "<html>Traceback (most recent call last):\n  File ...</html>"
    findings = evaluate(Probe(kind="reachable", route="/x"), 200, body)
    assert any(f.kind == "error_page" for f in findings)
    assert "Traceback" in next(f for f in findings if f.kind == "error_page").evidence


def test_a_healthy_page_produces_no_defect_finding() -> None:
    findings = evaluate(
        Probe(kind="reachable", route="/x"),
        200,
        "<html>All good</html>",
        {"x-content-type-options": "nosniff", "x-frame-options": "DENY"},
    )
    assert findings == []


def test_a_dead_link_is_reported() -> None:
    findings = evaluate(Probe(kind="broken_link", route="/logout"), 404, "not found")
    assert findings[0].kind == "broken_link"


def test_a_404_on_a_normal_page_is_not_reported_as_a_dead_link() -> None:
    """Only a link the application itself offered counts."""
    assert evaluate(Probe(kind="reachable", route="/x"), 404, "not found") == []


# --------------------------------------------------------------------------- #
# The false-accusation cases
# --------------------------------------------------------------------------- #
def test_an_empty_submission_that_is_accepted_is_a_finding() -> None:
    findings = evaluate(Probe(kind="missing_validation", route="/login", method="POST"), 200, "Welcome back")
    assert findings[0].kind == "missing_validation"
    assert findings[0].severity == "high"


@pytest.mark.parametrize(
    "body",
    [
        "Username is required",
        "Please enter a password",
        "This field cannot be empty",
        "Validation failed",
        "Invalid credentials",
    ],
)
def test_a_form_redisplayed_with_errors_is_not_missing_validation(body: str) -> None:
    """Plenty of apps re-render the form with a 200. That is a rejection."""
    findings = evaluate(Probe(kind="missing_validation", route="/login", method="POST"), 200, body)
    assert [f for f in findings if f.kind == "missing_validation"] == []


def test_a_rejected_submission_with_a_4xx_is_not_a_finding() -> None:
    findings = evaluate(Probe(kind="missing_validation", route="/login", method="POST"), 422, "")
    assert findings == []


def test_a_missing_security_header_is_an_observation_not_an_assertion() -> None:
    findings = evaluate(Probe(kind="reachable", route="/x"), 200, "ok", {})
    hardening = next(f for f in findings if f.kind == "hardening")
    assert hardening.confidence == "observation"
    assert hardening.severity == "low"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_a_site_wide_observation_is_reported_once() -> None:
    """Six identical rows bury the one finding that matters."""
    findings = [
        ExploratoryFinding(kind="hardening", route=r, title=f"{r} is missing x-frame-options",
                           severity="low", confidence="observation")
        for r in ("/", "/a", "/b", "/c")
    ]
    collapsed = collapse(findings)
    assert len(collapsed) == 1
    assert collapsed[0].route == "(site-wide)"
    assert "4 route(s)" in collapsed[0].title
    assert "/a" in collapsed[0].evidence


def test_a_single_observation_is_left_alone() -> None:
    one = [ExploratoryFinding(kind="hardening", route="/", title="/ is missing x-frame-options")]
    assert collapse(one) == one


def test_real_defects_are_never_collapsed() -> None:
    findings = [
        ExploratoryFinding(kind="server_error", route="/a", title="a", severity="critical"),
        ExploratoryFinding(kind="server_error", route="/b", title="b", severity="critical"),
    ]
    assert len(collapse(findings)) == 2


def test_the_worst_finding_is_listed_first() -> None:
    findings = rank(
        [
            ExploratoryFinding(kind="hardening", route="/a", title="h", severity="low"),
            ExploratoryFinding(kind="server_error", route="/b", title="s", severity="critical"),
            ExploratoryFinding(kind="broken_link", route="/c", title="b", severity="medium"),
        ]
    )
    assert [f.severity for f in findings] == ["critical", "medium", "low"]


# --------------------------------------------------------------------------- #
def test_a_regression_instruction_asserts_correct_behaviour_not_the_bug() -> None:
    """A test that reproduces a bug gets deleted with the fix."""
    finding = ExploratoryFinding(
        kind="missing_validation", route="/login", title="accepted an empty submission"
    )
    instruction = regression_instruction(finding)
    assert "rejects" in instruction and "validation message" in instruction
    assert "reproduce" not in instruction.lower()
