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
        assert sup.events[0].data["supervised"] is False

    async def test_a_nonzero_status_is_still_a_crash(self, sup):
        mi = _managed(sup)
        _write_status(sup, mi, "1")

        await sup._check_one(mi)

        assert sup.relaunched is True
        assert [s for s, _ in _states(sup)] == [RunState.crashed.value, RunState.starting.value]
        assert all(clean is False for _, clean in _states(sup))
        assert all(e.data["supervised"] for e in sup.events)

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
    def test_clearing_removes_a_previous_runs_status(self, sup):
        # Without this a killed session would inherit the last run's clean exit.
        mi = sup._managed(_spec())
        _write_status(sup, mi, "0")
        sup._clear_exit_status(mi)
        assert sup._read_exit_status(mi) is None

class TestRestartPolicy:
    """What each policy does with an exit the agent did not ask for."""

    async def test_never_leaves_a_crash_down(self, sup):
        mi = _managed(sup, restart_policy=RestartPolicy.never)
        _write_status(sup, mi, "1")

        await sup._check_one(mi)

        assert sup.relaunched is False
        assert mi.should_be_up is False
        assert [e.data["supervised"] for e in sup.events] == [False]

    async def test_always_overrules_a_console_stop(self, sup):
        mi = _managed(sup, restart_policy=RestartPolicy.always)
        _write_status(sup, mi, "0")

        await sup._check_one(mi)

        assert sup.relaunched is True
        assert mi.should_be_up is True
        # Nothing crashed, so no "crashed" flash; and no clean_exit, or the hub
        # would disarm what is being restarted.
        assert _states(sup) == [(RunState.starting.value, False)] * 2
        assert any("policy: always" in (e.data.get("detail") or "") for e in sup.events)

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


class TestSupervisedSignal:
    """Every state event says whether the agent is still keeping the instance up.

    It is what lets the UI distinguish "crashed, retrying" from "crashed, and
    nothing further will happen" without parsing the detail prose.
    """

    def _supervised(self, sup) -> list[bool]:
        return [
            e.data["supervised"] for e in sup.events
            if e.action == Action.ev_state_changed.value
        ]

    async def test_false_once_the_crash_loop_breaker_trips(self, sup):
        mi = _managed(sup)
        _write_status(sup, mi, "1")
        mi.crash_times = [time.monotonic()] * (sup_mod._CRASH_LIMIT + 1)

        await sup._check_one(mi)

        assert self._supervised(sup) == [False]

    async def test_false_once_launches_keep_failing(self, sup):
        mi = _managed(sup, ready=False)
        _write_status(sup, mi, "1")
        mi.launch_failures = sup_mod._LAUNCH_FAILURE_LIMIT - 1

        await sup._check_one(mi)

        assert self._supervised(sup) == [False]

    async def test_false_when_the_root_dir_is_unusable(self, sup):
        mi = _managed(sup)
        mi.spec.root_dir = "/nope/does/not/exist"
        _write_status(sup, mi, "1")

        await sup._check_one(mi)

        assert self._supervised(sup) == [False]
