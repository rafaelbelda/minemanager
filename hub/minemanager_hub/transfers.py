"""In-flight large-file transfer bookkeeping.

A transfer streams bytes between the browser and an agent *through* the hub,
correlated by a client-generated ``transfer_id``. The hub never buffers the whole
file: a small bounded queue bridges the browser side and the agent side, and TCP
backpressure on both hops keeps memory flat. This registry just tracks the live
contexts (for the bridge, for progress polling, and for cancellation).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

CHUNK = 256 * 1024          # streaming chunk size
_QUEUE_CHUNKS = 16          # ~4 MB in-flight buffer per transfer (bounds memory)


@dataclass
class TransferContext:
    id: str
    node_id: str
    direction: str          # "download" (agent->browser) | "upload" (browser->agent)
    path: str
    filename: str = ""
    total: int = 0          # 0 = unknown
    sent: int = 0
    state: str = "pending"  # pending | active | done | error | cancelled
    error: str | None = None
    queue: "asyncio.Queue[bytes | None]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_CHUNKS)
    )
    header_ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    created: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    async def put(self, item: "bytes | None") -> bool:

        if self.cancel.is_set():
            return False
        if not self.queue.full():
            self.queue.put_nowait(item)
            return True

        putter = asyncio.ensure_future(self.queue.put(item))
        waiter = asyncio.ensure_future(self.cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {putter, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if putter in done:
                return True
            putter.cancel()
            return False
        finally:
            waiter.cancel()

    def finish(self, state: str, error: str | None = None) -> None:
        self.state = state
        self.error = error
        if self.finished_at is None:
            self.finished_at = time.monotonic()

    def as_status(self) -> dict:
        return {
            "id": self.id,
            "direction": self.direction,
            "filename": self.filename,
            "total": self.total,
            "sent": self.sent,
            "state": self.state,
            "error": self.error,
        }


class TransferRegistry:
    def __init__(self) -> None:
        self._t: dict[str, TransferContext] = {}

    def create(self, tid: str, node_id: str, direction: str, path: str) -> TransferContext:
        self._sweep()
        ctx = TransferContext(id=tid, node_id=node_id, direction=direction, path=path)
        self._t[tid] = ctx
        return ctx

    def get(self, tid: str) -> TransferContext | None:
        self._sweep()
        return self._t.get(tid)

    def remove(self, tid: str) -> None:
        self._t.pop(tid, None)

    def _sweep(self, max_age: float = 6 * 3600, finished_ttl: float = 90) -> None:
        now = time.monotonic()
        stale = [
            k for k, v in self._t.items()
            if (now - v.created > max_age)
            or (v.finished_at is not None and now - v.finished_at > finished_ttl)
        ]
        for tid in stale:
            self._t.pop(tid, None)


transfers = TransferRegistry()
