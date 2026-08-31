"""The UI's live event socket, and what it leaves behind when it closes.

Every page close, reload and reconnect ends one of these, so the teardown path
runs far more often than the streaming one.
"""

from __future__ import annotations

import pytest

from minemanager_hub.agents.registry import registry
from minemanager_shared.protocol import Action, Event


@pytest.fixture(autouse=True)
def clean_registry():
    registry._subscribers.clear()
    yield
    registry._subscribers.clear()


def _events_url(node_id: str) -> str:
    return f"/api/nodes/{node_id}/events"


class TestDisconnect:
    def test_closing_unsubscribes(self, client, node_id):
        # The subscription lives in a `with` inside the stream generator, so it
        # is only released when that generator is actually closed. Leave it open
        # and every publish keeps feeding a queue nobody reads.
        with client.websocket_connect(_events_url(node_id)):
            assert len(registry._subscribers.get(node_id, ())) == 1

        assert not registry._subscribers.get(node_id)

    def test_repeated_connections_do_not_accumulate(self, client, node_id):
        for _ in range(5):
            with client.websocket_connect(_events_url(node_id)):
                pass

        assert not registry._subscribers.get(node_id)

    def test_closing_after_receiving_also_unsubscribes(self, client, node_id):
        # The other exit: the socket closes while the stream sits idle *between*
        # events rather than before the first one.
        with client.websocket_connect(_events_url(node_id)) as ws:
            registry.publish(node_id, Event(
                action=Action.ev_console_output.value,
                instance_id="i1",
                data={"line": "hello", "source": "log"},
            ))
            assert ws.receive_json()["data"]["line"] == "hello"

        assert not registry._subscribers.get(node_id)


class TestStreaming:
    def test_events_reach_the_client(self, client, node_id):
        with client.websocket_connect(_events_url(node_id)) as ws:
            for i in range(3):
                registry.publish(node_id, Event(
                    action=Action.ev_state_changed.value,
                    instance_id="i1",
                    data={"state": "running", "detail": str(i)},
                ))
            assert [ws.receive_json()["data"]["detail"] for _ in range(3)] == ["0", "1", "2"]

    def test_only_the_subscribed_node_is_forwarded(self, client, node_id):
        other = client.post("/api/nodes", json={"name": "box-b"}).json()["node_id"]
        with client.websocket_connect(_events_url(node_id)) as ws:
            registry.publish(other, Event(action=Action.ev_heartbeat.value, data={"uptime_s": 1}))
            registry.publish(node_id, Event(
                action=Action.ev_state_changed.value, instance_id="i1", data={"state": "running"},
            ))
            assert ws.receive_json()["action"] == Action.ev_state_changed.value
