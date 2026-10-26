"""Instances stay inside their node's root.

``resolve_within`` keeps a file operation inside one instance; this is the outer
half, keeping the instance itself inside the node's directory. The agent is the
only place the check can be trusted, because it is the only place that can
resolve the node's real filesystem, so these cover the agent's copy and the
dispatch gate that applies it to every instance-scoped command.
"""

from __future__ import annotations

import pytest

from minemanager_agent import files, handlers
from minemanager_shared.protocol import Action, Command, InstanceSpec


@pytest.fixture
def node_root(tmp_path):
    root = tmp_path / "srv"
    (root / "survival").mkdir(parents=True)
    return root


class TestResolveInstanceRoot:
    def test_a_directory_under_the_root_is_accepted(self, node_root):
        got = files.resolve_instance_root(node_root, str(node_root / "survival"))
        assert got == (node_root / "survival").resolve()

    def test_a_directory_that_does_not_exist_yet_is_accepted(self, node_root):
        """A new server's directory is created after the check, not before."""
        got = files.resolve_instance_root(node_root, str(node_root / "fresh"))
        assert got == (node_root / "fresh").resolve()

    def test_a_sibling_of_the_root_is_rejected(self, node_root, tmp_path):
        with pytest.raises(files.JailError):
            files.resolve_instance_root(node_root, str(tmp_path / "elsewhere"))

    def test_traversal_out_of_the_root_is_rejected(self, node_root):
        with pytest.raises(files.JailError):
            files.resolve_instance_root(node_root, str(node_root / ".." / "etc"))

    def test_traversal_that_lands_back_inside_is_accepted(self, node_root):
        """`..` is resolved, not banned: what matters is where it ends up."""
        sneaky = str(node_root / "survival" / ".." / "survival")
        assert files.resolve_instance_root(node_root, sneaky) == (node_root / "survival").resolve()

    def test_the_root_itself_is_rejected(self, node_root):
        """An instance rooted there would browse every other server on the node."""
        with pytest.raises(files.JailError):
            files.resolve_instance_root(node_root, str(node_root))

    def test_a_relative_path_is_rejected(self, node_root):
        with pytest.raises(files.JailError):
            files.resolve_instance_root(node_root, "survival")

    def test_an_empty_path_is_rejected(self, node_root):
        with pytest.raises(files.JailError):
            files.resolve_instance_root(node_root, "")

    def test_a_symlink_pointing_out_of_the_root_is_rejected(self, node_root, tmp_path):
        """The reason this check has to run on the node and not on the hub."""
        outside = tmp_path / "outside"
        outside.mkdir()
        link = node_root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this host does not allow creating symlinks")
        with pytest.raises(files.JailError):
            files.resolve_instance_root(node_root, str(link))

    def test_no_configured_root_enforces_nothing(self, tmp_path):
        """Legacy nodes keep working; they simply cannot provision."""
        anywhere = str(tmp_path / "anywhere")
        assert files.resolve_instance_root(None, anywhere) == (tmp_path / "anywhere").resolve()


class StubSupervisor:
    identity = None

    def __init__(self, node_root):
        self.node_root = node_root
        self.started = []

    async def start(self, spec):
        self.started.append(spec.id)
        return {"state": "starting"}


def _spec(root: str) -> InstanceSpec:
    return InstanceSpec(
        id="i1", type="paper", name="survival", root_dir=root,
        start_command="java -jar paper.jar nogui",
    )


class TestDispatchGate:
    """Every instance-scoped command passes the same check, not just file ops."""

    async def test_a_command_outside_the_root_is_refused(self, node_root, tmp_path):
        sup = StubSupervisor(node_root)
        cmd = Command(
            action=Action.power_start.value,
            data={"instance": _spec(str(tmp_path / "elsewhere")).model_dump(mode="json")},
        )
        resp = await handlers.handle(cmd, sup)
        assert resp.ok is False
        assert "path rejected" in resp.error
        assert sup.started == []        # refused before the supervisor saw it

    async def test_a_command_inside_the_root_is_carried_out(self, node_root):
        sup = StubSupervisor(node_root)
        cmd = Command(
            action=Action.power_start.value,
            data={"instance": _spec(str(node_root / "survival")).model_dump(mode="json")},
        )
        resp = await handlers.handle(cmd, sup)
        assert resp.ok is True
        assert sup.started == ["i1"]
