"""Bounds on the wire models.

Instance and transfer ids end up in a file name (the agent's pty mirror), a
tmux target and the URL an agent dials back on, so they are restricted to
characters that cannot address anything else. The hello is the one frame the
hub parses before authenticating, and its strings are stored and served to
every UI, so it is bounded.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minemanager_shared.protocol import (
    Hello,
    InstanceSpec,
    InstanceStatesData,
    TransferStartData,
)

SPEC = dict(type="paper", name="s", root_dir="/srv/mc/s", start_command="java -jar paper.jar")


@pytest.mark.parametrize("bad", ["../../etc/x", "a/b", "a b", "", "x" * 65, "mm:1.0", "a\nb"])
def test_unsafe_instance_ids_are_rejected(bad):
    with pytest.raises(ValidationError):
        InstanceSpec(id=bad, **SPEC)


@pytest.mark.parametrize("good", ["i1", "0f" * 16, "abc-DEF_9"])
def test_real_instance_ids_are_accepted(good):
    assert InstanceSpec(id=good, **SPEC).id == good


@pytest.mark.parametrize("bad", ["../../nodes/x/reenroll", "t?x=1", "t#f", "t/u"])
def test_unsafe_transfer_ids_are_rejected(bad):
    with pytest.raises(ValidationError):
        TransferStartData(instance=InstanceSpec(id="i1", **SPEC), tid=bad,
                          direction="download", path="world")


def test_instance_state_ids_are_checked_too():
    with pytest.raises(ValidationError):
        InstanceStatesData(ids=["ok", "../bad"])


@pytest.mark.parametrize("field,value", [
    ("hostname", "h" * 256),
    ("agent_version", "v" * 65),
    ("credential", "c" * 257),
    ("root_dir", "/" + "r" * 4096),
    ("capabilities", ["x"] * 33),
])
def test_hello_fields_are_bounded(field, value):
    frame = {"agent_version": "1", "hostname": "box", field: value}
    with pytest.raises(ValidationError):
        Hello(**frame)


def test_a_normal_hello_still_parses():
    Hello(agent_version="26.10.26", hostname="box", node_id="0f" * 16,
          credential="c" * 43, session_id="0f" * 16, capabilities=["power", "files"],
          root_dir="/srv/minecraft")
