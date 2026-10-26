"""Command dispatch: validation, error mapping, and routing to the supervisor.

No tmux involved — the supervisor is stubbed, so this covers the table and the
error contract rather than process behaviour.
"""

from __future__ import annotations

import pytest

from minemanager_agent import handlers
from minemanager_shared.protocol import Action, Command, InstanceSpec


class StubSupervisor:
    """Records what it was asked to do; returns plausible payloads."""

    identity = None
    node_root = None        # no node root configured: isolation is not enforced

    def __init__(self):
        self.calls = []

    async def start(self, spec):
        self.calls.append(("start", spec.id))
        return {"state": "starting"}

    async def stop(self, spec):
        self.calls.append(("stop", spec.id))
        return {"state": "stopped", "graceful": True}

    async def restart(self, spec):
        self.calls.append(("restart", spec.id))
        return {"state": "starting"}

    async def kill(self, spec):
        self.calls.append(("kill", spec.id))
        return {"state": "stopped"}

    async def send(self, spec, line):
        self.calls.append(("send", line))
        return {"sent": True}

    async def states_for(self, ids):
        self.calls.append(("states_for", tuple(ids)))
        return {i: "stopped" for i in ids}


@pytest.fixture
def sup():
    return StubSupervisor()


@pytest.fixture
def spec(instance_root):
    return InstanceSpec(
        id="i1", type="paper", name="survival", root_dir=str(instance_root),
        start_command="java -jar paper.jar",
    )


async def run(sup, action, data):
    return await handlers.handle(Command(action=action, data=data), sup)


class TestPowerRouting:
    @pytest.mark.parametrize("action,expected", [
        (Action.power_start, "start"),
        (Action.power_stop, "stop"),
        (Action.power_restart, "restart"),
        (Action.power_kill, "kill"),
    ])
    async def test_reaches_the_supervisor(self, sup, spec, action, expected):
        resp = await run(sup, action.value, {"instance": spec.model_dump(mode="json")})
        assert resp.ok is True
        assert sup.calls == [(expected, "i1")]


class TestFileRouting:
    async def test_list_returns_entries(self, sup, spec, instance_root):
        (instance_root / "server.properties").write_text("motd=hi")
        resp = await run(sup, Action.files_list.value, {
            "instance": spec.model_dump(mode="json"), "path": ".",
        })
        assert resp.ok is True
        assert [e["name"] for e in resp.data["entries"]] == ["server.properties"]

    async def test_write_then_read(self, sup, spec):
        payload = spec.model_dump(mode="json")
        assert (await run(sup, Action.files_write.value, {
            "instance": payload, "path": "a.txt", "content": "hello",
        })).ok is True
        resp = await run(sup, Action.files_read.value, {"instance": payload, "path": "a.txt"})
        assert resp.data["content"] == "hello"

    async def test_console_line_is_forwarded(self, sup, spec):
        resp = await run(sup, Action.console_send.value, {
            "instance": spec.model_dump(mode="json"), "line": "say hi",
        })
        assert resp.ok is True
        assert sup.calls == [("send", "say hi")]


class TestErrorMapping:
    async def test_missing_file_is_reported_not_raised(self, sup, spec):
        resp = await run(sup, Action.files_read.value, {
            "instance": spec.model_dump(mode="json"), "path": "nope.txt",
        })
        assert resp.ok is False
        assert "not found" in resp.error

    async def test_jail_escape_is_reported_as_rejected(self, sup, spec):
        resp = await run(sup, Action.files_read.value, {
            "instance": spec.model_dump(mode="json"), "path": "../../etc/passwd",
        })
        assert resp.ok is False
        assert "path rejected" in resp.error

    async def test_unsupported_archive_is_reported(self, sup, spec, instance_root):
        (instance_root / "a.rar").write_bytes(b"Rar!\x1a\x07\x00not-really")
        resp = await run(sup, Action.files_extract.value, {
            "instance": spec.model_dump(mode="json"), "path": "a.rar",
        })
        assert resp.ok is False

    async def test_transfer_without_identity_fails_cleanly(self, sup, spec):
        resp = await run(sup, Action.transfer_start.value, {
            "instance": spec.model_dump(mode="json"),
            "tid": "t1", "direction": "download", "path": "world",
        })
        assert resp.ok is False
        assert "identity" in resp.error

    async def test_a_handler_error_never_escapes_as_an_exception(self, sup, spec):
        """Whatever goes wrong, the agent answers the correlated command."""
        resp = await run(sup, Action.files_delete.value, {
            "instance": spec.model_dump(mode="json"), "path": "missing", "recursive": False,
        })
        assert resp.ok is False and resp.error


class TestDefaults:
    async def test_logs_tail_defaults_to_latest_log(self, sup, spec):
        resp = await run(sup, Action.logs_tail.value, {"instance": spec.model_dump(mode="json")})
        assert resp.ok is True
        assert resp.data["path"] == "logs/latest.log"
        assert resp.data["missing"] is True

    async def test_files_list_defaults_to_the_root(self, sup, spec):
        resp = await run(sup, Action.files_list.value, {"instance": spec.model_dump(mode="json")})
        assert resp.ok is True

    async def test_instance_states_defaults_to_no_ids(self, sup):
        resp = await run(sup, Action.instance_states.value, {})
        assert (resp.ok, resp.data) == (True, {"states": {}})


async def test_response_echoes_the_command_id(sup, spec):
    cmd = Command(action=Action.power_start.value, data={"instance": spec.model_dump(mode="json")})
    resp = await handlers.handle(cmd, sup)
    assert resp.id == cmd.id
