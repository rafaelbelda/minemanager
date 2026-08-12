"""Engine + session factory. SQLite is a perfect fit for single-tenant, 5–10 nodes."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Base, Node, SchemaMeta

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

    _apply_schema(_engine)
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


# Bump when a new migration is appended below.
SCHEMA_VERSION = 1


def _migrate_v1(conn) -> None:
    """Columns added, and RCON removed, before schema versioning existed.

    ``instances.rcon_host`` was ``NOT NULL`` with no DDL default (SQLAlchemy's
    ``default=`` is applied in Python), so once the ORM stopped sending it every
    INSERT on an existing database failed the constraint — dropping it is what
    keeps older databases working.

    Must stay idempotent: databases that predate ``schema_meta`` already had
    these applied by the old inspect-on-every-boot code, and will run it once
    more to get stamped.
    """
    added = {
        "version": "VARCHAR(64)",
        "build": "VARCHAR(32)",
        "jar_path": "VARCHAR(1024)",
        "java_home": "VARCHAR(1024)",
    }
    existing = {c["name"] for c in inspect(conn).get_columns("instances")}
    for name, ddl in added.items():
        if name not in existing:
            conn.execute(text(f"ALTER TABLE instances ADD COLUMN {name} {ddl}"))
    for name in ("rcon_host", "rcon_port"):
        if name in existing:
            conn.execute(text(f"ALTER TABLE instances DROP COLUMN {name}"))


#: (version, step). Appended to, never edited — an applied migration is history.
_MIGRATIONS = [(1, _migrate_v1)]


def _read_version(engine: Engine) -> int:
    with engine.connect() as conn:
        if not inspect(conn).has_table(SchemaMeta.__tablename__):
            return 0
        row = conn.execute(text("SELECT version FROM schema_meta WHERE id = 1")).scalar()
    return int(row or 0)


def _stamp_version(engine: Engine, version: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM schema_meta"))
        conn.execute(text("INSERT INTO schema_meta (id, version) VALUES (1, :v)"), {"v": version})


def _apply_schema(engine: Engine) -> None:
    """Create missing tables, then run only the migrations this database lacks.

    A database with no ``nodes`` table is brand new: ``create_all`` has just
    produced the current schema, so it is stamped as current rather than being
    walked through migrations that describe changes it never needed.
    """
    fresh = not inspect(engine).has_table("nodes")
    Base.metadata.create_all(engine)

    if fresh:
        _stamp_version(engine, SCHEMA_VERSION)
        log.info("database initialised at schema v%d", SCHEMA_VERSION)
        return

    current = _read_version(engine)
    pending = [(v, step) for v, step in _MIGRATIONS if v > current]
    for version, step in pending:
        with engine.begin() as conn:
            step(conn)
        log.info("applied schema migration v%d", version)
    if current < SCHEMA_VERSION:
        _stamp_version(engine, SCHEMA_VERSION)


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
