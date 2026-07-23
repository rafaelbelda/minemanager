"""In-memory registry of connected agents + pub/sub for their event streams.

One :class:`AgentConnection` exists per live agent WebSocket. The hub issues
commands through it and awaits correlated responses via per-command futures.
Unsolicited events (console output, log lines, state changes, heartbeats) are
published to any UI subscribers currently watching that node.

This state is deliberately in-memory and ephemeral — it mirrors *live*
connections. Durable/declared state lives in the database.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Callable

from minemanager_shared.protocol import Command, Event, Response

# A sink receives Event objects for a node the caller subscribed to.
EventSink = Callable[[Event], None]


class CommandTimeout(Exception):
    """Raised when an agent does not answer a command in time."""


class AgentConnection:
    """Server-side handle for one connected agent.

    ``send`` is an async callable that writes a JSON-serializable dict to the
    underlying WebSocket. Keeping it as a callable rather than the raw socket
    keeps this class transport-agnostic and easy to test.
    """

    def __init__(self, node_id: str, send: Callable[[dict[str, Any]], "asyncio.Future | Any"]):
        self.node_id = node_id
        self._send = send
        self._pending: dict[str, asyncio.Future[Response]] = {}
        self._send_lock = asyncio.Lock()

    async def _write(self, frame_dict: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._send(frame_dict)

    async def call(
        self,
        action: str,
        *,
        instance_id: str | None = None,
        data: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Response:
        """Send a command and await the agent's correlated response."""
        cmd = Command(action=action, instance_id=instance_id, data=data or {})
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Response] = loop.create_future()
        self._pending[cmd.id] = fut
        try:
            await self._write(cmd.model_dump(mode="json"))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CommandTimeout(f"agent {self.node_id} did not answer {action}") from exc
        finally:
            self._pending.pop(cmd.id, None)

    def resolve(self, response: Response) -> None:
        """Match an incoming response to its waiting future."""
        fut = self._pending.get(response.id)
        if fut is not None and not fut.done():
            fut.set_result(response)

    def fail_all(self, exc: Exception) -> None:
        """Reject every in-flight command (called when the connection drops)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentConnection] = {}
        # node_id -> set of subscriber sinks
        self._subscribers: dict[str, set[EventSink]] = {}

    # -- connection lifecycle ------------------------------------------------
    def register(self, conn: AgentConnection) -> None:
        self._agents[conn.node_id] = conn

    def unregister(self, node_id: str) -> None:
        conn = self._agents.pop(node_id, None)
        if conn is not None:
            conn.fail_all(ConnectionError("agent disconnected"))

    def get(self, node_id: str) -> AgentConnection | None:
        return self._agents.get(node_id)

    def is_online(self, node_id: str) -> bool:
        return node_id in self._agents

    def online_node_ids(self) -> list[str]:
        return list(self._agents)

    # -- event fan-out -------------------------------------------------------
    def publish(self, node_id: str, event: Event) -> None:
        for sink in list(self._subscribers.get(node_id, ())):
            try:
                sink(event)
            except Exception:  # noqa: BLE001 - a bad sink must not break others
                continue

    @contextlib.contextmanager
    def _subscription(self, node_id: str, sink: EventSink):
        self._subscribers.setdefault(node_id, set()).add(sink)
        try:
            yield
        finally:
            subs = self._subscribers.get(node_id)
            if subs is not None:
                subs.discard(sink)
                if not subs:
                    self._subscribers.pop(node_id, None)

    async def stream(self, node_id: str) -> AsyncIterator[Event]:
        """Async-iterate events for a node (for a UI WebSocket to forward)."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        with self._subscription(node_id, queue.put_nowait):
            while True:
                yield await queue.get()


# Process-wide singleton.
registry = AgentRegistry()
