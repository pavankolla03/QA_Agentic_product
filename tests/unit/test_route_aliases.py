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

from services.knowledge_service.application_map import ApplicationMap, PageKnowledge, dom_hash

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


def test_a_linked_route_outranks_a_guessed_one() -> None:
    """Crawl order is an accident; which route is real is not.

    The crawler tries its guesses in the order the instruction suggested them,
    so `/automation` can easily be recorded before `/dashboard`. Keeping
    whichever arrived first would make the invented route the page and demote
    the one the application actually links to into an alias of it.
    """
    m = ApplicationMap(project_id="p", base_url="http://x")
    # A page that links to /dashboard, so the map knows that route is real.
    m.put_page(
        PageKnowledge(route="/login", dom_hash="abc", elements=LOGIN_ELEMENTS,
                      navigations=["/dashboard", "/residents"])
    )
    # The guess lands first...
    m.put_page(PageKnowledge(route="/automation", dom_hash="xyz", elements=RESIDENT_ELEMENTS))
    # ...and then the route the application itself links to.
    m.put_page(PageKnowledge(route="/dashboard", dom_hash="xyz", elements=RESIDENT_ELEMENTS))

    assert "/dashboard" in m.known_routes(), m.known_routes()
    assert "/automation" not in m.known_routes()
    assert m.aliases["/automation"] == "/dashboard"
    assert m.page("/automation") is not None, "the alias still resolves"
    assert m.page("/dashboard").route == "/dashboard", "and the page knows its own name"


def test_a_named_route_outranks_the_root() -> None:
    """A Page Object whose `path` is `/` says nothing about what it models.

    The root usually redirects, so `/login` is both truer and more stable.
    """
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/", dom_hash="abc", elements=LOGIN_ELEMENTS))
    m.put_page(PageKnowledge(route="/login", dom_hash="abc", elements=LOGIN_ELEMENTS))
    assert m.known_routes() == ["/login"]
    assert m.aliases == {"/": "/login"}


def test_between_two_guesses_the_first_one_keeps_the_page() -> None:
    """There is no evidence to prefer either, so nothing pretends there is.

    "Shorter wins" would make `/form` beat `/login`, which is arbitrary dressed
    up as a rule. First seen is at least the order the run asked for.
    """
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/login", dom_hash="abc", elements=LOGIN_ELEMENTS))
    m.put_page(PageKnowledge(route="/form", dom_hash="abc", elements=LOGIN_ELEMENTS))
    assert m.known_routes() == ["/login"]
    assert m.aliases == {"/form": "/login"}


# --------------------------------------------------------------------------- #
# Aliasing that went too far
# --------------------------------------------------------------------------- #
# Collapsing a catch-all is worth doing; collapsing a real page is worse than
# not collapsing anything, because the lost page is never tested and the run
# still reports success. Both cases below were found by pointing autopilot at a
# six-page demo application and getting four features back.
def test_two_pages_with_nothing_on_them_are_not_the_same_page() -> None:
    """An unknown fingerprint must read as unknown, not as a match.

    /dashboard and /reports both render prose and no controls. Hashing their
    empty element lists gave the same perfectly stable value, so /reports became
    an alias of /dashboard and dropped out of the plan silently.
    """
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/dashboard", title="Dashboard", elements=[], dom_hash=dom_hash([])))
    m.put_page(PageKnowledge(route="/reports", title="Reports", elements=[], dom_hash=dom_hash([])))

    assert dom_hash([]) == ""
    assert m.aliases == {}
    assert sorted(m.known_routes()) == ["/dashboard", "/reports"]


def test_a_create_form_and_an_edit_form_are_two_pages() -> None:
    """Same fields, different page.

    /residents/new and /residents/:id/edit carry an identical field set, so they
    hash identically. They behave differently, and the edit path is exactly the
    one a suite is most likely to be missing.
    """
    fields = [
        {"name": "Full name", "role": "textbox", "locator": "getByTestId('name')", "confidence": 0.98},
        {"name": "Email", "role": "textbox", "locator": "getByTestId('email')", "confidence": 0.98},
    ]
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/residents/new", title="Add resident", dom_hash="same", elements=fields))
    m.put_page(PageKnowledge(route="/residents/:id/edit", title="Edit resident", dom_hash="same", elements=fields))

    assert m.aliases == {}
    assert sorted(m.known_routes()) == ["/residents/:id/edit", "/residents/new"]


def test_a_catch_all_is_still_collapsed_when_the_title_matches() -> None:
    """The original bug must stay fixed.

    Twenty routes serving the same login page share its title as surely as they
    share its DOM, so requiring the title to agree costs this nothing.
    """
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/login", title="Sign in", dom_hash="abc", elements=LOGIN_ELEMENTS))
    for route in ("/automation", "/valid", "/form"):
        m.put_page(PageKnowledge(route=route, title="Sign in", dom_hash="abc", elements=LOGIN_ELEMENTS))

    assert m.known_routes() == ["/login"]
    assert set(m.aliases) == {"/automation", "/valid", "/form"}


def test_a_missing_title_does_not_block_aliasing() -> None:
    """Plenty of pages have no title; "unknown" is not "different"."""
    m = ApplicationMap(project_id="p", base_url="http://x")
    m.put_page(PageKnowledge(route="/login", title="", dom_hash="abc", elements=LOGIN_ELEMENTS))
    m.put_page(PageKnowledge(route="/valid", title="", dom_hash="abc", elements=LOGIN_ELEMENTS))

    assert m.aliases == {"/valid": "/login"}
