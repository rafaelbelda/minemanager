"""Engine + session factory. SQLite is a perfect fit for single-tenant, 5–10 nodes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _init() -> None:
    global _engine, _SessionFactory
    if _engine is not None:
        return
    settings = get_settings()
    _engine = create_engine(
        settings.db_url,
        connect_args={"check_same_thread": False},
        future=True,
    )

    # Enforce foreign keys + WAL for concurrent readers on SQLite.
    @event.listens_for(_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    Base.metadata.create_all(_engine)
    _ensure_columns(_engine)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _ensure_columns(engine: Engine) -> None:
    """Add columns introduced after a DB was first created.

    We have no migration framework (single-tenant, tiny SQLite DB), and
    ``create_all`` never alters existing tables — so bring older DBs forward with
    idempotent ``ALTER TABLE`` for the handful of columns added over time.
    """
    from sqlalchemy import inspect, text

    wanted = {
        "instances": {
            "version": "VARCHAR(64)",
            "build": "VARCHAR(32)",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    """Create tables if needed. Idempotent."""
    _init()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context: commit on success, rollback on error."""
    _init()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
