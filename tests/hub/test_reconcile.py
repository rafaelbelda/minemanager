"""Connect-time restore of declared run state.

The behaviour under test is the one that makes a rebooted node come back on its
own: when an agent (re)connects, every instance the operator left marked
``start_with_node`` is started again, and nothing else is touched.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from minemanager_hub.agents.registry import AgentConnection, registry
from minemanager_hub.api import agent_ws, reconcile
from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Instance, Node
from minemanager_hub.db.session import session_scope
from minemanager_shared.protocol import (
    Action,
    Event,
    Hello,
    Response,
    RunState,
    StateChangedData,
)


class FakeAgent(AgentConnection):
    """A connection that answers every command itself, in-process.

    Subclasses the real :class:`AgentConnection` rather than stubbing ``call``,
    so the correlation-id round trip the restore depends on is exercised too.
    """

    def __init__(self, node_id: str, *, ok: bool = True, data: dict | None = None):
        super().__init__(node_id, self._answer)
        self.ok = ok
        self.data = data or {}
        self.commands: list[dict] = []
        # Runs after each answer. The hook lives here rather than being patched
        # onto the instance because the base class captures the send callable at
        # construction, so a later reassignment would never be called.
        self.after = None

    async def _answer(self, frame: dict) -> None:
        self.commands.append(frame)
        self.resolve(
            Response(
                id=frame["id"],
                ok=self.ok,
                data=self.data,
                error=None if self.ok else "no such directory",
            )
        )
        if self.after is not None:
            await self.after(frame)

    @property
    def started(self) -> list[str]:
        return [
            c["instance_id"] for c in self.commands
            if c["action"] == Action.power_start.value
        ]


@pytest.fixture
def agent(client, node_id, monkeypatch):
    """A connected fake agent for ``node_id``, with the stagger taken out."""
    monkeypatch.setattr(get_settings(), "autostart_stagger_s", 0)
    conn = FakeAgent(node_id)
    registry.register(conn)
    yield conn
    registry.unregister(node_id)


def _arm(client, instance_id: str, armed: bool = True) -> None:
    r = client.patch(f"/api/instances/{instance_id}", json={"start_with_node": armed})
    assert r.status_code == 200, r.text


def _make(client, node_id: str, instance_base: dict, name: str) -> str:
    body = dict(instance_base, name=name)
    return client.post(f"/api/nodes/{node_id}/instances", json=body).json()["id"]


class TestRestore:
    async def test_starts_only_what_is_armed(self, client, node_id, instance_base, agent):
        armed = _make(client, node_id, instance_base, "survival")
        _make(client, node_id, instance_base, "creative")   # left disarmed
        _arm(client, armed)

        await reconcile.restore(node_id, agent)

        assert agent.started == [armed]

    async def test_nothing_armed_sends_no_commands(self, client, node_id, instance_id, agent):
        await reconcile.restore(node_id, agent)
        assert agent.commands == []

    async def test_restore_order_is_deterministic_and_by_name(
        self, client, node_id, instance_base, agent
    ):
        # Declared out of alphabetical order, so this cannot pass by accident on
        # a platform whose clock happens to separate the three creations.
        ids = {n: _make(client, node_id, instance_base, n)
               for n in ("proxy", "lobby", "smp")}
        for iid in ids.values():
            _arm(client, iid)

        await reconcile.restore(node_id, agent)

        assert agent.started == [ids["lobby"], ids["proxy"], ids["smp"]]

    async def test_command_carries_the_declared_spec(
        self, client, node_id, instance_base, instance_id, agent
    ):
        client.patch(
            f"/api/instances/{instance_id}",
            json={"java_home": "/opt/jdk-21", "start_with_node": True},
        )

        await reconcile.restore(node_id, agent)

        spec = agent.commands[0]["data"]["instance"]
        assert spec["java_home"] == "/opt/jdk-21"
        assert spec["start_command"] == instance_base["start_command"]

    async def test_another_nodes_instances_are_untouched(
        self, client, node_id, instance_base, agent
    ):
        other = client.post("/api/nodes", json={"name": "box-b"}).json()["node_id"]
        _arm(client, _make(client, other, instance_base, "elsewhere"))

        await reconcile.restore(node_id, agent)

        assert agent.commands == []

    async def test_disabling_autostart_stops_all_of_it(
        self, client, node_id, instance_id, agent, monkeypatch
    ):
        _arm(client, instance_id)
        monkeypatch.setattr(get_settings(), "autostart", False)

        await reconcile.restore(node_id, agent)

        assert agent.commands == []


class TestRestoreIsSafe:
    """The restore runs unsupervised on a live socket: it must not misfire."""

    async def test_a_refused_start_leaves_the_flag_armed(
        self, client, node_id, instance_id, monkeypatch
    ):
        # The declared state is still what the operator asked for, and the agent
        # has already explained itself on the event stream. Disarming here would
        # quietly give up on a node whose disk came back a minute late.
        monkeypatch.setattr(get_settings(), "autostart_stagger_s", 0)
        _arm(client, instance_id)
        conn = FakeAgent(node_id, ok=False)
        registry.register(conn)
        try:
            await reconcile.restore(node_id, conn)
        finally:
            registry.unregister(node_id)

        assert client.get(f"/api/nodes/{node_id}/instances").json()[0]["start_with_node"]

    async def test_a_dead_agent_does_not_raise(
        self, client, node_id, instance_id, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "autostart_stagger_s", 0)
        _arm(client, instance_id)

        class Dead(FakeAgent):
            async def _answer(self, frame: dict) -> None:
                raise ConnectionError("agent disconnected")

        conn = Dead(node_id)
        registry.register(conn)
        try:
            await reconcile.restore(node_id, conn)   # must simply return
        finally:
            registry.unregister(node_id)

    async def test_abandoned_when_a_reconnect_takes_the_node(
        self, client, node_id, instance_base, monkeypatch
    ):
        # A replacement socket runs its own restore. Continuing on the displaced
        # one would send every remaining start twice.
        monkeypatch.setattr(get_settings(), "autostart_stagger_s", 0)
        for name in ("a", "b", "c"):
            _arm(client, _make(client, node_id, instance_base, name))

        old = FakeAgent(node_id)
        registry.register(old)

        async def get_replaced(_frame: dict) -> None:
            registry.register(FakeAgent(node_id))    # newest wins

        old.after = get_replaced
        try:
            await reconcile.restore(node_id, old)
        finally:
            registry.unregister(node_id)

        assert len(old.started) == 1

    async def test_disarmed_mid_restore_is_skipped(
        self, client, node_id, instance_base, monkeypatch
    ):
        # The flag is re-read per instance, so a Stop pressed while the restore
        # is still working through a slow node is honoured rather than raced.
        monkeypatch.setattr(get_settings(), "autostart_stagger_s", 0)
        first = _make(client, node_id, instance_base, "first")
        second = _make(client, node_id, instance_base, "second")
        _arm(client, first)
        _arm(client, second)

        conn = FakeAgent(node_id)
        registry.register(conn)

        async def disarm_the_rest(_frame: dict) -> None:
            with session_scope() as db:
                db.get(Instance, second).start_with_node = False

        conn.after = disarm_the_rest
        try:
            await reconcile.restore(node_id, conn)
        finally:
            registry.unregister(node_id)

        assert conn.started == [first]

    async def test_starts_are_staggered_not_simultaneous(
        self, client, node_id, instance_base, monkeypatch
    ):
        # Ten JVMs opening worlds at once will make a small box thrash.
        slept: list[float] = []

        async def record(delay: float) -> None:
            slept.append(delay)

        monkeypatch.setattr(get_settings(), "autostart_stagger_s", 7)
        monkeypatch.setattr(asyncio, "sleep", record)
        for name in ("a", "b", "c"):
            _arm(client, _make(client, node_id, instance_base, name))

        conn = FakeAgent(node_id)
        registry.register(conn)
        try:
            await reconcile.restore(node_id, conn)
        finally:
            registry.unregister(node_id)

        # Between the three starts, never before the first one.
        assert slept == [7, 7]


class TestPowerRecordsIntent:
    """What each power op declares, since that is what a restore replays."""

    @pytest.mark.parametrize(
        "op,expected",
        [("start", True), ("restart", True), ("stop", False), ("kill", False)],
    )
    async def test_power_op_sets_the_flag(
        self, client, node_id, instance_id, agent, op, expected
    ):
        client.post(f"/api/instances/{instance_id}/power/{op}")
        row = client.get(f"/api/nodes/{node_id}/instances").json()[0]
        assert row["start_with_node"] is expected

    async def test_kill_is_not_undone_by_the_next_connect(
        self, client, node_id, instance_id, agent
    ):
        # Kill means "stay down". Before this, only start/stop wrote the flag,
        # so a killed server was resurrected by the next handshake.
        client.post(f"/api/instances/{instance_id}/power/start")
        client.post(f"/api/instances/{instance_id}/power/kill")
        agent.commands.clear()

        await reconcile.restore(node_id, agent)

        assert agent.commands == []

    async def test_a_rejected_power_op_declares_nothing(self, client, instance_id):
        # No agent registered: the call 409s, and the DB must not be left
        # claiming the instance should be running.
        assert client.post(f"/api/instances/{instance_id}/power/start").status_code == 409
        with session_scope() as db:
            assert db.get(Instance, instance_id).start_with_node is False


class TestArmingViaPatch:
    def test_flag_is_editable(self, client, instance_id):
        on = client.patch(f"/api/instances/{instance_id}", json={"start_with_node": True})
        assert on.json()["start_with_node"] is True
        off = client.patch(f"/api/instances/{instance_id}", json={"start_with_node": False})
        assert off.json()["start_with_node"] is False

    def test_explicit_null_is_rejected(self, client, instance_id):
        r = client.patch(f"/api/instances/{instance_id}", json={"start_with_node": None})
        assert r.status_code == 422

    def test_omitting_it_leaves_it_alone(self, client, instance_id):
        client.patch(f"/api/instances/{instance_id}", json={"start_with_node": True})
        r = client.patch(f"/api/instances/{instance_id}", json={"name": "renamed"})
        assert r.json()["start_with_node"] is True


class TestOnlyOnAgentRestart:
    """A reconnect is not a reboot.

    While an agent process is alive it supervises its own servers, so redialling
    after a dropped link — or after a *hub* restart, which makes every agent
    redial at once — must not re-enact anything. Otherwise a server stopped
    outside the UI comes back the next time the network hiccups.
    """

    async def test_a_live_agents_reconnect_restores_nothing(
        self, client, node_id, instance_id, agent
    ):
        _arm(client, instance_id)

        await reconcile.restore(node_id, agent, agent_restarted=False)

        assert agent.commands == []

    async def test_an_agent_restart_does_restore(
        self, client, node_id, instance_id, agent
    ):
        _arm(client, instance_id)

        await reconcile.restore(node_id, agent, agent_restarted=True)

        assert agent.started == [instance_id]


class TestClaimAgentSession:
    """The signal the gate is built on: has this agent *process* changed?"""

    def test_first_sight_of_an_agent_counts_as_a_restart(self, client, node_id):
        assert agent_ws._claim_agent_session(node_id, "sess-1") is True

    def test_the_same_process_redialling_does_not(self, client, node_id):
        agent_ws._claim_agent_session(node_id, "sess-1")
        assert agent_ws._claim_agent_session(node_id, "sess-1") is False
        assert agent_ws._claim_agent_session(node_id, "sess-1") is False

    def test_a_new_process_does(self, client, node_id):
        agent_ws._claim_agent_session(node_id, "sess-1")
        assert agent_ws._claim_agent_session(node_id, "sess-2") is True

    def test_it_survives_a_hub_restart(self, client, node_id):
        # The id is a column, not process memory. Restarting the hub makes every
        # agent redial, and that must not look like every node having rebooted.
        agent_ws._claim_agent_session(node_id, "sess-1")
        with session_scope() as db:
            assert db.get(Node, node_id).agent_session_id == "sess-1"
        assert agent_ws._claim_agent_session(node_id, "sess-1") is False

    def test_an_agent_too_old_to_send_one_still_restores(self, client, node_id):
        # Restoring twice is recoverable; never restoring leaves a rebooted node
        # down, so an unknown process counts as a new one.
        assert agent_ws._claim_agent_session(node_id, None) is True
        assert agent_ws._claim_agent_session(node_id, None) is True

    def test_a_node_deleted_mid_handshake_restores_nothing(self, client, node_id):
        client.delete(f"/api/nodes/{node_id}")
        assert agent_ws._claim_agent_session(node_id, "sess-1") is False


def _receive_within(ws, seconds: float = 5.0) -> dict:
    """``ws.receive_json()`` with a deadline.

    A regression in the gate does not produce a wrong frame, it produces *no*
    frame — so the plain call would block for ever and hang CI instead of
    failing. The reader is a daemon thread: if it never returns, nothing has to
    join it for the interpreter to exit.
    """
    received: dict = {}
    reader = threading.Thread(
        target=lambda: received.update(frame=ws.receive_json()), daemon=True
    )
    reader.start()
    reader.join(seconds)
    if "frame" not in received:
        pytest.fail(f"hub sent nothing within {seconds:.0f}s")
    return received["frame"]


class TestHandshakeEndToEnd:
    """Through the real WebSocket endpoint, so the wiring is covered too.

    The gate is one argument threaded from the handshake into the restore, and a
    signature that does not line up is invisible to every test that calls
    ``restore`` directly.
    """

    def _hello(self, **kw) -> dict:
        return Hello(agent_version="test", hostname="box-a", **kw).model_dump(mode="json")

    def test_a_fresh_agent_gets_its_servers_started(self, client, instance_base):
        created = client.post("/api/nodes", json={"name": "box-a"}).json()
        node, token = created["node_id"], created["enrollment_token"]
        armed = _make(client, node, instance_base, "survival")
        _arm(client, armed)

        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(self._hello(enrollment_token=token, session_id="sess-1"))
            assert _receive_within(ws)["ok"] is True

            command = _receive_within(ws)
            assert command["action"] == Action.power_start.value
            assert command["instance_id"] == armed

    def test_the_same_process_redialling_is_recognised(self, client, instance_base):
        created = client.post("/api/nodes", json={"name": "box-a"}).json()
        node, token = created["node_id"], created["enrollment_token"]
        _arm(client, _make(client, node, instance_base, "survival"))

        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(self._hello(enrollment_token=token, session_id="sess-1"))
            credential = _receive_within(ws)["credential"]
            _receive_within(ws)                    # the restore's power.start

        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(
                self._hello(node_id=node, credential=credential, session_id="sess-1")
            )
            assert _receive_within(ws)["ok"] is True

        # The handshake stored the id, so the hub now knows this process and a
        # further redial restores nothing.
        assert agent_ws._claim_agent_session(node, "sess-1") is False


class TestConsoleShutdownDisarms:
    """`stop` typed at a console must mean what the UI's Stop button means.

    The agent reports the observation; the decision to change declared state is
    the hub's. Without this the server would come back at the next reboot.
    """

    def _exit_event(self, instance_id: str, *, clean: bool) -> Event:
        return Event(
            action=Action.ev_state_changed.value,
            instance_id=instance_id,
            data=StateChangedData(
                state=RunState.stopped,
                detail="shut down from the console" if clean else "crashed",
                clean_exit=clean,
            ).model_dump(mode="json"),
        )

    def test_a_clean_exit_disarms_the_instance(self, client, node_id, instance_id):
        _arm(client, instance_id)

        reconcile.note_clean_exit(self._exit_event(instance_id, clean=True))

        row = client.get(f"/api/nodes/{node_id}/instances").json()[0]
        assert row["start_with_node"] is False

    def test_a_crash_does_not(self, client, node_id, instance_id):
        # A server that died must still come back with the node; that is the
        # whole point of the flag.
        _arm(client, instance_id)

        reconcile.note_clean_exit(self._exit_event(instance_id, clean=False))

        row = client.get(f"/api/nodes/{node_id}/instances").json()[0]
        assert row["start_with_node"] is True

    def test_other_events_are_ignored(self, client, node_id, instance_id):
        _arm(client, instance_id)

        reconcile.note_clean_exit(
            Event(action=Action.ev_heartbeat.value, data={"clean_exit": True})
        )
        reconcile.note_clean_exit(
            Event(action=Action.ev_console_output.value, instance_id=instance_id,
                  data={"line": "stop", "clean_exit": True})
        )

        row = client.get(f"/api/nodes/{node_id}/instances").json()[0]
        assert row["start_with_node"] is True

    def test_an_event_without_an_instance_is_ignored(self, client):
        reconcile.note_clean_exit(
            Event(action=Action.ev_state_changed.value, data={"clean_exit": True})
        )

    def test_an_unknown_instance_is_ignored(self, client):
        reconcile.note_clean_exit(self._exit_event("nope", clean=True))

    async def test_a_disarmed_instance_then_stays_down_across_a_restart(
        self, client, node_id, instance_id, agent
    ):
        # End to end for the behaviour asked for: console stop, then the node
        # reboots, and the server is still down.
        _arm(client, instance_id)
        reconcile.note_clean_exit(self._exit_event(instance_id, clean=True))

        await reconcile.restore(node_id, agent, agent_restarted=True)

        assert agent.commands == []
