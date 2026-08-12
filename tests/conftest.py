"""Shared fixtures.

Two things need care. The hub memoises its settings *and* its SQLAlchemy engine
in module-level globals, so without an explicit reset each test inherits the
previous test's database. And ``minemanager_hub.main`` reads settings at import
time to build the middleware stack, so anything the guards depend on has to be
in the environment before the first import — hence the module-level setdefault
below rather than a fixture.
"""

from __future__ import annotations

import os
import shutil

import pytest

# Must precede any minemanager_hub import: HostGuard/OriginGuard bake these in
# when main.py is imported, and TestClient sends Host: testserver.
os.environ.setdefault("MM_ALLOWED_HOSTS", "testserver")
os.environ.setdefault("MM_ALLOW_API_CLIENTS", "1")
os.environ.setdefault("MM_DATA_DIR", "")   # replaced per-test by hub_env


def _reset_hub_singletons() -> None:
    from minemanager_hub import config
    from minemanager_hub.db import session

    config._settings = None
    session._engine = None
    session._SessionFactory = None


@pytest.fixture
def hub_env(tmp_path, monkeypatch):
    """A hub with its own data dir and a fresh database."""
    monkeypatch.setenv("MM_DATA_DIR", str(tmp_path / "data"))
    _reset_hub_singletons()
    yield tmp_path
    _reset_hub_singletons()


@pytest.fixture
def client(hub_env):
    """TestClient against the real app, on an isolated database."""
    from fastapi.testclient import TestClient

    from minemanager_hub.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def node_id(client) -> str:
    return client.post("/api/nodes", json={"name": "box-a"}).json()["node_id"]


_INSTANCE_BASE = {
    "name": "survival",
    "type": "paper",
    "root_dir": "/srv/mc/survival",
    "start_command": "java -jar paper.jar nogui",
}


@pytest.fixture
def instance_base() -> dict:
    """The declared spec tests start from. A copy, so a test may mutate it."""
    return dict(_INSTANCE_BASE)


@pytest.fixture
def instance_id(client, node_id, instance_base) -> str:
    return client.post(f"/api/nodes/{node_id}/instances", json=instance_base).json()["id"]


@pytest.fixture
def instance_root(tmp_path):
    """A real directory to use as an instance root for file-op tests."""
    root = tmp_path / "instance"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)


def pytest_collection_modifyitems(config, items):
    """Skip tmux/POSIX-only tests off Linux, so a bare `pytest` works anywhere."""
    if shutil.which("tmux") and os.name == "posix":
        return
    skip = pytest.mark.skip(reason="needs a POSIX host with tmux")
    for item in items:
        if "linux" in item.keywords:
            item.add_marker(skip)
