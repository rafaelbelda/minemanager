"""Creating a new server through the hub.

The agent is faked, so what is covered here is the hub's half: the refusals that
must happen before anything is built, the payload the agent is handed, and that
a failed creation leaves no instance declared.

The route returns 202 and builds in a task, so anything about the *outcome* is
asserted after :func:`settle`, and the 202 body is only the declared row.
"""

from __future__ import annotations

import time

import pytest

from minemanager_hub.agents.registry import AgentConnection, registry
from minemanager_hub.api import agent_ws, servers
from minemanager_hub.db.models import Node
from minemanager_hub.db.session import session_scope
from minemanager_hub.providers.base import Download, Provider, ProviderError
from minemanager_shared.protocol import Action, Hello, Response

NODE_ROOT = "/srv/minecraft"


class FakeAgent(AgentConnection):
    """Answers every command in-process, recording what it was sent."""

    def __init__(self, node_id: str, *, ok: bool = True, data: dict | None = None,
                 error: str = "the server never came up"):
        super().__init__(node_id, self._answer)
        self.ok = ok
        self.data = data if data is not None else {"ok": True, "started": False}
        self.error = error
        self.commands: list[dict] = []

    async def _answer(self, frame: dict) -> None:
        self.commands.append(frame)
        self.resolve(Response(
            id=frame["id"], ok=self.ok,
            data=self.data if self.ok else {},
            error=None if self.ok else self.error,
        ))

    def sent(self, action: str) -> list[dict]:
        return [c for c in self.commands if c["action"] == action]


class FakeProvider(Provider):
    software, label, has_builds = "paper", "Paper", True

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.resolved: list[tuple] = []

    async def versions(self):
        return []

    async def builds(self, version):
        return []

    async def resolve(self, version, build):
        if self.fail:
            raise ProviderError("no such version")
        self.resolved.append((version, build))
        return Download(
            url="https://example.invalid/paper.jar", filename="paper.jar",
            version=version, build=build or "60",
            checksum="ab" * 32, checksum_algo="sha256",
        )


def set_root(node_id: str, root: str | None) -> None:
    """Stand in for the agent reporting its root at handshake time."""
    with session_scope() as db:
        db.get(Node, node_id).root_dir = root


@pytest.fixture
def provider(monkeypatch):
    p = FakeProvider()
    monkeypatch.setattr(servers, "get_provider", lambda software: p)
    return p


@pytest.fixture
def agent(client, node_id):
    conn = FakeAgent(node_id)
    registry.register(conn)
    yield conn
    registry.unregister(node_id)


@pytest.fixture
def rooted(client, node_id):
    set_root(node_id, NODE_ROOT)
    return node_id


BODY = {"name": "survival", "type": "paper", "version": "1.21.8",
        "accept_eula": True, "memory": "4G"}


def create(client, node_id, **overrides):
    return client.post(f"/api/nodes/{node_id}/servers", json={**BODY, **overrides})


def settle(timeout_s: float = 5.0, *, require: bool = True) -> None:
    """Wait for the in-flight builds to finish.

    The TestClient runs the app's loop in its own thread for the lifetime of the
    fixture, so sleeping here lets the build task progress.
    """
    deadline = time.monotonic() + timeout_s
    while servers._running and time.monotonic() < deadline:
        time.sleep(0.01)
    if require:
        assert not servers._running, "a build task did not finish"


@pytest.fixture(autouse=True)
def _leave_no_build_running():
    """A build still in flight when the TestClient tears its loop down is
    cancelled mid-write, so every test ends with the hub idle."""
    yield
    settle(require=False)


def created(client, node_id, **overrides) -> dict:
    """Create, wait for the build, and read the instance back."""
    posted = create(client, node_id, **overrides)
    assert posted.status_code == 202, posted.text
    settle()
    iid = posted.json()["id"]
    listed = client.get(f"/api/nodes/{node_id}/instances").json()
    return next((i for i in listed if i["id"] == iid), {})


class TestRefusals:
    def test_an_unknown_node_is_404(self, client):
        assert create(client, "nope").status_code == 404

    def test_a_node_with_no_root_cannot_host_new_servers(self, client, node_id, agent):
        r = create(client, node_id)
        assert r.status_code == 409
        assert "MM_ROOT_DIR" in r.json()["detail"]
        assert client.get(f"/api/nodes/{node_id}/instances").json() == []

    def test_an_offline_agent_is_refused(self, client, rooted):
        r = create(client, rooted)
        assert r.status_code == 409
        assert "offline" in r.json()["detail"]

    @pytest.mark.parametrize("name", ["../escape", "a/b", "", ".", "..", "  "])
    def test_a_name_that_is_not_a_directory_component_is_rejected(
        self, client, rooted, agent, provider, name
    ):
        assert create(client, rooted, name=name).status_code == 422

    def test_a_duplicate_name_is_refused(self, client, rooted, agent, provider):
        assert create(client, rooted).status_code == 202
        settle()
        r = create(client, rooted)
        assert r.status_code == 409
        assert "already has an instance" in r.json()["detail"]

    def test_a_duplicate_is_refused_even_while_the_first_is_still_building(
        self, client, rooted, agent, provider
    ):
        """The uniqueness constraint, not a read of the table: the second
        request arrives before the first has anything on disk."""
        assert create(client, rooted).status_code == 202
        assert create(client, rooted).status_code == 409
        settle()

    def test_an_unresolvable_version_is_a_400(self, client, rooted, agent, monkeypatch):
        monkeypatch.setattr(servers, "get_provider", lambda s: FakeProvider(fail=True))
        r = create(client, rooted)
        assert r.status_code == 400
        assert client.get(f"/api/nodes/{rooted}/instances").json() == []

    @pytest.mark.parametrize("memory", [
        "lots",
        "4.5G",     # -Xmx takes a whole number; the JVM refuses this outright
        "2048",     # no unit means bytes to the JVM, so this is 2 KB of heap
    ])
    def test_a_memory_value_the_jvm_would_refuse_is_rejected(
        self, client, rooted, agent, provider, memory
    ):
        assert create(client, rooted, memory=memory).status_code == 422

    def test_a_minecraft_server_needs_the_eula_before_any_work(
        self, client, rooted, agent, provider
    ):
        """Refused by the hub, not by the agent: by the time the agent sees it,
        a download has been resolved and an instance declared."""
        r = create(client, rooted, accept_eula=False)
        assert r.status_code == 422
        assert agent.sent(Action.instance_create.value) == []
        assert client.get(f"/api/nodes/{rooted}/instances").json() == []

    def test_a_proxy_is_never_asked_about_the_eula(self, client, rooted, agent, provider):
        """Velocity is not Mojang's and holds no world; demanding acceptance
        would be both wrong and a thing to make an operator affirm falsely."""
        r = create(client, rooted, name="lobby", type="velocity", accept_eula=False)
        assert r.status_code == 202
        settle()
        assert agent.sent(Action.instance_create.value)[0]["data"]["accept_eula"] is False


class TestSuccessfulCreation:
    def test_the_instance_is_declared_under_the_node_root(
        self, client, rooted, agent, provider
    ):
        inst = create(client, rooted).json()
        assert inst["root_dir"] == "/srv/minecraft/survival"
        assert inst["name"] == "survival"

    def test_the_start_command_matches_the_platform(self, client, rooted, agent, provider):
        inst = create(client, rooted).json()
        assert inst["start_command"] == "java -Xmx4G -jar paper.jar nogui"
        assert inst["jar_path"] == "paper.jar"

    def test_the_agent_is_told_to_create_it(self, client, rooted, agent, provider):
        create(client, rooted)
        settle()
        sent = agent.sent(Action.instance_create.value)
        assert len(sent) == 1
        data = sent[0]["data"]
        assert data["jar_name"] == "paper.jar"
        assert data["accept_eula"] is True
        assert data["run_after"] is False
        assert data["instance"]["root_dir"] == "/srv/minecraft/survival"
        assert data["download"]["checksum_algo"] == "sha256"

    def test_the_installed_version_is_recorded(self, client, rooted, agent, provider):
        inst = created(client, rooted, version="1.21.8", build="60")
        assert (inst["version"], inst["build"]) == ("1.21.8", "60")
        assert provider.resolved == [("1.21.8", "60")]

    def test_the_declared_row_is_returned_before_anything_is_built(
        self, client, rooted, agent, provider
    ):
        """What the UI gets to show while the build runs."""
        posted = create(client, rooted)
        assert posted.status_code == 202
        body = posted.json()
        assert body["name"] == "survival"
        assert body["version"] is None        # nothing is installed yet
        assert client.get(f"/api/nodes/{rooted}/instances").json()[0]["id"] == body["id"]
        settle()

    def test_a_server_left_stopped_is_not_armed_for_the_next_boot(
        self, client, rooted, agent, provider
    ):
        inst = created(client, rooted, run_after=False)
        assert inst["start_with_node"] is False

    def test_a_server_that_was_started_stays_armed(self, client, rooted, provider, node_id):
        conn = FakeAgent(node_id, data={"ok": True, "started": True})
        registry.register(conn)
        try:
            inst = created(client, node_id, run_after=True)
            assert inst["start_with_node"] is True
        finally:
            registry.unregister(node_id)


class TestAgentFailure:
    def test_a_failed_creation_declares_nothing(self, client, rooted, provider, node_id):
        conn = FakeAgent(node_id, ok=False, error="the first run produced no server.properties")
        registry.register(conn)
        try:
            assert create(client, node_id).status_code == 202
            settle()
            assert client.get(f"/api/nodes/{node_id}/instances").json() == []
        finally:
            registry.unregister(node_id)

    def test_the_failure_is_reported_on_the_node_event_stream(
        self, client, rooted, provider, node_id
    ):
        """The request is long gone, so this is how the reason reaches anyone."""
        conn = FakeAgent(node_id, ok=False, error="exited immediately: boom")
        registry.register(conn)
        seen: list = []
        try:
            with registry._subscription(node_id, seen.append):
                assert create(client, node_id).status_code == 202
                settle()
        finally:
            registry.unregister(node_id)
        done = [e for e in seen if e.action == Action.ev_provision_done.value]
        assert len(done) == 1
        assert done[0].data["ok"] is False
        assert "boom" in done[0].data["error"]

    def test_success_is_reported_too(self, client, rooted, agent, provider):
        seen: list = []
        with registry._subscription(rooted, seen.append):
            assert create(client, rooted).status_code == 202
            settle()
        done = [e for e in seen if e.action == Action.ev_provision_done.value]
        assert len(done) == 1
        assert done[0].data == {"ok": True, "name": "survival", "error": None}


class TestDeclaredRootsAreConfined:
    """"Add existing server" is held to the same boundary."""

    def test_an_instance_outside_the_node_root_is_refused(self, client, rooted, instance_base):
        body = dict(instance_base, root_dir="/etc")
        r = client.post(f"/api/nodes/{rooted}/instances", json=body)
        assert r.status_code == 400
        assert "inside this node's root" in r.json()["detail"]

    def test_moving_an_instance_out_of_the_root_is_refused(
        self, client, rooted, instance_base
    ):
        body = dict(instance_base, root_dir="/srv/minecraft/survival")
        iid = client.post(f"/api/nodes/{rooted}/instances", json=body).json()["id"]
        r = client.patch(f"/api/instances/{iid}", json={"root_dir": "/srv/other"})
        assert r.status_code == 400

    def test_a_node_without_a_root_still_accepts_any_path(
        self, client, node_id, instance_base
    ):
        """Nodes that predate the setting keep working, and are not flagged."""
        body = dict(instance_base, root_dir="/opt/legacy/survival")
        r = client.post(f"/api/nodes/{node_id}/instances", json=body)
        assert r.status_code == 201
        assert r.json()["outside_node_root"] is False


class TestAnInstanceTheRootDoesNotCover:
    """Setting MM_ROOT_DIR on a node that already has servers elsewhere makes
    the agent refuse every command for them. Reported, not left to be met one
    failing button at a time.
    """

    def test_an_instance_inside_the_root_is_not_flagged(
        self, client, rooted, instance_base
    ):
        body = dict(instance_base, root_dir="/srv/minecraft/survival")
        inst = client.post(f"/api/nodes/{rooted}/instances", json=body).json()
        assert inst["outside_node_root"] is False

    def test_an_instance_the_root_does_not_cover_is_flagged(
        self, client, node_id, instance_base
    ):
        # Declared while the node had no root, which is how this arises: the
        # route would refuse the same path once the root is known.
        body = dict(instance_base, root_dir="/opt/legacy/survival")
        iid = client.post(f"/api/nodes/{node_id}/instances", json=body).json()["id"]
        set_root(node_id, NODE_ROOT)
        listed = client.get(f"/api/nodes/{node_id}/instances").json()
        assert [i["id"] for i in listed] == [iid]
        assert listed[0]["outside_node_root"] is True

    def test_the_handshake_names_them(self, client, node_id, instance_base, caplog):
        body = dict(instance_base, root_dir="/opt/legacy/survival")
        client.post(f"/api/nodes/{node_id}/instances", json=body)
        set_root(node_id, NODE_ROOT)
        with caplog.at_level("WARNING", logger="minemanager.hub"):
            agent_ws._warn_about_stranded_instances(node_id)
        assert "survival" in caplog.text and NODE_ROOT in caplog.text

    def test_it_stays_quiet_when_the_root_covers_everything(
        self, client, rooted, instance_base, caplog
    ):
        body = dict(instance_base, root_dir="/srv/minecraft/survival")
        client.post(f"/api/nodes/{rooted}/instances", json=body)
        with caplog.at_level("WARNING", logger="minemanager.hub"):
            agent_ws._warn_about_stranded_instances(rooted)
        assert "refuse" not in caplog.text


class TestHandshakeReportsTheRoot:
    def test_the_agent_root_is_mirrored_onto_the_node(self, client, node_id):
        token = client.post(f"/api/nodes/{node_id}/reenroll").json()["enrollment_token"]
        hello = Hello(
            agent_version="26.10.26", hostname="box-a", enrollment_token=token,
            root_dir="/srv/minecraft", session_id="s1",
        )
        resolved, _, error = agent_ws._authenticate(hello)
        assert error is None and resolved == node_id
        assert client.get("/api/nodes").json()[0]["root_dir"] == "/srv/minecraft"

    def test_an_agent_that_drops_its_root_clears_the_node(self, client, node_id):
        set_root(node_id, "/srv/minecraft")
        token = client.post(f"/api/nodes/{node_id}/reenroll").json()["enrollment_token"]
        hello = Hello(agent_version="26.10.26", hostname="box-a", enrollment_token=token)
        agent_ws._authenticate(hello)
        assert client.get("/api/nodes").json()[0]["root_dir"] is None
        with session_scope() as db:
            assert db.get(Node, node_id).root_dir is None
