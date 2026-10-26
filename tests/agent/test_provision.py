"""Creating a new server on the agent.

No network and no tmux: the download is stubbed at
:func:`minemanager_agent.updater.apply_update`, and a fake supervisor plays the
part of the server, writing the files each platform writes on its first run.
What is under test is the order of the steps and what is left on disk, which is
where this flow can go wrong in ways the operator pays for.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from minemanager_agent import provision
from minemanager_shared.protocol import InstanceSpec, UpdateDownload

DOWNLOAD = UpdateDownload(
    url="https://example.invalid/paper.jar",
    filename="paper-1.21.8-60.jar",
    version="1.21.8",
    build="60",
    checksum="00" * 32,
    checksum_algo="sha256",
)


class FakeSupervisor:
    """Stands in for tmux + the real supervisor.

    ``behaviour`` decides what the simulated server does on its first run:
    ``"eula_gate"`` writes the config and exits by itself (Paper/Vanilla with no
    accepted EULA), ``"proxy"`` writes the config and keeps running (Velocity),
    ``"dies"`` writes nothing, and ``"hangs"`` runs forever writing nothing.
    """

    identity = None

    def __init__(self, node_root: Path, behaviour: str = "eula_gate",
                 writes: tuple[str, ...] = ("server.properties",)):
        self.node_root = node_root
        self.behaviour = behaviour
        self.writes = writes
        self.alive = False
        self.calls: list[str] = []
        self.provisioned: list[str] = []
        # What the real supervisor would have mirrored from the tmux pane.
        self.pane: list[str] = ["Error: LinkageError", "UnsupportedClassVersionError"]
        self.forgotten: list[str] = []

    async def pty_tail(self, instance_id: str) -> list[str]:
        return self.pane

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)

    def session_name(self, instance_id: str) -> str:
        return f"mm-{instance_id}"

    @contextlib.contextmanager
    def provisioning(self, instance_id: str):
        self.provisioned.append(instance_id)
        yield

    async def start(self, spec):
        self.calls.append("start")
        root = Path(spec.root_dir)
        if self.behaviour in ("eula_gate", "proxy"):
            for name in self.writes:
                (root / name).write_text("generated\n", encoding="utf-8")
        # A Minecraft server stops at its own EULA gate; a proxy keeps running.
        self.alive = self.behaviour in ("proxy", "hangs")
        return {"state": "starting", "launch_failed": self.behaviour == "dies"}

    async def stop(self, spec):
        self.calls.append("stop")
        self.alive = False
        return {"state": "stopped"}

    async def kill(self, spec):
        self.calls.append("kill")
        self.alive = False
        return {"state": "stopped"}


@pytest.fixture
def node_root(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def stub_download(monkeypatch):
    """Install a jar without touching the network."""
    async def fake_apply(root, jar_name, download, *, allow_create=False):
        assert allow_create is True, "a new server's jar does not exist yet"
        (Path(root) / jar_name).write_bytes(b"jar")
        return {"ok": True, "jar": jar_name, "size": 3}

    monkeypatch.setattr(provision.updater, "apply_update", fake_apply)


@pytest.fixture(autouse=True)
def stub_tmux(monkeypatch):
    """Session liveness comes from the fake supervisor, not a real tmux."""
    holder = {}

    async def has_session(name):
        return bool(holder.get("sup") and holder["sup"].alive)

    monkeypatch.setattr(provision.tmux, "has_session", has_session)
    monkeypatch.setattr(provision, "_SETTLE_S", 0)
    monkeypatch.setattr(provision, "_POLL_S", 0.01)
    return holder


def spec_for(node_root: Path, name: str = "survival", kind: str = "paper") -> InstanceSpec:
    return InstanceSpec(
        id="i1", type=kind, name=name, root_dir=str(node_root / name),
        start_command=f"java -jar {'paper.jar' if kind == 'paper' else 'server.jar'} nogui",
    )


async def run(sup, stub_tmux, spec, **kw):
    stub_tmux["sup"] = sup
    kw.setdefault("accept_eula", True)
    return await provision.create(sup, spec, "paper.jar", DOWNLOAD, **kw)


class TestSuccessfulCreation:
    async def test_the_jar_lands_in_the_new_directory(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        spec = spec_for(node_root)
        await run(sup, stub_tmux, spec)
        assert (node_root / "survival" / "paper.jar").exists()

    async def test_the_first_run_generates_the_configuration(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        result = await run(sup, stub_tmux, spec_for(node_root))
        assert result["config_files"] == ["server.properties"]
        assert (node_root / "survival" / "server.properties").exists()

    async def test_the_eula_is_written_after_the_configuration_run(self, node_root, stub_tmux):
        """Written last on purpose: the first run stops at the gate instead of
        carrying on into world generation."""
        sup = FakeSupervisor(node_root)
        await run(sup, stub_tmux, spec_for(node_root))
        eula = node_root / "survival" / "eula.txt"
        assert eula.read_text(encoding="utf-8") == "eula=true\n"
        assert sup.calls == ["start", "stop"]

    async def test_it_stops_after_the_first_run_by_default(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        result = await run(sup, stub_tmux, spec_for(node_root))
        assert result["started"] is False
        assert sup.calls.count("start") == 1

    async def test_run_after_starts_it_a_second_time(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        result = await run(sup, stub_tmux, spec_for(node_root), run_after=True)
        assert result["started"] is True
        assert sup.calls == ["start", "stop", "start"]

    async def test_the_monitor_is_held_off_during_creation(self, node_root, stub_tmux):
        """The configuration run ends with the server gone, which the restart
        policy would otherwise read as a crash."""
        sup = FakeSupervisor(node_root)
        await run(sup, stub_tmux, spec_for(node_root))
        assert sup.provisioned == ["i1"]

    async def test_the_installed_version_is_reported(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        result = await run(sup, stub_tmux, spec_for(node_root))
        assert (result["version"], result["build"]) == ("1.21.8", "60")


class TestVelocity:
    """A proxy has no EULA and keeps running once configured."""

    async def test_it_is_stopped_once_its_config_appears(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root, behaviour="proxy", writes=("velocity.toml",))
        spec = spec_for(node_root, name="lobby", kind="velocity")
        result = await run(sup, stub_tmux, spec, accept_eula=False)
        assert result["config_files"] == ["velocity.toml"]
        assert sup.calls == ["start", "stop"]

    async def test_no_eula_file_is_written(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root, behaviour="proxy", writes=("velocity.toml",))
        spec = spec_for(node_root, name="lobby", kind="velocity")
        result = await run(sup, stub_tmux, spec, accept_eula=False)
        assert result["eula_accepted"] is False
        assert not (node_root / "lobby" / "eula.txt").exists()


class TestRefusals:
    async def test_a_minecraft_server_needs_the_eula(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        with pytest.raises(provision.ProvisionError, match="EULA"):
            await run(sup, stub_tmux, spec_for(node_root), accept_eula=False)
        assert not (node_root / "survival").exists()

    async def test_a_node_without_a_root_cannot_provision(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        sup.node_root = None
        with pytest.raises(provision.ProvisionError, match="MM_ROOT_DIR"):
            await run(sup, stub_tmux, spec_for(node_root))

    async def test_a_directory_outside_the_node_root_is_refused(
        self, node_root, tmp_path, stub_tmux
    ):
        sup = FakeSupervisor(node_root)
        spec = spec_for(node_root)
        spec.root_dir = str(tmp_path / "elsewhere")
        with pytest.raises(Exception, match="node root"):
            await run(sup, stub_tmux, spec)
        assert not (tmp_path / "elsewhere").exists()

    async def test_an_occupied_directory_is_left_alone(self, node_root, stub_tmux):
        existing = node_root / "survival"
        existing.mkdir()
        (existing / "world").write_text("precious", encoding="utf-8")

        sup = FakeSupervisor(node_root)
        with pytest.raises(provision.ProvisionError, match="not empty"):
            await run(sup, stub_tmux, spec_for(node_root))
        assert (existing / "world").read_text(encoding="utf-8") == "precious"


class TestFailureCleanup:
    async def test_a_server_that_never_starts_leaves_nothing_behind(
        self, node_root, stub_tmux
    ):
        sup = FakeSupervisor(node_root, behaviour="dies")
        with pytest.raises(provision.ProvisionError, match="exited immediately"):
            await run(sup, stub_tmux, spec_for(node_root))
        assert not (node_root / "survival").exists()
        assert "kill" in sup.calls

    async def test_a_run_that_generates_no_config_fails(self, node_root, stub_tmux, monkeypatch):
        monkeypatch.setattr(provision, "FIRST_RUN_TIMEOUT_S", 0.05)
        sup = FakeSupervisor(node_root, behaviour="hangs")
        with pytest.raises(provision.ProvisionError, match="did not finish"):
            await run(sup, stub_tmux, spec_for(node_root))
        assert not (node_root / "survival").exists()

    async def test_a_failed_download_leaves_nothing_behind(
        self, node_root, stub_tmux, monkeypatch
    ):
        async def boom(*a, **kw):
            raise RuntimeError("checksum mismatch")

        monkeypatch.setattr(provision.updater, "apply_update", boom)
        sup = FakeSupervisor(node_root)
        with pytest.raises(RuntimeError, match="checksum"):
            await run(sup, stub_tmux, spec_for(node_root))
        assert not (node_root / "survival").exists()

    async def test_an_empty_directory_that_already_existed_is_kept(
        self, node_root, stub_tmux, monkeypatch
    ):
        """Only a directory this flow created is removed again."""
        (node_root / "survival").mkdir()

        async def boom(*a, **kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(provision.updater, "apply_update", boom)
        sup = FakeSupervisor(node_root)
        with pytest.raises(RuntimeError):
            await run(sup, stub_tmux, spec_for(node_root))
        assert (node_root / "survival").is_dir()

    async def test_what_was_written_into_a_kept_directory_goes(
        self, node_root, stub_tmux
    ):
        """Otherwise the next attempt meets "exists and is not empty" forever,
        with no declared instance left to clear it through."""
        existing = node_root / "survival"
        existing.mkdir()
        sup = FakeSupervisor(node_root, behaviour="dies")   # fails after the jar lands
        with pytest.raises(provision.ProvisionError):
            await run(sup, stub_tmux, spec_for(node_root))
        assert existing.is_dir()
        assert list(existing.iterdir()) == []

    async def test_a_file_where_the_directory_should_go_says_so(
        self, node_root, stub_tmux
    ):
        (node_root / "survival").write_text("not a directory\n", encoding="utf-8")
        sup = FakeSupervisor(node_root)
        with pytest.raises(provision.ProvisionError, match="not a directory"):
            await run(sup, stub_tmux, spec_for(node_root))

    async def test_a_failure_carries_the_last_terminal_output(
        self, node_root, stub_tmux
    ):
        """The hub deletes the instance, so this is the only account of why."""
        sup = FakeSupervisor(node_root, behaviour="dies")
        with pytest.raises(provision.ProvisionError, match="UnsupportedClassVersion"):
            await run(sup, stub_tmux, spec_for(node_root))

    async def test_a_failure_with_an_empty_pane_is_just_the_reason(
        self, node_root, stub_tmux
    ):
        sup = FakeSupervisor(node_root, behaviour="dies")
        sup.pane = []
        with pytest.raises(provision.ProvisionError) as caught:
            await run(sup, stub_tmux, spec_for(node_root))
        assert "terminal output" not in str(caught.value)

    async def test_a_failed_second_start_still_reports_the_creation(
        self, node_root, stub_tmux
    ):
        """The server exists and is configured; only the optional start failed."""
        sup = FakeSupervisor(node_root)
        calls = {"n": 0}
        real_start = sup.start

        async def flaky(spec):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("no tmux on this node")
            return await real_start(spec)

        sup.start = flaky
        result = await run(sup, stub_tmux, spec_for(node_root), run_after=True)
        assert result["ok"] is True
        assert result["started"] is False
        assert "no tmux" in result["start_error"]
        assert (node_root / "survival" / "paper.jar").exists()


class TestBookkeeping:
    async def test_a_failed_creation_is_forgotten(self, node_root, stub_tmux):
        """The hub un-declares it, so the agent must not keep reporting it."""
        sup = FakeSupervisor(node_root, behaviour="dies")
        with pytest.raises(provision.ProvisionError):
            await run(sup, stub_tmux, spec_for(node_root))
        assert sup.forgotten == ["i1"]

    async def test_a_successful_creation_is_kept(self, node_root, stub_tmux):
        sup = FakeSupervisor(node_root)
        await run(sup, stub_tmux, spec_for(node_root))
        assert sup.forgotten == []
