"""Telling an operator's shutdown from a crash.

Both end a tmux session and nothing else, so the only way to distinguish them is
the status the launching shell recorded on its way out. These drive
``Supervisor._check_one`` with tmux stubbed, so they cover the decision rather
than tmux itself.
"""

from __future__ import annotations

import time

import pytest

from minemanager_agent import supervisor as sup_mod
from minemanager_agent.supervisor import ManagedInstance, Supervisor
from minemanager_shared.protocol import Action, InstanceSpec, RestartPolicy, RunState


def _spec(root_dir: str = "/srv/mc", **kw) -> InstanceSpec:
    base = dict(
        id="i1",
        type="paper",
        name="survival",
        root_dir=root_dir,
        start_command="java -jar paper.jar nogui",
        restart_policy=RestartPolicy.on_crash,
    )
    return InstanceSpec(**{**base, **kw})


@pytest.fixture
def sup(tmp_path, monkeypatch):
    """A supervisor whose sessions are always gone and which never launches.

    ``_check_one`` is the unit under test; letting it actually call tmux would
    make these tests about tmux instead.
    """
    events = []

    async def emit(event):
        events.append(event)

    s = Supervisor(emit, pty_dir=tmp_path)
    s.events = events

    async def no_session(_name):
        return False

    async def never_relaunch(_mi):
        s.relaunched = True
        return True

    async def no_pty(_mi):
        return None

    monkeypatch.setattr(sup_mod.tmux, "has_session", no_session)
    monkeypatch.setattr(s, "_launch", never_relaunch)
    monkeypatch.setattr(s, "_replay_pty", no_pty)
    # The drain sleeps for over a second before cancelling the log follower.
    monkeypatch.setattr(s, "_drain_tail", no_pty)
    s.relaunched = False
    # A real directory: the crash path checks the root dir before relaunching,
    # so a fake one would make every "this should relaunch" test pass for the
    # wrong reason.
    s.root = tmp_path / "root"
    s.root.mkdir()
    return s


def _managed(sup: Supervisor, *, ready: bool = True, **spec_kw) -> ManagedInstance:
    mi = sup._managed(_spec(root_dir=str(sup.root), **spec_kw))
    mi.should_be_up = True
    mi.ready = ready
    mi.state = RunState.running
    return mi


def _write_status(sup: Supervisor, mi: ManagedInstance, value: str) -> None:
    sup._status_path(mi).write_text(value, encoding="utf-8")


def _states(sup: Supervisor) -> list[tuple[str, bool]]:
    return [
        (e.data["state"], e.data.get("clean_exit", False))
        for e in sup.events
        if e.action == Action.ev_state_changed.value
    ]


class TestCleanExit:
    async def test_exit_zero_is_an_operator_shutdown(self, sup):
        mi = _managed(sup)
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert _states(sup) == [(RunState.stopped.value, True)]
        assert sup.relaunched is False
        assert mi.should_be_up is False

    async def test_it_is_not_fought_under_the_default_policy(self, sup):
        # The whole point: "stop" at the console must stick, and on_crash is
        # what every instance gets unless someone chooses otherwise.
        mi = _managed(sup, restart_policy=RestartPolicy.on_crash)
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert sup.relaunched is False

    async def test_a_nonzero_status_is_still_a_crash(self, sup):
        mi = _managed(sup)
        _write_status(sup, mi, "1")

        await sup._check_one(mi)

        assert sup.relaunched is True
        assert [s for s, _ in _states(sup)] == [RunState.crashed.value, RunState.starting.value]
        assert all(clean is False for _, clean in _states(sup))

    async def test_a_killed_session_leaves_no_status_and_is_a_crash(self, sup):
        # kill_session gives the shell no chance to record anything, so the file
        # is simply absent — which must not read as a clean exit.
        mi = _managed(sup)
        assert not sup._status_path(mi).exists()

        await sup._check_one(mi)

        assert sup.relaunched is True

    async def test_a_clean_exit_before_readiness_is_a_launch_failure(self, sup):
        # An unaccepted EULA exits 0 without the server ever coming up. Calling
        # that an operator shutdown would hide it behind "stopped"; the launch
        # failure path says why and shows the pane output.
        mi = _managed(sup, ready=False)
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert sup.relaunched is True
        assert mi.launch_failures == 1

    async def test_a_garbled_status_is_a_crash(self, sup):
        mi = _managed(sup)
        _write_status(sup, mi, "not-a-number")

        await sup._check_one(mi)

        assert sup.relaunched is True

    async def test_nothing_happens_during_a_shutdown(self, sup):
        # Every server exits 0 when the agent stops them on its way out. Reading
        # those as operator shutdowns would disarm the whole node and leave it
        # empty after the reboot.
        mi = _managed(sup)
        _write_status(sup, mi, "0")
        sup._shutting_down = True

        await sup._check_one(mi)

        assert sup.events == []
        assert mi.should_be_up is True

    async def test_a_stopped_instance_is_not_reconsidered(self, sup):
        # Already stopped through the UI: the flag is off, so the status file
        # from that run must not be re-read into a second stopped event.
        mi = _managed(sup)
        mi.should_be_up = False
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert sup.events == []


class TestExitStatusFile:
    def test_missing_file_reads_as_unknown(self, sup):
        mi = sup._managed(_spec())
        assert sup._read_exit_status(mi) is None

    def test_a_recorded_status_round_trips(self, sup):
        mi = sup._managed(_spec())
        _write_status(sup, mi, "0")
        assert sup._read_exit_status(mi) == 0
        _write_status(sup, mi, "143")
        assert sup._read_exit_status(mi) == 143

    def test_clearing_removes_a_previous_runs_status(self, sup):
        # Without this a killed session would inherit the last run's clean exit.
        mi = sup._managed(_spec())
        _write_status(sup, mi, "0")
        sup._clear_exit_status(mi)
        assert sup._read_exit_status(mi) is None

    def test_clearing_is_fine_when_there_is_nothing_to_clear(self, sup):
        sup._clear_exit_status(sup._managed(_spec()))

    def test_without_a_pty_dir_there_is_no_status(self, tmp_path):
        async def emit(_event):
            pass

        bare = Supervisor(emit, pty_dir=None)
        mi = bare._managed(_spec())
        assert bare._status_path(mi) is None
        assert bare._read_exit_status(mi) is None


class TestRestartPolicy:
    """What each policy does with an exit the agent did not ask for."""

    async def test_never_leaves_a_crash_down(self, sup):
        mi = _managed(sup, restart_policy=RestartPolicy.never)
        _write_status(sup, mi, "1")

        await sup._check_one(mi)

        assert sup.relaunched is False
        assert mi.should_be_up is False

    async def test_always_overrules_a_console_stop(self, sup):
        mi = _managed(sup, restart_policy=RestartPolicy.always)
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert sup.relaunched is True
        assert mi.should_be_up is True

    async def test_always_does_not_disarm_the_instance(self, sup):
        # No clean_exit is reported, so the hub keeps it armed. Telling the hub
        # to disarm something we are simultaneously restarting would be a
        # contradiction the next reboot would resolve the wrong way.
        mi = _managed(sup, restart_policy=RestartPolicy.always)
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert all(not e.data.get("clean_exit") for e in sup.events)

    async def test_always_says_why_it_ignored_the_stop(self, sup):
        mi = _managed(sup, restart_policy=RestartPolicy.always)
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        details = [e.data.get("detail") or "" for e in sup.events]
        assert any("policy: always" in d for d in details)

    async def test_always_still_hits_the_crash_loop_breaker(self, sup):
        # The safety valve has to survive the policy, or a server that refuses
        # to stay up would be relaunched forever.
        mi = _managed(sup, restart_policy=RestartPolicy.always)
        _write_status(sup, mi, "0")
        mi.crash_times = [time.monotonic()] * (sup_mod._CRASH_LIMIT + 1)

        await sup._check_one(mi)

        assert sup.relaunched is False
        assert mi.should_be_up is False

    async def test_a_deliberate_stop_never_reaches_the_policy(self, sup):
        # Stop, Kill and agent shutdown all clear should_be_up before the
        # monitor runs, which is why "always" cannot make a server unstoppable.
        mi = _managed(sup, restart_policy=RestartPolicy.always)
        mi.should_be_up = False
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert sup.relaunched is False
        assert sup.events == []
