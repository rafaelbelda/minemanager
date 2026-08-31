"""Engine + session factory. SQLite is a perfect fit for single-tenant, 5–10 nodes."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import DateTime, create_engine, event, func, inspect, select, text
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
SCHEMA_VERSION = 3


def _drop_columns(conn, table: str, drop: set[str]) -> None:
    """Remove columns by rebuilding the table.

    ``ALTER TABLE ... DROP COLUMN`` needs SQLite 3.35 (2021), and Ubuntu 20.04
    ships 3.31 — there the statement is a syntax error, so the hub would fail to
    start rather than migrate. Rebuilding from the ORM's own definition works on
    every version, and a migration runs once per database.
    """
    existing = {c["name"] for c in inspect(conn).get_columns(table)}
    if not (drop & existing):
        return
    model = Base.metadata.tables[table]
    keep = [c for c in model.columns if c.name in existing and c.name not in drop]
    names = ", ".join(c.name for c in keep)
    sources = ", ".join(_carry_over(c) for c in keep)
    conn.execute(text(f"ALTER TABLE {table} RENAME TO {table}_old"))
    model.create(conn)
    conn.execute(text(f"INSERT INTO {table} ({names}) SELECT {sources} FROM {table}_old"))
    conn.execute(text(f"DROP TABLE {table}_old"))


def _carry_over(col) -> str:
    """How to read a column out of the old table.

    A rebuild copies into the *current* schema, which may declare NOT NULL where
    the old one did not. Rather than let one stale NULL abort the migration —
    and with it the hub's startup — fill from the model's own default.
    """
    if col.nullable:
        return col.name
    default = getattr(col.default, "arg", None) if col.default is not None else None
    if isinstance(default, bool):
        return f"COALESCE({col.name}, {int(default)})"
    if isinstance(default, (int, float)):
        return f"COALESCE({col.name}, {default})"
    if isinstance(default, str):
        escaped = default.replace("'", "''")
        return f"COALESCE({col.name}, '{escaped}')"
    if isinstance(col.type, DateTime):          # created_at and friends
        return f"COALESCE({col.name}, CURRENT_TIMESTAMP)"
    return col.name


def _migrate_v1(conn) -> None:
    """Columns added, and RCON removed, before schema versioning existed.

    ``instances.rcon_host`` was ``NOT NULL`` with no DDL default (SQLAlchemy's
    ``default=`` is applied in Python), so once the ORM stopped sending it every
    INSERT on an existing database failed the constraint — removing it is what
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
    _drop_columns(conn, "instances", {"rcon_host", "rcon_port"})


def _migrate_v2(conn) -> None:
    """``nodes.agent_session_id`` — which agent process we last shook hands with.

    Nullable, and null on every existing row, which reads as "unknown". The
    first handshake after this migration therefore counts as a new agent process
    and restores that node's declared run state once; every reconnect after it
    is recognised as the same process and does not.
    """
    existing = {c["name"] for c in inspect(conn).get_columns("nodes")}
    if "agent_session_id" not in existing:
        conn.execute(text("ALTER TABLE nodes ADD COLUMN agent_session_id VARCHAR(32)"))


def _migrate_v3(conn) -> None:
    """Name both lifecycle flags for their trigger, and make the restart one a policy.

    ``desired_running`` -> ``start_with_node``, and ``auto_restart`` -> a
    three-valued ``restart_policy``. The old names described a wish; the new
    ones say *when* they apply, which is what an operator (and the next person
    to read ``api/reconcile.py``) needs. ``desired_running`` in particular read
    like a state a controller continuously converges to, which is exactly what
    it must never become.

    Deliberately tolerant about what it finds. This migration was reshaped
    during development, so a database may already carry the intermediate
    ``restart_on_crash`` column; accepting either source costs one lookup and
    removes a foot-gun that would otherwise only fire on someone's dev box.

    ``RENAME COLUMN`` needs SQLite 3.25 (2018) and the oldest target here,
    Ubuntu 20.04, ships 3.31. A rename preserves every row, so an instance stays
    armed across the upgrade — an unarmed one would silently stop coming back
    after a reboot, which is the whole point of the flag.
    """
    columns = {c["name"] for c in inspect(conn).get_columns("instances")}

    if "desired_running" in columns and "start_with_node" not in columns:
        conn.execute(text(
            "ALTER TABLE instances RENAME COLUMN desired_running TO start_with_node"
        ))

    if "restart_policy" in columns:
        return
    # True kept its meaning ("put it back when it dies"); False becomes the
    # explicit "never", so no instance changes behaviour across the upgrade.
    source = next((c for c in ("auto_restart", "restart_on_crash") if c in columns), None)
    conn.execute(text(
        "ALTER TABLE instances ADD COLUMN restart_policy VARCHAR(16) "
        "NOT NULL DEFAULT 'on_crash'"
    ))
    if source is not None:
        conn.execute(text(
            f"UPDATE instances SET restart_policy = "
            f"CASE WHEN {source} THEN 'on_crash' ELSE 'never' END"
        ))
        _drop_columns(conn, "instances", {"auto_restart", "restart_on_crash"})


#: (version, step). Appended to, never edited — an applied migration is history.
_MIGRATIONS = [(1, _migrate_v1), (2, _migrate_v2), (3, _migrate_v3)]


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
        # One transaction per step, so a failure leaves the database as it was.
        # Foreign keys stay enforced: no table references `instances`, and its
        # rows already satisfied the same constraint before the rebuild.
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
