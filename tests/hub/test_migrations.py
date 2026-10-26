"""Upgrading an existing database.

A migration runs against hubs that already hold someone's declared network, and
a bad one fails at startup on a box that was working a minute earlier. These
build a database at the *old* shape and walk it forward, rather than trusting
that ``create_all`` on a fresh database proves anything about an upgrade.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text


def _v1_database(path) -> str:
    """A database at schema v1: nodes without ``agent_session_id``, one row.

    The DDL is written out rather than generated from the ORM on purpose — the
    point is to reproduce what is actually on disk in an older install, which
    the current models no longer describe.
    """
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE nodes (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                hostname VARCHAR(255),
                credential_hash VARCHAR(128),
                enroll_token_hash VARCHAR(128),
                enroll_expires_at FLOAT,
                agent_version VARCHAR(32),
                last_seen DATETIME,
                created_at DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE instances (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                node_id VARCHAR(32) NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                name VARCHAR(128) NOT NULL,
                type VARCHAR(16) NOT NULL,
                root_dir VARCHAR(1024) NOT NULL,
                start_command TEXT NOT NULL,
                jar_path VARCHAR(1024),
                java_home VARCHAR(1024),
                desired_running BOOLEAN,
                auto_restart BOOLEAN,
                version VARCHAR(64),
                build VARCHAR(32),
                created_at DATETIME
            )
        """))
        conn.execute(text("CREATE TABLE schema_meta (id INTEGER NOT NULL PRIMARY KEY, version INTEGER NOT NULL)"))
        conn.execute(text("INSERT INTO schema_meta (id, version) VALUES (1, 1)"))
        conn.execute(text(
            "INSERT INTO nodes (id, name, hostname, agent_version) "
            "VALUES ('n1', 'box-a', 'box-a.lan', '0.1.0')"
        ))
        conn.execute(text(
            "INSERT INTO instances (id, node_id, name, type, root_dir, start_command, "
            "desired_running, auto_restart) "
            "VALUES ('i1', 'n1', 'survival', 'paper', '/srv/mc', 'java -jar p.jar', 1, 1)"
        ))
    engine.dispose()
    return url


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    """A v1 database on disk, walked forward by the hub's own startup path."""
    from minemanager_hub.db import session as db_session

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _v1_database(data_dir / "minemanager.db")

    monkeypatch.setenv("MM_DATA_DIR", str(data_dir))
    from minemanager_hub import config
    config._settings = None
    db_session._engine = None
    db_session._SessionFactory = None

    db_session.init_db()
    yield db_session
    config._settings = None
    db_session._engine = None
    db_session._SessionFactory = None


class TestUpgradeFromV1:
    def test_the_new_column_is_added(self, upgraded):
        columns = {c["name"] for c in inspect(upgraded._engine).get_columns("nodes")}
        assert "agent_session_id" in columns

    def test_the_node_root_column_is_added(self, upgraded):
        columns = {c["name"] for c in inspect(upgraded._engine).get_columns("nodes")}
        assert "root_dir" in columns

    def test_the_node_root_starts_null_until_the_agent_reports_one(self, upgraded):
        # Null means "no root configured", which leaves an upgraded node working
        # exactly as before until its agent says otherwise.
        with upgraded._engine.connect() as conn:
            value = conn.execute(text("SELECT root_dir FROM nodes WHERE id='n1'")).scalar()
        assert value is None

    def test_it_starts_null_so_the_first_handshake_restores(self, upgraded):
        # Null reads as "we have never seen this agent process", so an existing
        # node restores once on the first connect after the upgrade and not on
        # the reconnects after it.
        with upgraded._engine.connect() as conn:
            value = conn.execute(text("SELECT agent_session_id FROM nodes WHERE id='n1'")).scalar()
        assert value is None

    def test_existing_rows_survive(self, upgraded):
        from minemanager_hub.db.models import Instance, Node

        with upgraded.session_scope() as db:
            node = db.get(Node, "n1")
            assert (node.name, node.hostname) == ("box-a", "box-a.lan")
            inst = db.get(Instance, "i1")
            assert inst.name == "survival"
            assert inst.start_with_node is True     # declared state is preserved

    def test_the_version_is_stamped(self, upgraded):
        with upgraded._engine.connect() as conn:
            version = conn.execute(text("SELECT version FROM schema_meta WHERE id=1")).scalar()
        assert version == upgraded.SCHEMA_VERSION

    def test_running_it_again_is_a_no_op(self, upgraded):
        # Startup applies only the migrations a database lacks; a second call
        # must not try to add the column twice.
        upgraded.init_db()
        columns = {c["name"] for c in inspect(upgraded._engine).get_columns("nodes")}
        assert "agent_session_id" in columns


class TestFlagReshapeV3:
    """The v1 fixture still declares the old names, so this exercises the change."""

    def test_the_old_columns_are_gone(self, upgraded):
        columns = {c["name"] for c in inspect(upgraded._engine).get_columns("instances")}
        assert {"start_with_node", "restart_policy"} <= columns
        assert not {"desired_running", "auto_restart", "restart_on_crash"} & columns

    def test_an_armed_instance_stays_armed(self, upgraded):
        # A rename, not a rebuild, so an instance that was armed before the
        # upgrade is still armed after it — otherwise upgrading the hub would
        # quietly stop every server coming back on the next reboot.
        with upgraded._engine.connect() as conn:
            value = conn.execute(text(
                "SELECT start_with_node FROM instances WHERE id='i1'"
            )).scalar()
        assert value == 1

    def test_the_boolean_becomes_the_equivalent_policy(self, upgraded):
        # auto_restart=1 meant "put it back when it dies", which is on_crash.
        # Nobody's restart behaviour may change just because they upgraded.
        with upgraded._engine.connect() as conn:
            value = conn.execute(text(
                "SELECT restart_policy FROM instances WHERE id='i1'"
            )).scalar()
        assert value == "on_crash"

    def test_auto_restart_off_becomes_never(self, tmp_path, monkeypatch):
        from minemanager_hub.db import session as db_session

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _v1_database(data_dir / "minemanager.db")
        with create_engine(f"sqlite:///{data_dir / 'minemanager.db'}").begin() as conn:
            conn.execute(text("UPDATE instances SET auto_restart = 0 WHERE id='i1'"))

        monkeypatch.setenv("MM_DATA_DIR", str(data_dir))
        from minemanager_hub import config
        config._settings = None
        db_session._engine = None
        db_session._SessionFactory = None
        try:
            db_session.init_db()
            with db_session._engine.connect() as conn:
                value = conn.execute(text(
                    "SELECT restart_policy FROM instances WHERE id='i1'"
                )).scalar()
            assert value == "never"
        finally:
            config._settings = None
            db_session._engine = None
            db_session._SessionFactory = None

    def test_running_it_again_is_a_no_op(self, upgraded):
        upgraded.init_db()
        columns = {c["name"] for c in inspect(upgraded._engine).get_columns("instances")}
        assert "start_with_node" in columns


def test_an_intermediate_shape_is_also_accepted(tmp_path, monkeypatch):
    """A database left mid-reshape by a development build still upgrades.

    v3 was reworked while it was unreleased, so a dev box can be carrying the
    intermediate ``restart_on_crash`` column. Accepting either source is the
    difference between an upgrade and a hub that will not start.
    """
    from minemanager_hub.db import session as db_session

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "minemanager.db"
    _v1_database(db)
    with create_engine(f"sqlite:///{db}").begin() as conn:
        conn.execute(text("ALTER TABLE instances RENAME COLUMN desired_running TO start_with_node"))
        conn.execute(text("ALTER TABLE instances RENAME COLUMN auto_restart TO restart_on_crash"))
        conn.execute(text("UPDATE instances SET restart_on_crash = 0 WHERE id='i1'"))

    monkeypatch.setenv("MM_DATA_DIR", str(data_dir))
    from minemanager_hub import config
    config._settings = None
    db_session._engine = None
    db_session._SessionFactory = None
    try:
        db_session.init_db()
        columns = {c["name"] for c in inspect(db_session._engine).get_columns("instances")}
        assert "restart_policy" in columns and "restart_on_crash" not in columns
        with db_session._engine.connect() as conn:
            row = conn.execute(text(
                "SELECT start_with_node, restart_policy FROM instances WHERE id='i1'"
            )).one()
        assert tuple(row) == (1, "never")
    finally:
        config._settings = None
        db_session._engine = None
        db_session._SessionFactory = None
