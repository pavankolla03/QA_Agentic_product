"""Database validation tools.

Two hard rules, enforced here rather than left to prompt discipline:

1. **The agent never sees a DSN.** A project registers ``database_dsn_ref`` —
   the *name* of an environment variable. The value is read at the boundary.
2. **Read-only by default.** Only a single ``SELECT``/``WITH`` statement is
   permitted; anything that could mutate data is refused unless the project
   explicitly opts into write mode for seeding.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from packages.aiqa_types.enums import ToolCategory
from packages.security.guard import PolicyViolation
from tools.base import Tool, ToolResult

_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|merge|replace|call|do|copy|vacuum)\b",
    re.IGNORECASE,
)
_MULTI_STATEMENT = re.compile(r";\s*\S")
_COMMENT_STRIP = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.DOTALL)


def _strip_comments(sql: str) -> str:
    return _COMMENT_STRIP.sub(" ", sql).strip()


def assert_read_only(sql: str) -> str:
    """Validate that ``sql`` is a single read-only statement."""
    cleaned = _strip_comments(sql)
    if not cleaned:
        raise PolicyViolation("db.empty_query", "Empty SQL statement.", "")
    if _MULTI_STATEMENT.search(cleaned):
        raise PolicyViolation(
            "db.multi_statement",
            "Multiple SQL statements in one call are not permitted.",
            cleaned[:200],
        )
    first = cleaned.lstrip("(").split(None, 1)[0].lower()
    if first not in ("select", "with", "show", "explain", "describe", "desc"):
        raise PolicyViolation(
            "db.read_only",
            f"Only read-only queries are permitted; statement starts with '{first}'.",
            cleaned[:200],
        )
    if _WRITE_KEYWORDS.search(cleaned):
        raise PolicyViolation(
            "db.write_keyword",
            "Query contains a data-modifying keyword and was refused.",
            cleaned[:200],
        )
    return cleaned


def _resolve_dsn(ctx_metadata: dict[str, Any]) -> tuple[str, str]:
    """Return ``(dsn, source_name)`` from the project's env-var reference."""
    ref = ctx_metadata.get("database_dsn_ref") or ""
    if not ref:
        return "", ""
    dsn = os.environ.get(ref, "")
    return dsn, ref


class DbQueryTool(Tool):
    name = "db.query"
    category = ToolCategory.DATABASE
    description = "Run a single read-only SQL query to validate application state."
    schema = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SELECT/WITH statement"},
            "params": {"type": "object", "description": "Bound parameters — always prefer these over string building"},
            "max_rows": {"type": "integer"},
        },
        "required": ["sql"],
    }

    def _run(self, sql: str, params: dict[str, Any] | None = None, max_rows: int = 200, **_: Any) -> ToolResult:
        cleaned = assert_read_only(sql)          # raises PolicyViolation
        dsn, ref = _resolve_dsn(self.ctx.metadata)
        if not dsn:
            return ToolResult.failure(
                "no database configured for this project. Register `database_dsn_ref` "
                f"(currently {ref or 'unset'}) and export that environment variable."
            )

        started = time.perf_counter()
        engine = None
        try:
            connect_args = {"connect_timeout": 10} if dsn.startswith("postgres") else {}
            engine = create_engine(dsn, pool_pre_ping=True, connect_args=connect_args)
            with engine.connect() as conn:
                result = conn.execute(text(cleaned), params or {})
                columns = list(result.keys())
                rows = [dict(zip(columns, row)) for row in result.fetchmany(max_rows)]
        except SQLAlchemyError as exc:
            return ToolResult.failure(f"query failed: {str(exc)[:400]}")
        finally:
            if engine is not None:
                engine.dispose()

        latency = int((time.perf_counter() - started) * 1000)
        return ToolResult.success(
            {"columns": columns, "rows": _jsonable(rows), "row_count": len(rows), "latency_ms": latency},
            row_count=len(rows),
        )


class DbRowExistsTool(Tool):
    """A safe, parameterised existence check — the most common DB assertion in QA."""

    name = "db.row_exists"
    category = ToolCategory.DATABASE
    description = "Assert that a row matching the given column/value pairs exists in a table."
    schema = {
        "type": "object",
        "properties": {
            "table": {"type": "string"},
            "where": {"type": "object", "description": "column -> expected value"},
        },
        "required": ["table", "where"],
    }

    _IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

    def _run(self, table: str, where: dict[str, Any], **_: Any) -> ToolResult:
        if not self._IDENT.match(table):
            raise PolicyViolation("db.identifier", f"Unsafe table identifier: {table}", table)
        if not where:
            return ToolResult.failure("`where` must contain at least one column")
        for column in where:
            if not self._IDENT.match(column):
                raise PolicyViolation("db.identifier", f"Unsafe column identifier: {column}", column)

        clauses = " AND ".join(f"{col} = :{col}" for col in where)
        sql = f"SELECT COUNT(*) AS matches FROM {table} WHERE {clauses}"

        inner = DbQueryTool(self.ctx)
        result = inner._run(sql=sql, params=where, max_rows=1)
        if not result.ok:
            return result
        rows = result.data.get("rows", [])
        count = int(list(rows[0].values())[0]) if rows else 0
        return ToolResult.success(
            {"exists": count > 0, "matches": count, "table": table, "where": where},
            exists=count > 0,
        )


class DbSchemaTool(Tool):
    name = "db.schema"
    category = ToolCategory.DATABASE
    description = "List tables and columns so the Test Design Agent can plan data validation."
    schema = {"type": "object", "properties": {"table": {"type": "string"}}}

    def _run(self, table: str = "", **_: Any) -> ToolResult:
        dsn, ref = _resolve_dsn(self.ctx.metadata)
        if not dsn:
            return ToolResult.failure(f"no database configured (database_dsn_ref={ref or 'unset'})")
        engine = None
        try:
            from sqlalchemy import inspect

            engine = create_engine(dsn, pool_pre_ping=True)
            inspector = inspect(engine)
            if table:
                columns = [
                    {"name": c["name"], "type": str(c["type"]), "nullable": bool(c.get("nullable", True))}
                    for c in inspector.get_columns(table)
                ]
                payload: Any = {"table": table, "columns": columns}
            else:
                payload = {"tables": inspector.get_table_names()[:300]}
        except SQLAlchemyError as exc:
            return ToolResult.failure(f"schema inspection failed: {str(exc)[:300]}")
        finally:
            if engine is not None:
                engine.dispose()
        return ToolResult.success(payload)


def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import datetime
    import decimal
    import uuid

    def convert(value: Any) -> Any:
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, decimal.Decimal):
            return float(value)
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        return value

    return [{k: convert(v) for k, v in row.items()} for row in rows]


DATABASE_TOOLS = [DbQueryTool, DbRowExistsTool, DbSchemaTool]
