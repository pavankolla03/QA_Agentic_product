"""A catch-all route is not a page.

The crawler proposes candidate routes from the words in the instruction —
/automation, /form, /valid, /registration — because that is a cheap way to find
pages a link graph misses. An application that answers 200 to everything
accepts all of them.

One demo map held 25 "routes", of which 21 were byte-identical copies of the
login page. A `ResidentsPage` was then bound to whichever of them ranked first,
so every generated method on the resident registration form filled `username`
and `password`. The code compiled, the locators were real, and the Page Object
modelled the wrong page entirely.
"""

from __future__ import annotations

from services.knowledge_service.application_map import ApplicationMap, PageKnowledge

LOGIN_ELEMENTS = [
    {"name": "Username", "role": "textbox", "locator": "getByTestId('login-username')", "confidence": 0.98},
    {"name": "Password", "role": "textbox", "locator": "getByTestId('login-password')", "confidence": 0.98},
]
RESIDENT_ELEMENTS = [
    {"name": "Full name", "role": "textbox", "locator": "getByTestId('resident-name')", "confidence": 0.98},
]


def _map_with_catchall() -> ApplicationMap:
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/login", dom_hash="abc", elements=LOGIN_ELEMENTS))
    for route in ("/automation", "/valid", "/form", "/registration", "/"):
        m.put_page(PageKnowledge(route=route, dom_hash="abc", elements=LOGIN_ELEMENTS))
    m.put_page(PageKnowledge(route="/residents", dom_hash="def", elements=RESIDENT_ELEMENTS))
    return m


def test_identical_dom_collapses_into_one_page() -> None:
    m = _map_with_catchall()
    assert m.known_routes() == ["/login", "/residents"]
    assert set(m.aliases) == {"/automation", "/valid", "/form", "/registration", "/"}
    assert all(target == "/login" for target in m.aliases.values())


def test_the_catalogue_offers_only_real_pages() -> None:
    """This is the property that matters: what code generation can bind to."""
    routes = {entry["page"] for entry in _map_with_catchall().catalog()}
    assert routes == {"/login", "/residents"}


def test_an_alias_still_resolves_to_its_page() -> None:
    """`/` reaching the login page is true, and a lookup for it should work."""
    page = _map_with_catchall().page("/")
    assert page is not None
    assert page.route == "/login"


def test_different_pages_are_never_merged() -> None:
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/login", dom_hash="abc", elements=LOGIN_ELEMENTS))
    m.put_page(PageKnowledge(route="/residents", dom_hash="def", elements=RESIDENT_ELEMENTS))
    assert m.known_routes() == ["/login", "/residents"]
    assert not m.aliases


def test_a_page_with_no_dom_hash_is_never_treated_as_a_duplicate() -> None:
    """An empty hash is an absence of evidence, not evidence of sameness."""
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/a", dom_hash="", elements=LOGIN_ELEMENTS))
    m.put_page(PageKnowledge(route="/b", dom_hash="", elements=LOGIN_ELEMENTS))
    assert m.known_routes() == ["/a", "/b"]
    assert not m.aliases


def test_revisiting_a_real_page_still_updates_it() -> None:
    """Folding duplicates must not break the ordinary re-crawl path."""
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/login", dom_hash="abc", elements=LOGIN_ELEMENTS))
    m.put_page(PageKnowledge(route="/login", dom_hash="abc", elements=LOGIN_ELEMENTS))
    page = m.page("/login")
    assert page is not None
    assert page.visit_count == 2
    assert page.stable_visits == 1, "two identical visits is what makes a page baselineable"
