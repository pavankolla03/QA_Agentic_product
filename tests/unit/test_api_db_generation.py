"""API and database checks as generated files.

A UI test proves a button works; it does not prove the record was written. These
checks verify the same acceptance criterion one layer down, so they are the
cheapest real coverage the platform can add.

They are also the easiest thing to get dangerously wrong. A test posting to a
URL nobody ever observed, or asserting a status code nobody ever saw, fails for
reasons unrelated to the product — and a suite that cries wolf gets ignored,
which is worse than having no API tests at all. Every test here is about
refusing to emit that.
"""

from __future__ import annotations

import re

from agents.code_generation.api_renderer import (
    api_checks_from,
    db_checks_from,
    render_api_tests,
    render_db_checks,
    spec_file_name,
)
from services.knowledge_service.application_map import ApplicationMap, PageKnowledge

CATALOG = [
    {
        "path": "/residents",
        "method": "POST",
        "page": "/residents/new",
        "fields": ["Full name", "Email", "Resident type"],
        "required_fields": ["Full name", "Email"],
        "source": "form_action",
    },
    {
        "path": "/residents",
        "method": "GET",
        "page": "/residents",
        "fields": [],
        "required_fields": [],
        "source": "form_action",
    },
]


# --------------------------------------------------------------------------- #
# The endpoint catalogue comes from evidence
# --------------------------------------------------------------------------- #
def test_endpoints_are_derived_from_observed_forms() -> None:
    page = PageKnowledge(
        route="/residents/new",
        forms=[{"action": "/residents", "method": "post"}],
        elements=[
            {"name": "Full name", "role": "textbox", "locator": "getByTestId('n')", "required": True},
            {"name": "Email", "role": "textbox", "locator": "getByTestId('e')", "required": True},
            {"name": "Create", "role": "button", "locator": "getByTestId('c')"},
        ],
    )
    endpoints = page.endpoints()
    assert len(endpoints) == 1
    assert endpoints[0]["method"] == "POST"
    assert endpoints[0]["path"] == "/residents"
    assert endpoints[0]["required_fields"] == ["Full name", "Email"]


def test_non_endpoints_are_not_treated_as_api_surface() -> None:
    page = PageKnowledge(
        route="/x",
        forms=[{"action": "#"}, {"action": "javascript:void(0)"}, {"action": ""}],
    )
    assert page.endpoints() == []


def test_the_catalogue_deduplicates_across_routes() -> None:
    form = {"action": "/login", "method": "post"}
    amap = ApplicationMap(
        pages={
            "/": PageKnowledge(route="/", forms=[form]).__dict__,
            "/login": PageKnowledge(route="/login", forms=[form]).__dict__,
        }
    )
    assert len(amap.api_catalog()) == 1


# --------------------------------------------------------------------------- #
# Nothing is called that was not observed
# --------------------------------------------------------------------------- #
def test_an_unobserved_endpoint_is_dropped() -> None:
    planned = [
        {"name": "real", "method": "POST", "path": "/residents"},
        {"name": "invented", "method": "DELETE", "path": "/admin/purge"},
    ]
    checks = api_checks_from(planned, CATALOG)
    assert [c.path for c in checks] == ["/residents"]


def test_prose_checks_still_yield_a_test() -> None:
    """Plans stored before checks became structured must not silently vanish."""
    checks = api_checks_from(["GET /residents returns 200"], CATALOG)
    assert len(checks) == 1
    assert checks[0].method == "GET"
    assert checks[0].expect_status == 200


def test_a_write_check_inherits_the_forms_required_fields() -> None:
    checks = api_checks_from([{"method": "POST", "path": "/residents"}], CATALOG)
    assert checks[0].body_fields == ["Full name", "Email"]


# --------------------------------------------------------------------------- #
# Nothing is emitted that fails for the wrong reason
# --------------------------------------------------------------------------- #
def test_an_unfilled_request_body_is_marked_unfinished() -> None:
    """Posting the literal string '<fullName>' is not a test, it is noise."""
    checks = api_checks_from([{"method": "POST", "path": "/residents"}], CATALOG)
    source = render_api_tests(checks, suite="Residents")
    assert "test.fixme(true, 'fill in the request body: Full name, Email');" in source


def test_a_read_check_is_immediately_runnable() -> None:
    checks = api_checks_from([{"method": "GET", "path": "/residents"}], CATALOG)
    source = render_api_tests(checks, suite="Residents")
    assert "test.fixme" not in source
    assert "await request.get('/residents');" in source


def test_an_unstated_status_asserts_success_not_a_guess() -> None:
    """A login POST returns 200 and a create returns 201; guessing breaks one."""
    checks = api_checks_from(
        [{"method": "GET", "path": "/residents", "expect_status": 0}], CATALOG
    )
    source = render_api_tests(checks, suite="Residents")
    assert "expect(response.ok()).toBeTruthy();" in source
    assert "toBe(200)" not in source


def test_a_stated_status_is_asserted_exactly() -> None:
    checks = api_checks_from(
        [{"method": "GET", "path": "/residents", "expect_status": 404, "negative": True}], CATALOG
    )
    source = render_api_tests(checks, suite="Residents")
    assert "expect(response.status()).toBe(404);" in source


def test_generated_api_source_is_balanced() -> None:
    checks = api_checks_from(
        [{"method": "POST", "path": "/residents", "asserts": ["id"]},
         {"method": "GET", "path": "/residents"}],
        CATALOG,
    )
    source = render_api_tests(checks, suite="Residents")
    assert source.count("{") == source.count("}")
    assert source.count("test(") == 2
    assert source.rstrip().endswith("});")


# --------------------------------------------------------------------------- #
# Database checks
# --------------------------------------------------------------------------- #
def test_a_structured_db_check_becomes_a_query() -> None:
    checks = db_checks_from(
        [{"name": "one row per identifier", "table": "residents",
          "where": "email = :email", "expect_rows": 1, "columns": ["id", "email"]}]
    )
    source = render_db_checks(checks, suite="Residents")
    assert "SELECT id, email FROM residents WHERE email = :email" in source
    assert "expect(rows).toHaveLength(1);" in source


def test_a_db_check_with_no_table_does_not_pretend_to_assert() -> None:
    """An empty test body would report green and prove nothing."""
    source = render_db_checks(db_checks_from(["some prose about the database"]), suite="X")
    assert "test.fixme(true, 'supply the table and predicate for this check');" in source
    assert "queryRows(" not in source.split("test.fixme")[1]


def test_the_db_helper_import_is_a_deliberate_compile_error() -> None:
    source = render_db_checks(db_checks_from([{"table": "residents"}]), suite="X")
    assert "import { queryRows } from '../support/db';" in source


# --------------------------------------------------------------------------- #
def test_spec_file_names_are_kebab_case() -> None:
    assert spec_file_name("Resident Registration", "api") == "resident-registration.api.spec.ts"
    assert spec_file_name("Resident Registration", "db") == "resident-registration.db.spec.ts"
    assert re.match(r"^[a-z0-9-]+\.(api|db)\.spec\.ts$", spec_file_name("A  B!", "api"))
