"""Engine + session factory. SQLite is a perfect fit for single-tenant, 5–10 nodes."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Base, Node

log = logging.getLogger("minemanager.hub")

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
        # Wait rather than failing instantly on a concurrent writer
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(_engine)
    _ensure_columns(_engine)
    _ensure_secret_uniqueness(_engine)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)

    with _engine.connect() as conn:
        nodes = conn.execute(select(func.count()).select_from(Node.__table__)).scalar_one()
    log.info("database: %s (%d node(s) declared)", settings.db_path, nodes)
    if nodes == 0:
        log.warning(
            "no nodes in %s - if you expected some, check MM_DATA_DIR: a different "
            "data dir means a different database, and agents will be rejected",
            settings.db_path,
        )


def _ensure_columns(engine: Engine) -> None:
    from sqlalchemy import inspect, text

    wanted = {
        "instances": {
            "version": "VARCHAR(64)",
            "build": "VARCHAR(32)",
            "jar_path": "VARCHAR(1024)",
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


def _ensure_secret_uniqueness(engine: Engine) -> None:
    # Bring existing DBs forward to the (scope, scope_id, key) uniqueness rule.
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table("secrets"):
        return
    if any(ix["name"] == "uq_secret_scope_key" for ix in inspector.get_indexes("secrets")):
        return

    with engine.begin() as conn:
        # SQLite's bare-column rule: `id` is taken from the row supplying MAX().
        removed = conn.execute(
            text(
                "DELETE FROM secrets WHERE id NOT IN ("
                "  SELECT id FROM ("
                "    SELECT id, MAX(updated_at) FROM secrets"
                "    GROUP BY scope, scope_id, key"
                "  )"
                ")"
            )
        ).rowcount
        if removed:
            log.warning(
                "removed %d duplicate secret row(s) while adding the uniqueness "
                "constraint; the most recently updated value of each key was kept",
                removed,
            )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_secret_scope_key "
                "ON secrets (scope, scope_id, key)"
            )
        )


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
