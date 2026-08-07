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

# Crash-loop policy: if an instance crashes more than N times within WINDOW
# seconds, stop auto-restarting it and surface a crashed state.
_CRASH_LIMIT = 5
_CRASH_WINDOW_S = 60.0
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
            # did not launch. Adopt it and report the real state.
            mi.desired_running = True
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
        if not alive:
            await self._replay_pty(mi)
            await self._set_state(mi, RunState.crashed, detail="exited immediately after launch")
            return {"state": mi.state.value, "launch_failed": True}
        await self._set_state(mi, RunState.running, detail=f"tmux session {mi.session} up")
        return {"state": mi.state.value}

    async def _launch(self, mi: ManagedInstance) -> bool:
        """Create the session, mirror its pane, and report whether the command
        was still alive after the settle window.

        Raises :class:`tmux.TmuxError` only when the session could not be created
        at all; a command that starts and then dies returns ``False``.
        """
        await asyncio.to_thread(self._reset_pty_log, mi)
        mi.pty_replayed = False
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
        self._cancel_tail(mi)
        if not await tmux.has_session(mi.session):
            await self._set_state(mi, RunState.stopped)
            return {"state": mi.state.value, "already_stopped": True}

        stop_cmd = _STOP_COMMAND.get(spec.type.value, _DEFAULT_STOP_VERB)
        await self._set_state(mi, RunState.stopping, detail=f"sending '{stop_cmd}', waiting for a clean save")
        graceful = await self._stop_session(mi.session, stop_cmd, graceful_s)
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
        except asyncio.CancelledError:
            pass

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
            await asyncio.to_thread(self._trim_pty_log, mi)
            return
        if not mi.desired_running:
            return

        # Gone while it was supposed to be running -> a crash. Replay the raw
        # pane first: for a death before the logger is up, that is the only record.
        await self._replay_pty(mi)
        now = time.monotonic()
        mi.crash_times = [t for t in mi.crash_times if now - t < _CRASH_WINDOW_S]
        mi.crash_times.append(now)

        if not mi.spec.auto_restart:
            mi.desired_running = False
            await self._set_state(mi, RunState.crashed, detail="exited (auto-restart off)")
            return

        if len(mi.crash_times) > _CRASH_LIMIT:
            mi.desired_running = False
            await self._set_state(
                mi, RunState.crashed, detail=f"crash-loop: >{_CRASH_LIMIT} in {_CRASH_WINDOW_S:.0f}s"
            )
            return

        if mi.spec.java_home:
            problem = java_home_problem(mi.spec.java_home)
            if problem is not None:
                mi.desired_running = False
                await self._set_state(mi, RunState.crashed, detail=problem)
                return

        await self._set_state(mi, RunState.crashed, detail="crashed; restarting")
        try:
            relaunched = await self._launch(mi)
        except tmux.TmuxError as exc:
            await self._set_state(mi, RunState.crashed, detail=f"restart failed: {exc}")
            return
        if not relaunched:
            await self._replay_pty(mi)
            await self._set_state(mi, RunState.crashed, detail="exited immediately after restart")
            return
        await self._set_state(mi, RunState.running, detail="restarted after crash")

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
        actively tracked, so a freshly-connected agent answers truthfully."""
        transient = (RunState.starting, RunState.stopping, RunState.crashed)
        out: dict[str, str] = {}
        for iid in ids:
            if iid in self._updating:
                out[iid] = RunState.updating.value
                continue
            mi = self._instances.get(iid)
            if mi is not None and mi.state in transient:
                out[iid] = mi.state.value            # trust tracked transient states
            else:
                alive = await tmux.has_session(self.session_name(iid))
                out[iid] = RunState.running.value if alive else RunState.stopped.value
        return out

    # -- introspection -------------------------------------------------------
    def states(self) -> dict[str, RunState]:
        return {iid: mi.state for iid, mi in self._instances.items()}

    def uptime_s(self) -> float:
        return time.monotonic() - self._started_at
