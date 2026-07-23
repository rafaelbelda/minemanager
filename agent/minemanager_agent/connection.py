"""Persistent WebSocket connection to the hub, with handshake and reconnect.

The agent dials out (agent -> hub) so only the hub needs a stable address on the
WireGuard mesh. On connect it performs the :class:`Hello`/:class:`Welcome`
handshake (enrollment token on first run, persisted credential thereafter),
then pumps frames: commands go to the dispatcher, and the supervisor's events
are written back up the socket.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from minemanager_agent import __version__, handlers
from minemanager_agent.config import AgentConfig
from minemanager_agent.supervisor import Supervisor
from minemanager_shared.protocol import (
    Action,
    Command,
    Event,
    Hello,
    Welcome,
    parse_frame,
)

log = logging.getLogger("minemanager.agent")


class Connection:
    def __init__(self, config: AgentConfig, hostname: str):
        self.config = config
        self.hostname = hostname
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._send_lock = asyncio.Lock()
        # The supervisor emits events through this connection.
        self.supervisor = Supervisor(config.session_prefix, self._emit_event)

    async def _emit_event(self, event: Event) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async with self._send_lock:
                await ws.send(event.model_dump_json())
        except websockets.ConnectionClosed:
            pass  # dropped mid-stream; reconnect logic will re-establish

    async def _handshake(self, ws: websockets.WebSocketClientProtocol) -> str:
        node_id, credential = self.config.load_identity()
        hello = Hello(
            agent_version=__version__,
            hostname=self.hostname,
            node_id=node_id,
            credential=credential,
            enrollment_token=None if credential else self.config.enroll_token,
            capabilities=["power", "console", "files", "rcon", "logs"],
        )
        await ws.send(hello.model_dump_json())
        raw = json.loads(await ws.recv())
        welcome = parse_frame(raw)
        if not isinstance(welcome, Welcome) or not welcome.ok:
            reason = getattr(welcome, "error", "unknown")
            raise RuntimeError(f"handshake rejected: {reason}")
        if welcome.credential and welcome.node_id:
            # First enrollment — persist the issued long-lived credential.
            self.config.save_identity(welcome.node_id, welcome.credential)
            log.info("enrolled as node %s", welcome.node_id)
        return welcome.node_id or node_id or "?"

    async def _heartbeat_loop(self) -> None:
        """Periodically report liveness + instance states to the hub.

        Keeps the hub's ``last_seen`` fresh even when an agent is idle (no
        running servers, so no console/state events would otherwise flow).
        """
        while True:
            await asyncio.sleep(self.config.heartbeat_s)
            await self._emit_event(
                Event(
                    action=Action.ev_heartbeat.value,
                    data={
                        "uptime_s": self.supervisor.uptime_s(),
                        "instances": {k: v.value for k, v in self.supervisor.states().items()},
                    },
                )
            )

    async def _serve(self, ws: websockets.WebSocketClientProtocol) -> None:
        self._ws = ws
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._pump(ws)
        finally:
            heartbeat.cancel()

    async def _pump(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            try:
                frame = parse_frame(json.loads(raw))
            except Exception:
                continue
            if isinstance(frame, Command):
                # Dispatch concurrently so a slow command can't block the socket.
                asyncio.create_task(self._handle_command(ws, frame))

    async def _handle_command(self, ws, cmd: Command) -> None:
        resp = await handlers.handle(cmd, self.supervisor)
        try:
            async with self._send_lock:
                await ws.send(resp.model_dump_json())
        except websockets.ConnectionClosed:
            pass

    async def run_forever(self) -> None:
        """Connect, serve, and reconnect with exponential backoff forever."""
        monitor = asyncio.create_task(self.supervisor.run_monitor())
        backoff = self.config.reconnect_min_s
        try:
            while True:
                try:
                    async with websockets.connect(self.config.hub_url, max_size=16 * 2**20) as ws:
                        node_id = await self._handshake(ws)
                        log.info("connected to hub as node %s", node_id)
                        backoff = self.config.reconnect_min_s  # reset on success
                        await self._serve(ws)
                except (OSError, websockets.WebSocketException, RuntimeError) as exc:
                    log.warning("connection lost (%s); retrying in %.0fs", exc, backoff)
                finally:
                    self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.config.reconnect_max_s)
        finally:
            monitor.cancel()
