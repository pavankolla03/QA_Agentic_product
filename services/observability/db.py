"""Database engine + session management.

SQLite by default (zero-setup local install), PostgreSQL for shared/enterprise
deployments — the ORM layer is identical for both.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from configs.settings import get_settings

log = logging.getLogger("aiqa.db")

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _engine_kwargs(url: str) -> dict[str, Any]:
    if url.startswith("sqlite"):
        return {
            "connect_args": {"check_same_thread": False, "timeout": 30},
            "pool_pre_ping": True,
        }
    return {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20}


def get_engine(url: str | None = None) -> Engine:
    global _engine
    if _engine is not None and url is None:
        return _engine
    target = url or get_settings().database_url
    engine = create_engine(target, future=True, **_engine_kwargs(target))

    if target.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    if url is None:
        _engine = engine
    return engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on failure."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db(url: str | None = None, drop: bool = False) -> Engine:
    """Create all tables. Safe to call repeatedly."""
    from services.observability.models import Base  # local import avoids a cycle

    engine = get_engine(url)
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    log.info("database initialised at %s", engine.url.render_as_string(hide_password=True))
    return engine


def reset_db_state() -> None:
    """Test hook: forget the cached engine/session factory."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
