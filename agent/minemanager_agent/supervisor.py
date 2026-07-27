"""Process supervisor: the agent owns each server's lifecycle (Model B).

Each instance runs in its own tmux session. The supervisor starts/stops them,
streams console output by tailing ``logs/latest.log``, and — crucially — is the
thing that keeps them running: it watches for vanished sessions and restarts
crashed instances, with crash-loop protection so a broken server doesn't spin
forever.

systemd supervises the *agent*; the agent supervises the *servers*.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from minemanager_agent import logtail, tmux, updater
from minemanager_shared.protocol import Action, Event, InstanceSpec, RunState

EmitFn = Callable[[Event], Awaitable[None]]

# Console command that asks each software to shut down gracefully.
_STOP_COMMAND = {"paper": "stop", "vanilla": "stop", "velocity": "end"}

# Crash-loop policy: if an instance crashes more than N times within WINDOW
# seconds, stop auto-restarting it and surface a crashed state.
_CRASH_LIMIT = 5
_CRASH_WINDOW_S = 60.0
_GRACEFUL_STOP_S = 45.0


@dataclass
class ManagedInstance:
    spec: InstanceSpec
    session: str
    state: RunState = RunState.stopped
    desired_running: bool = False
    crash_times: list[float] = field(default_factory=list)
    tail_task: asyncio.Task | None = None

    @property
    def log_path(self) -> Path:
        return Path(self.spec.root_dir) / "logs" / "latest.log"


class Supervisor:
    def __init__(self, session_prefix: str, emit: EmitFn):
        self._prefix = session_prefix
        self._emit = emit
        self._instances: dict[str, ManagedInstance] = {}
        self._monitor: asyncio.Task | None = None
        self._started_at = time.monotonic()
        self._updating: set[str] = set()  # instance ids with an update in progress

    # -- lifecycle -----------------------------------------------------------
    def session_name(self, instance_id: str) -> str:
        return f"{self._prefix}-{instance_id}"

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
            mi.desired_running = True
            return {"state": mi.state.value, "already_running": True}

        await self._set_state(mi, RunState.starting)
        await tmux.new_session(mi.session, spec.root_dir, spec.start_command)
        mi.desired_running = True
        mi.crash_times.clear()
        self._ensure_tail(mi)
        await self._set_state(mi, RunState.running)
        return {"state": mi.state.value}

    async def stop(self, spec: InstanceSpec, *, graceful_s: float = _GRACEFUL_STOP_S) -> dict:
        mi = self._managed(spec)
        mi.desired_running = False
        if not await tmux.has_session(mi.session):
            await self._set_state(mi, RunState.stopped)
            return {"state": mi.state.value, "already_stopped": True}

        await self._set_state(mi, RunState.stopping)
        stop_cmd = _STOP_COMMAND.get(spec.type.value, "stop")
        try:
            await tmux.send_keys(mi.session, stop_cmd)
        except tmux.TmuxError:
            pass

        deadline = time.monotonic() + graceful_s
        while time.monotonic() < deadline:
            if not await tmux.has_session(mi.session):
                await self._set_state(mi, RunState.stopped)
                return {"state": mi.state.value, "graceful": True}
            await asyncio.sleep(0.5)

        # Graceful window elapsed — force it.
        await tmux.kill_session(mi.session)
        await self._set_state(mi, RunState.stopped, detail="killed after graceful timeout")
        return {"state": mi.state.value, "graceful": False}

    async def restart(self, spec: InstanceSpec) -> dict:
        await self.stop(spec)
        return await self.start(spec)

    async def kill(self, spec: InstanceSpec) -> dict:
        mi = self._managed(spec)
        mi.desired_running = False
        await tmux.kill_session(mi.session)
        await self._set_state(mi, RunState.stopped, detail="killed")
        return {"state": mi.state.value}

    async def send(self, spec: InstanceSpec, line: str) -> dict:
        mi = self._managed(spec)
        await tmux.send_keys(mi.session, line)
        return {"sent": True}

    # -- version updater -----------------------------------------------------
    async def apply_update(self, spec: InstanceSpec, jar_name: str, download: dict) -> dict:
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
            result = await updater.apply_update(spec.root_dir, jar_name, download)
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

    # -- monitor / restart policy -------------------------------------------
    async def run_monitor(self, interval_s: float = 3.0) -> None:
        """Background loop: detect vanished sessions and apply restart policy."""
        while True:
            await asyncio.sleep(interval_s)
            for mi in list(self._instances.values()):
                await self._check_one(mi)

    async def _check_one(self, mi: ManagedInstance) -> None:
        if mi.spec.id in self._updating:
            return  # never treat a mid-update instance as crashed
        alive = await tmux.has_session(mi.session)
        if alive or not mi.desired_running:
            return

        # Session gone while it was supposed to be running -> a crash.
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

        await self._set_state(mi, RunState.crashed, detail="crashed; restarting")
        try:
            await tmux.new_session(mi.session, mi.spec.root_dir, mi.spec.start_command)
            self._ensure_tail(mi)
            await self._set_state(mi, RunState.running, detail="restarted after crash")
        except tmux.TmuxError as exc:
            await self._set_state(mi, RunState.crashed, detail=f"restart failed: {exc}")

    # -- introspection -------------------------------------------------------
    def states(self) -> dict[str, RunState]:
        return {iid: mi.state for iid, mi in self._instances.items()}

    def uptime_s(self) -> float:
        return time.monotonic() - self._started_at
