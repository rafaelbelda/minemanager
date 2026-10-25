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

# In-flight buffer per transfer, in bytes rather than queue items: the hub does
# not choose the chunk size on the producing hop (``request.stream()`` hands over
# whatever the ASGI server read off the socket), so an item count bounded memory
# only incidentally.
_WINDOW_BYTES = 8 * 1024 * 1024
# A parked producer resumes here, not as soon as one chunk drains: waking per
# chunk costs about 60% of throughput on loopback.
_RESUME_BYTES = _WINDOW_BYTES // 2


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
        default_factory=lambda: asyncio.Queue()      # unbounded; _window bounds it
    )
    header_ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    created: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    # Bytes queued but not yet handed to the consuming hop, plus the future a
    # blocked producer parks on (a bare future needs no clear/set handshake).
    _queued: int = 0
    _resume: "asyncio.Future[None] | None" = None

    async def put(self, item: "bytes | None") -> bool:
        """Queue a chunk, applying backpressure once the window is full.

        Returns False when the transfer was cancelled while waiting, so the
        producing hop can exit promptly instead of parking forever on a queue
        nothing is draining any more.
        """
        if self.cancel.is_set():
            return False

        # EOF is never held back: it carries no bytes, and blocking it would
        # deadlock a consumer waiting for the end of the stream.
        if item is not None and self._queued >= _WINDOW_BYTES:
            if self._resume is None:
                self._resume = asyncio.get_running_loop().create_future()
            waiter = self._resume
            canceller = asyncio.ensure_future(self.cancel.wait())
            try:
                done, _ = await asyncio.wait(
                    {waiter, canceller}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                canceller.cancel()
            if waiter not in done:
                return False

        if self.cancel.is_set():
            return False
        if item is not None:
            self._queued += len(item)
        self.queue.put_nowait(item)
        return True

    async def get(self) -> "bytes | None":
        """Take the next chunk, releasing its bytes back into the window."""
        item = await self.queue.get()
        if item is not None:
            self._queued -= len(item)
        self._maybe_resume()
        return item

    def _maybe_resume(self) -> None:
        """Wake a parked producer once the backlog drains to _RESUME_BYTES."""
        if self._resume is not None and (
            self._queued <= _RESUME_BYTES or self.cancel.is_set()
        ):
            if not self._resume.done():
                self._resume.set_result(None)
            self._resume = None

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


class TransferIdInUse(Exception):
    """A live transfer already holds the requested id."""


class TransferRegistry:
    def __init__(self) -> None:
        self._t: dict[str, TransferContext] = {}

    def create(self, tid: str, node_id: str, direction: str, path: str) -> TransferContext:
        self._sweep()
        if tid in self._t:
            raise TransferIdInUse(tid)
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
