"""The hub/agent payload contract, in both directions.

The models exist so a renamed field fails loudly instead of surfacing as a
``KeyError`` in a toast. These pin that it actually happens — and that unknown
fields stay *ignored*, since a newer hub adding one must not break an older
agent.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import minemanager_shared.protocol as P
from minemanager_shared.protocol import (
    Action,
    Command,
    FilesRenameData,
    InstanceSpec,
    PowerData,
    TransferStartData,
)

SPEC = InstanceSpec(
    id="i1", type="paper", name="survival", root_dir="/srv/mc",
    start_command="java -jar paper.jar",
)


class FakeSupervisor:
    """Enough surface for the handlers under test; no tmux involved."""

    identity = None

    async def start(self, spec):
        return {"state": "starting"}


async def dispatch(action: str, data: dict):
    from minemanager_agent import handlers

    return await handlers.handle(Command(action=action, data=data), FakeSupervisor())


class TestAgentValidatesIncoming:
    async def test_renamed_field_gives_a_clear_error(self):
        resp = await dispatch(Action.files_rename.value, {
            "instance": SPEC.model_dump(mode="json"), "path": "a.txt", "newName": "b.txt",
        })
        assert resp.ok is False
        assert "files.rename" in resp.error
        assert "new_name" in resp.error
        assert "KeyError" not in resp.error

    async def test_missing_instance_spec_is_a_validation_error(self):
        resp = await dispatch(Action.files_list.value, {"path": "."})
        assert resp.ok is False
        assert "instance" in resp.error

    async def test_unknown_action_is_reported(self):
        resp = await dispatch("does.not.exist", {})
        assert resp.ok is False
        assert "unknown action" in resp.error

    async def test_unknown_fields_are_ignored(self):
        """Forward compatibility: a newer hub may add fields."""
        resp = await dispatch(Action.power_start.value, {
            "instance": SPEC.model_dump(mode="json"), "field_from_the_future": 123,
        })
        assert resp.ok is True


class TestHubConstructsOutgoing:
    def test_bad_keyword_is_rejected_at_the_call_site(self):
        with pytest.raises((ValidationError, TypeError)):
            FilesRenameData(instance=SPEC, path="a", newName="b")

    def test_missing_field_is_rejected(self):
        with pytest.raises(ValidationError):
            FilesRenameData(instance=SPEC, path="a")

    def test_valid_payload_builds(self):
        assert FilesRenameData(instance=SPEC, path="a", new_name="b").new_name == "b"

    def test_transfer_direction_is_a_closed_set(self):
        with pytest.raises(ValidationError):
            TransferStartData(instance=SPEC, tid="t", direction="sideways", path="p")

    @pytest.mark.parametrize("direction", ["download", "upload"])
    def test_valid_directions_accepted(self, direction):
        assert TransferStartData(
            instance=SPEC, tid="t", direction=direction, path="p"
        ).direction == direction


class TestWireRoundTrip:
    def test_payload_survives_json_encoding(self):
        wire = PowerData(instance=SPEC).model_dump(mode="json")
        assert isinstance(wire["instance"], dict)
        assert PowerData.model_validate(wire).instance.id == "i1"

    def test_enum_fields_serialise_to_their_value(self):
        assert PowerData(instance=SPEC).model_dump(mode="json")["instance"]["type"] == "paper"


def test_every_action_the_agent_handles_has_a_model():
    from minemanager_agent.handlers import _HANDLERS

    commands = {a.value for a in Action if not a.name.startswith("ev_")}
    assert set(_HANDLERS) == commands, "handler table and Action enum have drifted"


def test_no_payload_model_is_unused():
    """The original defect: models defined, documented as validated, never imported."""
    models = {
        name for name, obj in vars(P).items()
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj.__module__ == P.__name__
    }
    repo = Path(__file__).resolve().parents[2]
    sources = [
        p.read_text(encoding="utf-8")
        for pkg in ("hub", "agent")
        for p in (repo / pkg).rglob("*.py")
    ]
    blob = "\n".join(sources)
    unused = [m for m in models if not re.search(rf"\b{m}\b", blob)]
    assert not unused, f"payload models defined but never used: {sorted(unused)}"
