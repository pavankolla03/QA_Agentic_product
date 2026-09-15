"""Deterministic rendering of API and database checks.

A UI test proves the button works. It does not prove the record was written
correctly, and it is the slowest, most brittle way to find out. The cheapest
high-value coverage a QA platform can add is to verify the same acceptance
criterion one layer down — so `api_checks` and `db_checks`, which until now were
prose in the plan and nothing else, become real files.

The same rule as page objects governs what may be generated:

* **API tests may only call an endpoint the application revealed.** Endpoints
  come from form actions captured during exploration — the application's own
  markup saying where it posts. An endpoint nobody observed is not tested,
  because a test against a guessed URL is worse than no test: it fails for the
  wrong reason and trains people to ignore it.
* **Database checks are never invented.** The platform has no schema and no
  connection, so it emits the expectations it *does* know as data, against a
  query function the project supplies. What it cannot know is a parameter, not
  a guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agents.code_generation.renderer import camel, pascal, safe_identifier


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
@dataclass
class ApiCheck:
    """One request/response assertion."""

    name: str
    method: str = "GET"
    path: str = "/"
    expect_status: int = 200
    body_fields: list[str] = field(default_factory=list)
    asserts: list[str] = field(default_factory=list)     # JSON paths expected present
    criteria: list[str] = field(default_factory=list)
    negative: bool = False

    @property
    def title(self) -> str:
        return self.name or f"{self.method} {self.path}"


@dataclass
class DbCheck:
    """One assertion about persisted state."""

    name: str
    table: str = ""
    where: str = ""
    expect_rows: int = 1
    columns: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parsing — structured objects preferred, prose accepted
# --------------------------------------------------------------------------- #
_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")
_PATH_RE = re.compile(r"(/[A-Za-z0-9_\-./:{}]*)")
_METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b", re.IGNORECASE)


def api_checks_from(raw: Any, catalog: list[dict[str, Any]]) -> list[ApiCheck]:
    """Validate planned API checks against the observed endpoint catalogue.

    Accepts either structured objects or the free-text form the design agent
    used to emit, so an older plan still yields something. Either way the
    endpoint must exist: an unobserved path is dropped, exactly as an
    unverified locator is.
    """
    known = {(e["method"].upper(), e["path"]): e for e in catalog if e.get("path")}
    by_path: dict[str, dict[str, Any]] = {}
    for endpoint in catalog:
        by_path.setdefault(endpoint.get("path", ""), endpoint)

    out: list[ApiCheck] = []
    for item in raw or []:
        check = _parse_api(item)
        if check is None:
            continue
        entry = known.get((check.method, check.path)) or by_path.get(check.path)
        if entry is None:
            continue                       # never observed -> never called
        check.method = entry["method"]
        check.path = entry["path"]
        if not check.body_fields and check.method in ("POST", "PUT", "PATCH"):
            check.body_fields = list(entry.get("required_fields") or entry.get("fields") or [])
        out.append(check)
    return out


def _status(value: Any, default: int = 200) -> int:
    """A planned status code; 0 means "any success", None means "unstated"."""
    if value is None or value == "":
        return default
    try:
        code = int(value)
    except (TypeError, ValueError):
        return default
    return code if code == 0 or 100 <= code <= 599 else default


def _parse_api(item: Any) -> ApiCheck | None:
    if isinstance(item, dict):
        path = str(item.get("path") or "").strip()
        if not path:
            return None
        return ApiCheck(
            name=str(item.get("name") or "")[:120],
            method=str(item.get("method") or "GET").upper(),
            path=path,
            # `or` would swallow a deliberate 0, which is how a plan says
            # "assert success, I do not know which 2xx".
            expect_status=_status(item.get("expect_status", item.get("status"))),
            body_fields=[str(f) for f in (item.get("body_fields") or item.get("fields") or [])][:12],
            asserts=[str(a) for a in (item.get("asserts") or [])][:8],
            criteria=[str(c) for c in (item.get("criteria") or [])][:4],
            negative=bool(item.get("negative", False)),
        )
    if isinstance(item, str) and item.strip():
        text = item.strip()
        path_match = _PATH_RE.search(text)
        if not path_match:
            return None
        method_match = _METHOD_RE.search(text)
        status_match = _STATUS_RE.search(text)
        return ApiCheck(
            name=text[:120],
            method=(method_match.group(1).upper() if method_match else "GET"),
            path=path_match.group(1),
            expect_status=int(status_match.group(1)) if status_match else 200,
        )
    return None


def db_checks_from(raw: Any) -> list[DbCheck]:
    """Structured database checks; prose becomes a named, unparameterised check."""
    out: list[DbCheck] = []
    for item in raw or []:
        if isinstance(item, dict) and (item.get("table") or item.get("name")):
            out.append(
                DbCheck(
                    name=str(item.get("name") or item.get("table") or "check")[:120],
                    table=str(item.get("table") or ""),
                    where=str(item.get("where") or ""),
                    expect_rows=int(item.get("expect_rows", 1)),
                    columns=[str(c) for c in (item.get("columns") or [])][:12],
                    criteria=[str(c) for c in (item.get("criteria") or [])][:4],
                )
            )
        elif isinstance(item, str) and item.strip():
            out.append(DbCheck(name=item.strip()[:120]))
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_api_tests(checks: list[ApiCheck], *, suite: str, tags: list[str] | None = None) -> str:
    """A Playwright API spec using the built-in `request` fixture.

    No page, no browser: these run in a fraction of the time of the equivalent
    UI test, which is the entire point of generating them.
    """
    tag_text = " ".join(tags or ["@api"])
    lines: list[str] = [
        f"// {suite} - API checks generated by AI QA Engineer.",
        "// Endpoints come from forms observed during exploration; nothing here is guessed.",
        "import { expect, test } from '@playwright/test';",
        "",
        f"test.describe('{_escape(suite)} {tag_text}', () => {{",
    ]

    for check in checks:
        needs_body = check.method in _WRITE_METHODS and bool(check.body_fields)
        lines.append(f"  test('{_escape(check.title)}', async ({{ request }}) => {{")

        if needs_body:
            # The endpoint is real but the values are not: nobody told the
            # platform what a valid resident looks like. Running this as-is
            # would post the literal string "<fullName>" and fail for a reason
            # that has nothing to do with the application, so it is marked
            # unfinished instead. One edit here makes it a real test.
            fields = ", ".join(check.body_fields)
            lines.append(f"    test.fixme(true, 'fill in the request body: {_escape(fields)}');")
            lines.append("")
            lines.append(f"    const payload = {_payload_literal(check)};")
            lines.append(
                f"    const response = await request.{check.method.lower()}"
                f"('{_escape(check.path)}', {{ data: payload }});"
            )
        else:
            lines.append(
                f"    const response = await request.{check.method.lower()}"
                f"('{_escape(check.path)}');"
            )

        if check.expect_status > 0:
            lines.append(f"    expect(response.status()).toBe({check.expect_status});")
        else:
            # The plan did not commit to a status code, and guessing one turns a
            # passing endpoint into a red test. 2xx is what was actually claimed.
            lines.append("    expect(response.ok()).toBeTruthy();")

        if check.asserts and not check.negative:
            lines.append("")
            lines.append("    const body = await response.json();")
            for assertion in check.asserts:
                prop = _json_path(assertion)
                if prop:
                    lines.append(f"    expect(body{prop}).toBeDefined();")
        lines.append("  });")
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    lines.append("});")
    lines.append("")
    return "\n".join(lines)


_WRITE_METHODS = ("POST", "PUT", "PATCH")


def _payload_literal(check: ApiCheck) -> str:
    """The request body, with each field a clearly unfilled placeholder."""
    payload = {safe_identifier(f): f"<{camel(f)}>" for f in check.body_fields}
    return json.dumps(payload, indent=6).replace("\n", "\n    ")


def _json_path(assertion: str) -> str:
    """Turn `id` or `data.id` into a property access, or nothing if unusable."""
    cleaned = re.sub(r"[^A-Za-z0-9_.]", "", str(assertion))
    if not cleaned:
        return ""
    parts = [p for p in cleaned.split(".") if p and re.match(r"^[A-Za-z_]", p)]
    return "".join(f".{p}" for p in parts)


#: Import specifier the generated database spec uses, relative to `tests/api/`.
DB_HELPER_IMPORT = "../support/db"


def render_db_helper() -> str:
    """The stub `queryRows` the database spec imports.

    Generated rather than assumed. A missing module is a compile error for the
    *whole project*, so an unfinished database helper used to stop `tsc` from
    checking anything else — including the page objects and steps, which is
    where the real defects are.

    It throws rather than returning `[]`: a database check that quietly passes
    against no database is precisely the failure this file exists to prevent.
    """
    return """// Database access for generated @database checks.
//
// Replace the body with a real query against your TEST database. It throws
// until you do, on purpose: a check that silently returns no rows would report
// green while proving nothing.
export async function queryRows(sql: string): Promise<Record<string, unknown>[]> {
  throw new Error(
    `AI QA: queryRows is not implemented yet. Point tests/support/db.ts at your ` +
      `test database to enable @database checks. Query was: ${sql}`,
  );
}
"""


def render_db_checks(checks: list[DbCheck], *, suite: str, db_helper_import: str = DB_HELPER_IMPORT) -> str:
    """Database assertions, parameterised by a query function the project owns.

    The platform has no schema and no credentials, so it does not pretend to.
    What it knows — table, predicate, expected row count — is emitted as data;
    the connection is imported from the project's own helper, and its absence is
    a compile error rather than a silent pass.
    """
    lines: list[str] = [
        f"// {suite} - database checks generated by AI QA Engineer.",
        "//",
        "// `queryRows` is YOUR function. The generated stub throws until you point",
        "// it at the test database, so these tests fail loudly rather than passing",
        "// against nothing - but the project still compiles, because one unfinished",
        "// helper should not break the typecheck for the whole suite.",
        "import { expect, test } from '@playwright/test';",
        "",
        f"import {{ queryRows }} from '{db_helper_import}';",
        "",
        f"test.describe('{_escape(suite)} @database', () => {{",
    ]

    for check in checks:
        lines.append(f"  test('{_escape(check.name)}', async () => {{")
        if check.table:
            where = f" WHERE {check.where}" if check.where else ""
            columns = ", ".join(check.columns) if check.columns else "*"
            lines.append(
                f"    const rows = await queryRows(`SELECT {columns} FROM {check.table}{where}`);"
            )
            lines.append(f"    expect(rows).toHaveLength({check.expect_rows});")
            for column in check.columns:
                lines.append(f"    expect(rows[0]).toHaveProperty('{column}');")
        else:
            # The plan described the check in prose and named no table. Say so
            # plainly rather than emitting an assertion that proves nothing.
            lines.append("    // TODO(aiqa): the plan described this check but named no table.")
            lines.append(f"    //   \"{_escape(check.name)}\"")
            lines.append("    test.fixme(true, 'supply the table and predicate for this check');")
        lines.append("  });")
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    lines.append("});")
    lines.append("")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("'", "\\'").replace("`", "\\`")


def spec_file_name(feature_name: str, kind: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(feature_name).lower()).strip("-") or "suite"
    return f"{slug}.{kind}.spec.ts"


__all__ = [
    "DB_HELPER_IMPORT",
    "ApiCheck",
    "DbCheck",
    "api_checks_from",
    "db_checks_from",
    "render_api_tests",
    "render_db_checks",
    "render_db_helper",
    "spec_file_name",
    "pascal",
]
