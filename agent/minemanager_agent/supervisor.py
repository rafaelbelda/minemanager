"""Process supervisor: the agent owns each server's lifecycle.

Each instance runs in its own tmux session. The supervisor starts and stops them,
streams console output by tailing ``logs/latest.log``, watches for vanished
sessions, and restarts crashed instances under a crash-loop limit.

systemd supervises the *agent*; the agent supervises the *servers*.

A failed launch explains itself through the mirrored tmux pane (see
:func:`tmux.pipe_pane`), whose tail is replayed onto the console on a crash —
tailing the log alone cannot show output written before the logger starts.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from minemanager_agent import logtail, tmux, updater
from minemanager_shared.protocol import Action, Event, InstanceSpec, RunState

EmitFn = Callable[[Event], Awaitable[None]]

# Console command that asks each software to shut down gracefully.
_STOP_COMMAND = {"paper": "stop", "vanilla": "stop", "velocity": "end"}

# Verb for a session whose instance type we no longer know. "stop" is wrong only
# for Velocity, and a proxy holds no world, so falling back to the kill path
# costs it nothing.
_DEFAULT_STOP_VERB = "stop"

_CRASH_LIMIT = 5
_CRASH_WINDOW_S = 60.0

# Failures *before* readiness are counted separately and much more tightly. A
# window cannot catch them
_LAUNCH_FAILURE_LIMIT = 3

# A server is RUNNING once it says so. Until then it is STARTING, however long
# tmux has had a session open. Paper/Vanilla print
#   Done (4.115s)! For help, type "help"
# and Velocity prints `Done (0.5s)!`.
_READY_RE = re.compile(r"Done \(\d+[.,]?\d*s\)!")

# Not every server announces readiness in a form we recognise (modded servers,
# forks, a relocated log). Rather than leave those STARTING forever, assume a
# still-alive instance is up after this long. Generous: a large world can take
# minutes to load, and being late here is harmless.
_STARTUP_GRACE_S = 300.0
# Stop budgets, one ordered chain. Keep SHUTDOWN_GRACE_S well under the unit's
# TimeoutStopSec (300s) — that is systemd's SIGKILL deadline and the backstop if
# the agent hangs. KillMode=mixed means systemd signals only the agent, so the
# sequence below is what actually stops worlds.
_GRACEFUL_STOP_S = 45.0
SHUTDOWN_GRACE_S = 120.0

# tmux reports success as soon as the *session* exists, which says nothing about
# whether the command inside it survived. Re-check after this long so an
# instantly-dying JVM is reported as crashed rather than running.
_LAUNCH_SETTLE_S = 1.0

# Grace for the log follower to pick up a stopped server's final lines before it
# is cancelled. Must exceed logtail.follow's poll interval.
_TAIL_DRAIN_S = 1.5

# Raw pane mirror (see tmux.pipe_pane): how much to replay on a crash, and the
# ceiling the file is kept under. It duplicates console output for a healthy
# server, so it must stay bounded.
_PTY_TAIL_LINES = 40
_PTY_MAX_BYTES = 2 * 1024 * 1024
_PTY_KEEP_BYTES = 512 * 1024

# ANSI/pty control noise. The pane is a terminal, so its mirror carries escape
# sequences that logs/latest.log never has. Tab and newline are kept.
_ANSI_RE = re.compile(
    r"\x1b[@-Z\\-_]|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


@dataclass
class ManagedInstance:
    spec: InstanceSpec
    session: str
    state: RunState = RunState.stopped
    desired_running: bool = False
    crash_times: list[float] = field(default_factory=list)
    tail_task: asyncio.Task | None = None
    # A failed launch is noticed twice (settle check, then monitor); this stops
    # the same error printing twice. Reset by _launch, once per run.
    pty_replayed: bool = False
    # Whether the *current* run announced readiness, and when it was launched.
    # Together these decide whether a vanished session was a runtime crash or a
    # launch that never came up. Reset by _launch.
    ready: bool = False
    launched_at: float = 0.0
    # Consecutive launches that died before readiness. Cleared once one gets up.
    launch_failures: int = 0

    @property
    def log_path(self) -> Path:
        return Path(self.spec.root_dir) / "logs" / "latest.log"


def java_home_problem(java_home: str) -> str | None:
    """Why ``java_home`` is unusable, or None if it looks fine.

    Checked before every launch, so an instance pointed at a moved JDK says so
    once instead of crash-looping with "command not found".
    """
    exe = Path(java_home) / "bin" / "java"
    if not exe.is_file():
        return f"java_home is not a JDK directory: no {exe}"
    if not os.access(exe, os.X_OK):
        return f"java_home is unusable: {exe} is not executable"
    return None


def launch_command(spec: InstanceSpec) -> str:
    """The start command as actually run, with the instance's JDK applied.

    Injecting JAVA_HOME/PATH rather than rewriting the ``java`` token leaves the
    operator's command untouched and works for launches that never name an
    interpreter (wrapper scripts, ``@argfile``), since the child inherits the
    environment either way. Only the directory is quoted — ``$PATH`` must stay
    expandable by the shell tmux runs the command with.
    """
    if not spec.java_home:
        return spec.start_command
    home = shlex.quote(spec.java_home)
    return f"JAVA_HOME={home} PATH={shlex.quote(spec.java_home + '/bin')}:$PATH {spec.start_command}"


class Supervisor:
    def __init__(self, emit: EmitFn, pty_dir: Path | None = None):
        self._emit = emit
        self._pty_dir = pty_dir
        self._instances: dict[str, ManagedInstance] = {}
        self._monitor: asyncio.Task | None = None
        self._started_at = time.monotonic()
        self._updating: set[str] = set()  # instance ids with an update in progress
        self._shutting_down = False       # set by shutdown_all; freezes restart policy
        self.identity = None              # set by the connection after handshake
                                          # (node_id, credential, http_base) for transfers

    # -- lifecycle -----------------------------------------------------------
    def session_name(self, instance_id: str) -> str:
        return f"{tmux.SESSION_PREFIX}-{instance_id}"

    def _managed(self, spec: InstanceSpec) -> ManagedInstance:
        mi = self._instances.get(spec.id)
        if mi is None:
            mi = ManagedInstance(spec=spec, session=self.session_name(spec.id))
            self._instances[spec.id] = mi
        else:
            mi.spec = spec  # always use the hub's latest declared spec
        return mi

    async def _set_state(self, mi: ManagedInstance, state: RunState, detail: str | None = None):
        mi.state = state
        await self._emit(
            Event(
                action=Action.ev_state_changed.value,
                instance_id=mi.spec.id,
                data={"state": state.value, "detail": detail},
            )
        )

    async def start(self, spec: InstanceSpec) -> dict:
        mi = self._managed(spec)
        if spec.id in self._updating:
            raise RuntimeError("cannot start: a server update is in progress")
        if await tmux.has_session(mi.session):
            # Already up, usually because the agent reconnected to a server it
            # did not launch. Its readiness line is long gone from a log we were
            # not tailing, but it has demonstrably come up — so treat it as
            # ready, which also routes a later death down the crash path rather
            # than the launch-failure one.
            mi.desired_running = True
            mi.ready = True
            mi.launch_failures = 0
            await self._attach_pty_mirror(mi)  # no-op if one is already piped
            self._ensure_tail(mi)
            if mi.state is not RunState.running:
                await self._set_state(mi, RunState.running, detail="already running; adopted")
            return {"state": mi.state.value, "already_running": True}

        if spec.java_home:
            problem = java_home_problem(spec.java_home)
            if problem is not None:
                await self._set_state(mi, RunState.stopped, detail=problem)
                raise RuntimeError(problem)

        command = launch_command(spec)
        await self._set_state(mi, RunState.starting, detail=f"launching: {command}")
        try:
            alive = await self._launch(mi)
        except tmux.TmuxError as exc:
            await self._set_state(mi, RunState.stopped, detail=f"failed to start: {exc}")
            raise RuntimeError(f"failed to start: {exc}") from exc

        # Set only now: any earlier and the monitor races the settle window below
        # and counts the same launch as a crash twice.
        mi.desired_running = True
        mi.crash_times.clear()
        mi.launch_failures = 0
        if not alive:
            # Not counted here: the monitor counts every run that ends without
            # reaching readiness, and this session is already gone, so it will
            # see this one on its next pass. One counting site, no double-count.
            await self._replay_pty(mi)
            await self._set_state(mi, RunState.crashed, detail="exited immediately after launch")
            return {"state": mi.state.value, "launch_failed": True}
        # Deliberately still STARTING: the session existing says nothing about
        # whether the server came up. _on_ready promotes it.
        return {"state": mi.state.value}

    async def _launch(self, mi: ManagedInstance) -> bool:
        """Create the session, mirror its pane, and report whether the command
        was still alive after the settle window.

        Raises :class:`tmux.TmuxError` only when the session could not be created
        at all; a command that starts and then dies returns ``False``.
        """
        await asyncio.to_thread(self._reset_pty_log, mi)
        mi.pty_replayed = False
        mi.ready = False
        mi.launched_at = time.monotonic()
        path = self._pty_path(mi)
        await tmux.new_session(
            mi.session, mi.spec.root_dir, launch_command(mi.spec),
            mirror_path=None if path is None else str(path),
        )
        self._ensure_tail(mi)
        await asyncio.sleep(_LAUNCH_SETTLE_S)
        return await tmux.has_session(mi.session)

    async def stop(self, spec: InstanceSpec, *, graceful_s: float = _GRACEFUL_STOP_S) -> dict:
        mi = self._managed(spec)
        mi.desired_running = False
        if not await tmux.has_session(mi.session):
            self._cancel_tail(mi)
            await self._set_state(mi, RunState.stopped)
            return {"state": mi.state.value, "already_stopped": True}

        stop_cmd = _STOP_COMMAND.get(spec.type.value, _DEFAULT_STOP_VERB)
        await self._set_state(mi, RunState.stopping, detail=f"sending '{stop_cmd}', waiting for a clean save")
        # Keep tailing until it is actually down: "Saving worlds" / "Closing
        # Server" are written during this wait, and they are what tells the
        # operator the stop was clean.
        graceful = await self._stop_session(mi.session, stop_cmd, graceful_s)
        await self._drain_tail(mi)
        await self._set_state(
            mi, RunState.stopped,
            detail="stopped gracefully" if graceful else "killed after graceful timeout",
        )
        return {"state": mi.state.value, "graceful": graceful}

    async def restart(self, spec: InstanceSpec) -> dict:
        await self.stop(spec)
        return await self.start(spec)

    async def kill(self, spec: InstanceSpec) -> dict:
        mi = self._managed(spec)
        mi.desired_running = False
        self._cancel_tail(mi)
        await tmux.kill_session(mi.session)
        await self._set_state(mi, RunState.stopped, detail="killed")
        return {"state": mi.state.value}

    async def send(self, spec: InstanceSpec, line: str) -> dict:
        mi = self._managed(spec)
        await tmux.send_keys(mi.session, line)
        return {"sent": True}

    # -- version updater -----------------------------------------------------
    async def apply_update(
        self, spec: InstanceSpec, jar_name: str, download: dict, *, allow_create: bool = False
    ) -> dict:
        """Transactionally replace the server jar. Refuses if the instance is
        running or already updating; holds the ``updating`` state (which also
        blocks ``start``) for the duration."""
        mi = self._managed(spec)
        if await tmux.has_session(mi.session):
            raise RuntimeError("instance must be stopped before updating")
        if spec.id in self._updating:
            raise RuntimeError("an update is already in progress")

        self._updating.add(spec.id)
        prev_state = mi.state
        await self._set_state(mi, RunState.updating, detail=f"installing {download.get('version')}")
        try:
            result = await updater.apply_update(
                spec.root_dir, jar_name, download, allow_create=allow_create
            )
            await self._set_state(
                mi, RunState.stopped,
                detail=f"updated to {download.get('version')}"
                + (f" build {download['build']}" if download.get("build") else ""),
            )
            return result
        except Exception as exc:
            await self._set_state(mi, prev_state if prev_state != RunState.updating else RunState.stopped,
                                   detail=f"update failed: {exc}")
            raise
        finally:
            self._updating.discard(spec.id)

    def is_updating(self, instance_id: str) -> bool:
        return instance_id in self._updating

    # -- console tailing -----------------------------------------------------
    def _ensure_tail(self, mi: ManagedInstance) -> None:
        if mi.tail_task and not mi.tail_task.done():
            return
        mi.tail_task = asyncio.create_task(self._tail(mi))

    def _cancel_tail(self, mi: ManagedInstance) -> None:
        """Stop following a log we no longer expect to grow. Called from every
        path that ends a server, so no polling task outlives the instance."""
        if mi.tail_task and not mi.tail_task.done():
            mi.tail_task.cancel()

    async def _drain_tail(self, mi: ManagedInstance) -> None:
        # Let the tail catch the last lines, then stop it.

        await asyncio.sleep(_TAIL_DRAIN_S)
        self._cancel_tail(mi)

    async def _tail(self, mi: ManagedInstance) -> None:
        try:
            async for line in logtail.follow(mi.log_path):
                await self._emit(
                    Event(
                        action=Action.ev_console_output.value,
                        instance_id=mi.spec.id,
                        data={"line": line, "source": "log"},
                    )
                )
                if not mi.ready and _READY_RE.search(line):
                    await self._on_ready(mi, "reported ready")
        except asyncio.CancelledError:
            pass

    async def _on_ready(self, mi: ManagedInstance, detail: str) -> None:
        # Promote a starting instance to running, once per run.
        if mi.ready:
            return
        mi.ready = True
        mi.launch_failures = 0
        await tmux.stop_pipe_pane(mi.session)
        if mi.state is not RunState.running:
            await self._set_state(mi, RunState.running, detail=detail)

    # -- raw pane mirror (the "why" behind a failed launch) ------------------
    def _pty_path(self, mi: ManagedInstance) -> Path | None:
        if self._pty_dir is None:
            return None
        return self._pty_dir / f"{mi.spec.id}.log"

    def _reset_pty_log(self, mi: ManagedInstance) -> None:
        """Begin each run with an empty mirror, so a replay is never the previous
        run's output. Truncates *in place*: the pipe's ``cat`` holds the inode
        open, so replacing the file would leave it writing to an unlinked one."""
        path = self._pty_path(mi)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb"):
                pass
        except OSError:
            pass  # diagnostics are best-effort; never block a launch

    def _trim_pty_log(self, mi: ManagedInstance) -> None:
        """Keep the mirror bounded: it duplicates console output, so left alone
        it grows for as long as the instance runs."""
        path = self._pty_path(mi)
        if path is None:
            return
        try:
            if path.stat().st_size <= _PTY_MAX_BYTES:
                return
            with path.open("rb") as fh:
                fh.seek(-_PTY_KEEP_BYTES, os.SEEK_END)
                keep = fh.read()
            with path.open("wb") as fh:
                fh.write(keep)
        except OSError:
            pass

    def _read_pty_tail(self, mi: ManagedInstance) -> list[str]:
        path = self._pty_path(mi)
        if path is None:
            return []
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - _PTY_KEEP_BYTES))
                raw = fh.read()
        except OSError:
            return []
        text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
        lines = (ln.rstrip("\r") for ln in text.split("\n"))
        return [ln for ln in lines if ln.strip()][-_PTY_TAIL_LINES:]

    async def _attach_pty_mirror(self, mi: ManagedInstance) -> None:
        """Mirror an adopted session. Whatever it printed before we arrived is
        gone, but a later crash still gets explained. Fresh launches mirror from
        the start — see :func:`tmux.new_session`."""
        path = self._pty_path(mi)
        if path is not None:
            await tmux.pipe_pane(mi.session, str(path))

    async def _replay_pty(self, mi: ManagedInstance) -> None:
        """Push the tail of the raw pane onto the console: the output that never
        reached ``logs/latest.log``. Emitted before the crashed state so the
        reason reads above the verdict."""
        if mi.pty_replayed:
            return                  # already shown for this run
        lines = await asyncio.to_thread(self._read_pty_tail, mi)
        if not lines:
            return
        mi.pty_replayed = True
        for line in ("--- last terminal output before exit ---", *lines):
            await self._emit(
                Event(
                    action=Action.ev_console_output.value,
                    instance_id=mi.spec.id,
                    data={"line": line, "source": "pty"},
                )
            )

    # -- monitor / restart policy -------------------------------------------
    async def run_monitor(self, interval_s: float = 3.0) -> None:
        """Background loop: detect vanished sessions and apply restart policy."""
        while True:
            await asyncio.sleep(interval_s)
            for mi in list(self._instances.values()):
                await self._check_one(mi)

    async def _check_one(self, mi: ManagedInstance) -> None:
        if self._shutting_down:
            return  # a shutdown is in progress: sessions vanishing is expected
        if mi.spec.id in self._updating:
            return  # never treat a mid-update instance as crashed
        alive = await tmux.has_session(mi.session)
        if alive:
            if not mi.ready:
                await self._check_startup_grace(mi)
            await asyncio.to_thread(self._trim_pty_log, mi)
            return
        if not mi.desired_running:
            return

        # Gone while it was supposed to be running. Replay the raw pane first:
        # for a death before the logger is up, that is the only record.
        await self._replay_pty(mi)
        launched = mi.ready   # did *this* run ever come up?
        now = time.monotonic()

        if launched:
            mi.crash_times = [t for t in mi.crash_times if now - t < _CRASH_WINDOW_S]
            mi.crash_times.append(now)
        else:
            mi.launch_failures += 1

        if not mi.spec.auto_restart:
            mi.desired_running = False
            await self._set_state(mi, RunState.crashed, detail="exited (auto-restart off)")
            return

        if launched and len(mi.crash_times) > _CRASH_LIMIT:
            mi.desired_running = False
            await self._set_state(
                mi, RunState.crashed, detail=f"crash-loop: >{_CRASH_LIMIT} in {_CRASH_WINDOW_S:.0f}s"
            )
            return

        if not launched and mi.launch_failures >= _LAUNCH_FAILURE_LIMIT:
            mi.desired_running = False
            await self._set_state(
                mi, RunState.crashed,
                detail=f"failed to start {mi.launch_failures}x in a row without coming up — "
                       f"not retrying. See the console output above for why.",
            )
            return

        if mi.spec.java_home:
            problem = java_home_problem(mi.spec.java_home)
            if problem is not None:
                mi.desired_running = False
                await self._set_state(mi, RunState.crashed, detail=problem)
                return

        await self._set_state(
            mi, RunState.crashed,
            detail="crashed; restarting" if launched else
                   f"exited before coming up (attempt {mi.launch_failures}"
                   f"/{_LAUNCH_FAILURE_LIMIT}); retrying",
        )
        try:
            relaunched = await self._launch(mi)
        except tmux.TmuxError as exc:
            await self._set_state(mi, RunState.crashed, detail=f"restart failed: {exc}")
            return
        if not relaunched:
            await self._replay_pty(mi)   # counted on the next pass, as above
            await self._set_state(mi, RunState.crashed, detail="exited immediately after restart")
            return
        await self._set_state(mi, RunState.starting, detail="relaunched; waiting for it to come up")

    async def _check_startup_grace(self, mi: ManagedInstance) -> None:
        """Assume a still-alive instance is up once the grace period expires.

        Covers software whose readiness line we do not recognise; without this
        such an instance would sit in STARTING for its whole life.
        """
        if mi.launched_at and time.monotonic() - mi.launched_at >= _STARTUP_GRACE_S:
            await self._on_ready(
                mi, f"no readiness line in {_STARTUP_GRACE_S:.0f}s; assuming it is up"
            )

    # -- graceful shutdown (agent stop / restart / system reboot) ------------
    async def shutdown_all(self, graceful_s: float = SHUTDOWN_GRACE_S) -> int:
        """Stop every live managed server cleanly, then tear down our tmux server.

        Covers all ``<SESSION_PREFIX>-*`` sessions, including any this agent did
        not start itself. Sessions are stopped concurrently, so ``graceful_s`` is
        the budget for the whole operation, not per server. Returns how many.
        """
        # Freeze the restart policy *before* anything stops. The monitor is still
        # running, and a clean exit is indistinguishable from a crash — without
        # this it relaunches servers mid-shutdown and kill_server() then tears the
        # fresh ones down ungracefully.
        self._shutting_down = True
        for mi in self._instances.values():
            mi.desired_running = False
            self._cancel_tail(mi)

        names = await tmux.list_sessions()
        await asyncio.gather(
            *(self._stop_session(n, self._verb_for_session(n), graceful_s) for n in names),
            return_exceptions=True,
        )
        await tmux.kill_server()   # leave no tmux server behind
        return len(names)

    def _verb_for_session(self, name: str) -> str:
        """Console stop verb for a session, by its instance type when we know it."""
        for mi in self._instances.values():
            if mi.session == name:
                return _STOP_COMMAND.get(mi.spec.type.value, _DEFAULT_STOP_VERB)
        return _DEFAULT_STOP_VERB

    async def _stop_session(self, name: str, verb: str, graceful_s: float) -> bool:
        """Type ``verb`` at the console, wait for the session to end, else kill it.

        The single stop primitive — both per-instance ``stop`` and the shutdown
        path go through here, so there is one wait loop and one kill-on-timeout
        rule. Returns True if the server exited on its own.
        """
        try:
            await tmux.send_keys(name, verb)
        except tmux.TmuxError:
            pass          # session already gone, or tmux unavailable — fall through
        deadline = time.monotonic() + graceful_s
        while time.monotonic() < deadline:
            if not await tmux.has_session(name):
                return True
            await asyncio.sleep(0.5)
        await tmux.kill_session(name)   # last resort so shutdown is never blocked
        return False

    async def states_for(self, ids: list[str]) -> dict[str, str]:
        """Real run-state for a set of instances, checking tmux for anything not
        actively tracked, so a freshly-connected agent answers truthfully.

        Queried concurrently: one ``has-session`` per instance, run serially,
        made a page load wait on N subprocess spawns.
        """
        transient = (RunState.starting, RunState.stopping, RunState.crashed)

        async def one(iid: str) -> str:
            if iid in self._updating:
                return RunState.updating.value
            mi = self._instances.get(iid)
            if mi is not None and mi.state in transient:
                return mi.state.value               # trust tracked transient states
            if not await tmux.has_session(self.session_name(iid)):
                return RunState.stopped.value
            # A live session we are not tracking: it predates this agent, so we
            # have no readiness signal for it and "up" is the honest answer.
            return RunState.running.value

        results = await asyncio.gather(*(one(iid) for iid in ids))
        return dict(zip(ids, results))

    # -- introspection -------------------------------------------------------
    def states(self) -> dict[str, RunState]:
        return {iid: mi.state for iid, mi in self._instances.items()}

    def uptime_s(self) -> float:
        return time.monotonic() - self._started_at
