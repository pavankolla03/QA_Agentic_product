"""A tiny application under test.

The platform's cost story depends on exploring a *real* application once and
reusing that knowledge. Demonstrating and benchmarking that needs something to
crawl, so this ships a minimal server-rendered app with the shape QA tooling
actually meets: a login page, a dashboard, a list view and a create form with
required fields, validation messages and `data-testid` attributes.

    python -m scripts.demo_app --port 8123

It is dependency-free (stdlib only) so it can start inside a test or a
benchmark without touching the project's requirements.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Seeded accounts. A demo whose login accepts any non-empty password cannot
# exercise a negative path, so every generated "wrong password" scenario fails
# for the same uninteresting reason and the failure analyst has nothing real to
# analyse. These are fixture credentials for a local toy server; the test suite
# reads them from the environment, as it should.
_USERS: dict[str, str] = {
    "std.user": "Passw0rd!",
    "admin.user": "Adm1nPass!",
    "ro.user": "ReadOnly1!",
}

_RESIDENTS: list[dict[str, str]] = [
    {"id": "1", "name": "Ada Lovelace", "email": "ada@example.com", "unit": "A-101"},
    {"id": "2", "name": "Grace Hopper", "email": "grace@example.com", "unit": "B-204"},
]

_LAYOUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title} - Acme Residents</title></head>
<body>
<nav data-testid="main-nav">
  <a href="/dashboard" data-testid="nav-dashboard">Dashboard</a>
  <a href="/residents" data-testid="nav-residents">Residents</a>
  <a href="/reports" data-testid="nav-reports">Reports</a>
  <a href="/logout" data-testid="nav-logout">Log out</a>
</nav>
<main>
<h1 data-testid="{heading_id}">{title}</h1>
{body}
</main>
</body></html>
"""

_LOGIN = """
<form method="post" action="/login" data-testid="login-form">
  <label for="username">Username</label>
  <input id="username" name="username" data-testid="login-username" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" data-testid="login-password" required>
  <button type="submit" data-testid="login-submit">Sign in</button>
</form>
{error}
"""

_NEW_RESIDENT = """
<form method="post" action="/residents/new" data-testid="resident-form">
  <label for="name">Full name</label>
  <input id="name" name="name" data-testid="resident-name" required>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" data-testid="resident-email" required>
  <label for="unit">Unit number</label>
  <input id="unit" name="unit" data-testid="resident-unit" required>
  <label for="type">Resident type</label>
  <select id="type" name="type" data-testid="resident-type">
    <option value="owner">Owner</option>
    <option value="tenant">Tenant</option>
  </select>
  <label for="notes">Notes</label>
  <textarea id="notes" name="notes" data-testid="resident-notes"></textarea>
  <button type="submit" data-testid="resident-submit">Create resident</button>
  <a href="/residents" data-testid="resident-cancel">Cancel</a>
</form>
{error}
"""


def _rows() -> str:
    body = "".join(
        f'<tr data-testid="resident-row-{r["id"]}">'
        f'<td>{r["name"]}</td><td>{r["email"]}</td><td>{r["unit"]}</td>'
        f'<td><a href="/residents/{r["id"]}/edit" data-testid="edit-{r["id"]}">Edit</a></td></tr>'
        for r in _RESIDENTS
    )
    return (
        '<form method="get" action="/residents" data-testid="resident-search-form">'
        '<label for="q">Search residents</label>'
        '<input id="q" name="q" data-testid="resident-search" placeholder="Name or unit">'
        '<button type="submit" data-testid="resident-search-submit">Search</button></form>'
        '<a href="/residents/new" data-testid="resident-new">Add resident</a>'
        f'<table data-testid="resident-table"><thead><tr><th>Name</th><th>Email</th>'
        f"<th>Unit</th><th></th></tr></thead><tbody>{body}</tbody></table>"
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:      # keep benchmark output clean
        return

    # ------------------------------------------------------------------ #
    def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _page(self, title: str, body: str, heading_id: str = "page-heading", status: int = 200) -> None:
        self._send(_LAYOUT.format(title=title, body=body, heading_id=heading_id), status=status)

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802 - stdlib contract
        route = urlparse(self.path).path.rstrip("/") or "/"

        if route in ("/", "/login"):
            self._page("Sign in", _LOGIN.format(error=""), "login-heading")
        elif route == "/dashboard":
            self._page(
                "Dashboard",
                '<p data-testid="welcome">Welcome back.</p>'
                '<a href="/residents" data-testid="go-residents">Manage residents</a>',
                "dashboard-heading",
            )
        elif route == "/residents":
            self._page("Residents", _rows(), "residents-heading")
        elif route == "/residents/new":
            self._page("Add resident", _NEW_RESIDENT.format(error=""), "new-resident-heading")
        elif route == "/reports":
            self._page("Reports", '<p data-testid="reports-empty">No reports yet.</p>', "reports-heading")
        elif route == "/api/residents":
            self._send(json.dumps({"residents": _RESIDENTS}), content_type="application/json")
        elif route == "/health":
            self._send(json.dumps({"status": "ok"}), content_type="application/json")
        elif route.startswith("/residents/") and route.endswith("/edit"):
            self._page("Edit resident", _NEW_RESIDENT.format(error=""), "edit-resident-heading")
        else:
            self._page("Not found", '<p data-testid="not-found">Nothing here.</p>', "notfound-heading", status=404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length", "0") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace")) if length else {}

        def field(name: str) -> str:
            return (form.get(name) or [""])[0].strip()

        if route == "/login":
            username, password = field("username"), field("password")
            if not username or not password:
                missing = "".join(
                    f'<p data-testid="error-{name}">{label} is required</p>'
                    for name, label in (("username", "Username"), ("password", "Password"))
                    if not field(name)
                )
                self._page("Sign in", _LOGIN.format(error=missing), "login-heading", status=400)
            elif _USERS.get(username) == password:
                self._page("Dashboard", '<p data-testid="welcome">Welcome back.</p>', "dashboard-heading")
            else:
                self._page(
                    "Sign in",
                    _LOGIN.format(error='<p data-testid="login-error">Invalid username or password</p>'),
                    "login-heading",
                    status=401,
                )
        elif route == "/residents/new":
            missing = [name for name in ("name", "email", "unit") if not field(name)]
            if missing:
                errors = "".join(f'<p data-testid="error-{name}">{name} is required</p>' for name in missing)
                self._page("Add resident", _NEW_RESIDENT.format(error=errors), "new-resident-heading", status=400)
            elif any(r["email"] == field("email") for r in _RESIDENTS):
                self._page(
                    "Add resident",
                    _NEW_RESIDENT.format(error='<p data-testid="error-duplicate">A resident with that email already exists</p>'),
                    "new-resident-heading",
                    status=409,
                )
            else:
                _RESIDENTS.append(
                    {"id": str(len(_RESIDENTS) + 1), "name": field("name"),
                     "email": field("email"), "unit": field("unit")}
                )
                self._page(
                    "Residents",
                    '<p data-testid="toast">Resident created successfully</p>' + _rows(),
                    "residents-heading",
                )
        else:
            self._page("Not found", "<p>Nothing here.</p>", "notfound-heading", status=404)


class DemoApp:
    """Context-manager wrapper so tests and benchmarks can start it inline."""

    def __init__(self, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> DemoApp:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Minimal application under test for AI QA Engineer")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    with DemoApp(args.port) as app:
        print(f"demo application under test: {app.base_url}")
        print("routes: /login  /dashboard  /residents  /residents/new  /reports  /api/residents")
        print("Ctrl+C to stop.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nstopped")
