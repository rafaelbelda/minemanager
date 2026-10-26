"""Process supervisor: the agent owns each server's lifecycle.

Each instance runs in its own tmux session. The supervisor starts and stops them,
streams console output by tailing ``logs/latest.log``, watches for vanished
sessions, and restarts crashed instances under a crash-loop limit.

systemd supervises the *agent*; the agent supervises the *servers*.

A failed launch explains itself through the mirrored tmux pane (see
:func:`tmux.pipe_pane`), whose tail is replayed onto the console on a crash.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from minemanager_agent import logtail, tmux, updater
from minemanager_shared.protocol import (
    Action,
    ConsoleOutputData,
    Event,
    InstanceSpec,
    RestartPolicy,
    RunState,
    StateChangedData,
)

EmitFn = Callable[[Event], Awaitable[None]]

# Console command that asks each software to shut down gracefully.
_STOP_COMMAND = {"paper": "stop", "vanilla": "stop", "velocity": "end"}

# Verb for a session whose instance type we no longer know. Wrong only for
# Velocity, which holds no world, so the kill fallback costs it nothing.
_DEFAULT_STOP_VERB = "stop"

_CRASH_LIMIT = 5
_CRASH_WINDOW_S = 60.0

# Failures before readiness are counted separately and more tightly.
_LAUNCH_FAILURE_LIMIT = 3

# A server is RUNNING once it says so; until then it is STARTING. Paper and
# Vanilla print `Done (4.115s)! For help, type "help"`, Velocity `Done (0.5s)!`.
_READY_RE = re.compile(r"Done \(\d+[.,]?\d*s\)!")

# Not every server announces readiness in a form we recognise (modded servers,
# forks, a relocated log), so assume a still-alive instance is up after this
# long. Generous, because a large world can take minutes to load.
_STARTUP_GRACE_S = 300.0
# Stop budgets, one ordered chain. Keep SHUTDOWN_GRACE_S well under the unit's
# TimeoutStopSec (300s), systemd's SIGKILL deadline. KillMode=mixed means
# systemd signals only the agent, so this sequence is what stops worlds.
_GRACEFUL_STOP_S = 45.0
SHUTDOWN_GRACE_S = 120.0

# tmux reports success once the session exists, which says nothing about the
# command inside it. Re-check after this long so an instantly-dying JVM reads
# as crashed rather than running.
_LAUNCH_SETTLE_S = 1.0

# Grace for the log follower to pick up a stopped server's final lines before it
# is cancelled. Must exceed logtail.follow's poll interval.
_TAIL_DRAIN_S = 1.5

# Raw pane mirror (see tmux.pipe_pane): how much to replay on a crash, and the
# ceiling the file is kept under. It duplicates console output, so it is bounded.
_PTY_TAIL_LINES = 40
_PTY_MAX_BYTES = 2 * 1024 * 1024
_PTY_KEEP_BYTES = 512 * 1024

# ANSI/pty control noise: the pane is a terminal, so its mirror carries escape
# sequences the log never has. Tab and newline are kept.
_ANSI_RE = re.compile(
    r"\x1b[@-Z\\-_]|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


@dataclass
class ManagedInstance:
    spec: InstanceSpec
    session: str
    state: RunState = RunState.stopped
    # "Should I relaunch this right now?" Set by start, cleared by stop/kill and
    # by the restart policy giving up. Process-local runtime state, not the hub's
    # ``start_with_node``, which declares what happens at the next agent start.
    should_be_up: bool = False
    crash_times: list[float] = field(default_factory=list)
    tail_task: asyncio.Task | None = None
    # A failed launch is noticed twice (settle check, then monitor); this keeps
    # the error to one printing. Reset by _launch, once per run.
    pty_replayed: bool = False
    # Whether this run announced readiness, and when it launched. Together they
    # decide whether a vanished session was a crash or a launch that never came
    # up. Reset by _launch.
    ready: bool = False
    launched_at: float = 0.0
    # Consecutive launches that died before readiness. Cleared once one gets up.
    launch_failures: int = 0

    @property
    def log_path(self) -> Path:
        return Path(self.spec.root_dir) / "logs" / "latest.log"


def root_dir_problem(root_dir: str) -> str | None:
    """Why ``root_dir`` is unusable as a working directory, or None if it is fine."""
    path = Path(root_dir)
    if not path.is_dir():
        return f"root directory does not exist on this node: {root_dir}"
    if not os.access(path, os.R_OK | os.X_OK):
        return f"root directory is not readable by the agent: {root_dir}"
    return None


def java_home_problem(java_home: str) -> str | None:
    """Why ``java_home`` is unusable, or None if it looks fine.

    Checked before every launch, so an instance pointed at a moved JDK says so
    once instead of crash-looping.
    """
    exe = Path(java_home) / "bin" / "java"
    if not exe.is_file():
        return f"java_home is not a JDK directory: no {exe}"
    if not os.access(exe, os.X_OK):
        return f"java_home is unusable: {exe} is not executable"
    return None


def launch_command(spec: InstanceSpec) -> str:
    """The start command as actually run, with the instance's JDK applied.

    JAVA_HOME/PATH are injected rather than rewriting the ``java`` token, so
    wrapper scripts and ``@argfile`` launches inherit the JDK too. Only the
    directory is quoted; ``$PATH`` must stay expandable by tmux's shell.
    """
    if not spec.java_home:
        return spec.start_command
    home = shlex.quote(spec.java_home)
    return f"JAVA_HOME={home} PATH={shlex.quote(spec.java_home + '/bin')}:$PATH {spec.start_command}"


class Supervisor:
    def __init__(self, emit: EmitFn, pty_dir: Path | None = None,
                 node_root: Path | None = None):
        self._emit = emit
        self._pty_dir = pty_dir
        self.node_root = node_root        # every instance must live under this
        self._instances: dict[str, ManagedInstance] = {}
        self._monitor: asyncio.Task | None = None
        self._started_at = time.monotonic()
        self._updating: set[str] = set()  # instance ids with an update in progress
        self._provisioning: set[str] = set()  # ids being created right now
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

    async def _set_state(
        self,
        mi: ManagedInstance,
        state: RunState,
        detail: str | None = None,
        *,
        clean_exit: bool = False,
    ):
        mi.state = state
        await self._emit(
            Event(
                action=Action.ev_state_changed.value,
                instance_id=mi.spec.id,
                data=StateChangedData(
                    state=state, detail=detail, clean_exit=clean_exit,
                    supervised=mi.should_be_up,
                ).model_dump(mode="json"),
            )
        )

    async def start(self, spec: InstanceSpec) -> dict:
        mi = self._managed(spec)
        if spec.id in self._updating:
            raise RuntimeError("cannot start: a server update is in progress")
        if await tmux.has_session(mi.session):
            # Already up, usually a server this agent did not launch. Its
            # readiness line is long gone, but it has demonstrably come up, so
            # mark it ready: a later death then takes the crash path rather than
            # the launch-failure one.
            mi.should_be_up = True
            mi.ready = True
            mi.launch_failures = 0
            await self._attach_pty_mirror(mi)  # no-op if one is already piped
            self._ensure_tail(mi)
            if mi.state is not RunState.running:
                await self._set_state(mi, RunState.running, detail="already running; adopted")
            return {"state": mi.state.value, "already_running": True}

        problem = root_dir_problem(spec.root_dir)
        if problem is None and spec.java_home:
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

        # Set only now, so the monitor cannot race the settle window above and
        # count one launch as two crashes.
        mi.should_be_up = True
        mi.crash_times.clear()
        mi.launch_failures = 0
        if not alive:
            # Not counted here: the monitor counts every run that ends without
            # reaching readiness, and will see this one on its next pass.
            await self._replay_pty(mi)
            await self._set_state(mi, RunState.crashed, detail="exited immediately after launch")
            return {"state": mi.state.value, "launch_failed": True}
        # Still STARTING: a live session says nothing about whether the server
        # came up. _on_ready promotes it.
        return {"state": mi.state.value}

    async def _launch(self, mi: ManagedInstance) -> bool:
        """Create the session, mirror its pane, and report whether the command
        was still alive after the settle window.

        Raises :class:`tmux.TmuxError` only when the session could not be created
        at all; a command that starts and then dies returns ``False``.
        """
        await asyncio.to_thread(self._reset_pty_log, mi)
        await asyncio.to_thread(self._clear_exit_status, mi)
        mi.pty_replayed = False
        mi.ready = False
        mi.launched_at = time.monotonic()
        path = self._pty_path(mi)
        status = self._status_path(mi)
        await tmux.new_session(
            mi.session, mi.spec.root_dir, launch_command(mi.spec),
            mirror_path=None if path is None else str(path),
            status_path=None if status is None else str(status),
        )
        self._ensure_tail(mi)
        await asyncio.sleep(_LAUNCH_SETTLE_S)
        return await tmux.has_session(mi.session)

    async def stop(self, spec: InstanceSpec, *, graceful_s: float = _GRACEFUL_STOP_S) -> dict:
        mi = self._managed(spec)
        mi.should_be_up = False
        if not await tmux.has_session(mi.session):
            self._cancel_tail(mi)
            await self._set_state(mi, RunState.stopped)
            return {"state": mi.state.value, "already_stopped": True}

        stop_cmd = _STOP_COMMAND.get(spec.type.value, _DEFAULT_STOP_VERB)
        await self._set_state(mi, RunState.stopping, detail=f"sending '{stop_cmd}', waiting for a clean save")
        # Keep tailing until it is down: "Saving worlds" and "Closing Server"
        # are written during this wait and show the stop was clean.
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
        mi.should_be_up = False
        self._cancel_tail(mi)
        await tmux.kill_session(mi.session)
        await self._set_state(mi, RunState.stopped, detail="killed")
        return {"state": mi.state.value}

    def forget(self, instance_id: str) -> None:
        """Drop an instance this agent will never hear about again.

        Nothing else prunes ``_instances``, so an un-declared id would be
        carried for the life of the process and reported by :meth:`states`.
        Only drops the bookkeeping: stop or kill it first.
        """
        mi = self._instances.pop(instance_id, None)
        if mi is not None:
            self._cancel_tail(mi)

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

    @contextmanager
    def provisioning(self, instance_id: str):
        """Hold an instance out of the restart policy while it is being created.

        The configuration run ends with the server exiting by itself, which the
        monitor would otherwise read as a launch failure and retry.
        """
        self._provisioning.add(instance_id)
        try:
            yield
        finally:
            self._provisioning.discard(instance_id)

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
        """Let the tail catch the last lines, then stop it."""
        await asyncio.sleep(_TAIL_DRAIN_S)
        self._cancel_tail(mi)

    async def _tail(self, mi: ManagedInstance) -> None:
        try:
            async for line in logtail.follow(mi.log_path):
                await self._emit(
                    Event(
                        action=Action.ev_console_output.value,
                        instance_id=mi.spec.id,
                        data=ConsoleOutputData(line=line).model_dump(mode="json"),
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

    # -- raw pane mirror -----------------------------------------------------
    def _pty_path(self, mi: ManagedInstance) -> Path | None:
        if self._pty_dir is None:
            return None
        return self._pty_dir / f"{mi.spec.id}.log"

    def _status_path(self, mi: ManagedInstance) -> Path | None:
        if self._pty_dir is None:
            return None
        return self._pty_dir / f"{mi.spec.id}.exit"

    def _clear_exit_status(self, mi: ManagedInstance) -> None:
        """Drop the previous run's status before launching.

        A killed session records no status, so a stale file would read as that
        run's clean shutdown and disarm the instance.
        """
        path = self._status_path(mi)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _read_exit_status(self, mi: ManagedInstance) -> int | None:
        """The status the last run ended with, or None when we cannot know.

        None means "not an orderly shutdown": no pty dir, a session killed
        before its shell could write, or a garbled file.
        """
        path = self._status_path(mi)
        if path is None:
            return None
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _reset_pty_log(self, mi: ManagedInstance) -> None:
        """Begin each run with an empty mirror.

        Truncates in place: the pipe's ``cat`` holds the inode open, so
        replacing the file would leave it writing to an unlinked one.
        """
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
        """Keep the mirror bounded; it grows for as long as the instance runs."""
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

    async def pty_tail(self, instance_id: str) -> list[str]:
        """The tail of an instance's raw pane, for a caller with no console.

        :meth:`_replay_pty` is the same text pushed onto the console; this is
        for a failed creation, where no instance survives to hold one.
        """
        mi = self._instances.get(instance_id)
        if mi is None:
            return []
        return await asyncio.to_thread(self._read_pty_tail, mi)

    async def _attach_pty_mirror(self, mi: ManagedInstance) -> None:
        """Mirror an adopted session, so a later crash is still explained.

        Fresh launches mirror from the start; see :func:`tmux.new_session`.
        """
        path = self._pty_path(mi)
        if path is not None:
            await tmux.pipe_pane(mi.session, str(path))

    async def _replay_pty(self, mi: ManagedInstance) -> None:
        """Push the tail of the raw pane onto the console, for output that never
        reached ``logs/latest.log``. Emitted before the crashed state."""
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
                    data=ConsoleOutputData(line=line, source="pty").model_dump(mode="json"),
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
        if mi.spec.id in self._updating or mi.spec.id in self._provisioning:
            return  # an instance mid-update or mid-creation is not crashed
        alive = await tmux.has_session(mi.session)
        if alive:
            if not mi.ready:
                await self._check_startup_grace(mi)
            await asyncio.to_thread(self._trim_pty_log, mi)
            return
        if not mi.should_be_up:
            return

        # Gone while it was supposed to be running: failure, or a person? A
        # console "stop" exits 0 through the shell that records the status; a
        # crash does not, and a killed session records nothing. Only counted
        # once it had come up, since a clean exit before readiness is a server
        # refusing to run (an unaccepted EULA exits 0).
        clean_exit = mi.ready and await asyncio.to_thread(self._read_exit_status, mi) == 0

        # ALWAYS is the one policy that overrules a person: a console stop is
        # put back like any other exit. Stop, Kill and agent shutdown clear
        # should_be_up before reaching here, and the crash-loop breaker caps it.
        if clean_exit and mi.spec.restart_policy is not RestartPolicy.always:
            mi.should_be_up = False
            mi.crash_times.clear()
            mi.launch_failures = 0
            await self._drain_tail(mi)
            await self._set_state(
                mi, RunState.stopped,
                detail="shut down from the console",
                clean_exit=True,      # the hub disarms it, so it stays down
            )
            return

        # A failure, or a shutdown this instance is configured to overrule.
        # Replay the raw pane only for a failure: when something dies before the
        # logger is up, that is the only record of it.
        if not clean_exit:
            await self._replay_pty(mi)
        launched = mi.ready   # did *this* run ever come up?
        now = time.monotonic()

        if launched:
            mi.crash_times = [t for t in mi.crash_times if now - t < _CRASH_WINDOW_S]
            mi.crash_times.append(now)
        else:
            mi.launch_failures += 1

        if mi.spec.restart_policy is RestartPolicy.never:
            mi.should_be_up = False
            await self._set_state(
                mi, RunState.crashed, detail="exited (restart policy: never)"
            )
            return

        if launched and len(mi.crash_times) > _CRASH_LIMIT:
            mi.should_be_up = False
            await self._set_state(
                mi, RunState.crashed, detail=f"crash-loop: >{_CRASH_LIMIT} in {_CRASH_WINDOW_S:.0f}s"
            )
            return

        if not launched and mi.launch_failures >= _LAUNCH_FAILURE_LIMIT:
            mi.should_be_up = False
            await self._set_state(
                mi, RunState.crashed,
                detail=f"failed to start {mi.launch_failures}x in a row without "
                       f"coming up; not retrying. See the console output above.",
            )
            return

        problem = root_dir_problem(mi.spec.root_dir)
        if problem is None and mi.spec.java_home:
            problem = java_home_problem(mi.spec.java_home)
        if problem is not None:
            mi.should_be_up = False
            await self._set_state(mi, RunState.crashed, detail=problem)
            return

        if clean_exit:
            # Nothing crashed: the operator stopped it, the policy overruled.
            await self._set_state(
                mi, RunState.starting,
                detail="shut down from the console; restarting anyway (policy: always)",
            )
        else:
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

        Covers software whose readiness line we do not recognise.
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
        # Freeze the restart policy before anything stops: the monitor is still
        # running and would relaunch servers mid-shutdown.
        self._shutting_down = True
        for mi in self._instances.values():
            mi.should_be_up = False
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

        The single stop primitive: per-instance ``stop`` and the shutdown path
        both go through here. Returns True if the server exited on its own.
        """
        try:
            await tmux.send_keys(name, verb)
        except tmux.TmuxError:
            pass          # session already gone, or tmux unavailable
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

        Queried concurrently: one ``has-session`` subprocess per instance.
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
            # A live session we do not track predates this agent, so there is no
            # readiness signal for it and "up" is the honest answer.
            return RunState.running.value

        results = await asyncio.gather(*(one(iid) for iid in ids))
        return dict(zip(ids, results))

    # -- introspection -------------------------------------------------------
    def states(self) -> dict[str, RunState]:
        return {iid: mi.state for iid, mi in self._instances.items()}

    def uptime_s(self) -> float:
        return time.monotonic() - self._started_at
