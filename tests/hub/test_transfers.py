"""In-flight transfer bookkeeping.

The id is client-supplied, so collision handling is the interesting part: two
browsers picking the same tid must not silently bridge one transfer's hops to
the other's context.
"""

from __future__ import annotations

import asyncio

import pytest

from minemanager_hub.transfers import _WINDOW_BYTES, TransferIdInUse, TransferRegistry


@pytest.fixture
def reg():
    return TransferRegistry()


def make(reg, tid="t1", direction="download"):
    return reg.create(tid, node_id="n1", direction=direction, path="world.zip")


async def _fill_window(ctx, chunk=64 * 1024):
    """Queue exactly enough bytes to close the in-flight window."""
    while ctx._queued < _WINDOW_BYTES:
        assert await ctx.put(b"x" * chunk) is True


class TestRegistry:
    def test_create_and_get(self, reg):
        ctx = make(reg)
        assert reg.get("t1") is ctx
        assert (ctx.direction, ctx.state) == ("download", "pending")

    def test_duplicate_id_is_refused(self, reg):
        make(reg)
        with pytest.raises(TransferIdInUse):
            make(reg)

    def test_the_original_context_survives_a_collision(self, reg):
        first = make(reg)
        with pytest.raises(TransferIdInUse):
            make(reg)
        assert reg.get("t1") is first

    def test_distinct_ids_coexist(self, reg):
        a, b = make(reg, "t1"), make(reg, "t2")
        assert (reg.get("t1"), reg.get("t2")) == (a, b)

    def test_remove(self, reg):
        make(reg)
        reg.remove("t1")
        assert reg.get("t1") is None

    def test_an_id_is_reusable_once_removed(self, reg):
        make(reg)
        reg.remove("t1")
        assert make(reg) is not None


class TestSweep:
    def test_finished_contexts_are_swept(self, reg, monkeypatch):
        ctx = make(reg)
        ctx.finish("done")
        monkeypatch.setattr(ctx, "finished_at", ctx.finished_at - 1000)
        assert reg.get("t1") is None

    def test_live_contexts_are_kept(self, reg):
        make(reg)
        assert reg.get("t1") is not None

    def test_a_finished_context_lingers_briefly_for_the_progress_poll(self, reg):
        make(reg).finish("done")
        assert reg.get("t1") is not None


class TestContext:
    async def test_put_and_drain(self, reg):
        ctx = make(reg)
        assert await ctx.put(b"chunk") is True
        assert await ctx.get() == b"chunk"

    async def test_put_refused_after_cancel(self, reg):
        ctx = make(reg)
        ctx.cancel.set()
        assert await ctx.put(b"chunk") is False

    async def test_put_unblocks_when_cancelled_mid_wait(self, reg):
        ctx = make(reg)
        await _fill_window(ctx)

        async def cancel_soon():
            await asyncio.sleep(0.05)
            ctx.cancel.set()

        asyncio.ensure_future(cancel_soon())
        assert await asyncio.wait_for(ctx.put(b"blocked"), timeout=2) is False

    async def test_the_window_bounds_bytes_not_chunk_count(self, reg):
        """An item-counted queue blocked after 16 chunks however small they
        were, so the real bound depended on the transport's chunk size."""
        ctx = make(reg)
        for _ in range(64):
            assert await asyncio.wait_for(ctx.put(b"x" * 1024), timeout=1) is True
        assert ctx._queued == 64 * 1024

    async def test_put_blocks_once_the_byte_window_is_full(self, reg):
        ctx = make(reg)
        await _fill_window(ctx)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ctx.put(b"over"), timeout=0.2)

    async def test_draining_reopens_the_window(self, reg):
        ctx = make(reg)
        await _fill_window(ctx)
        await ctx.get()                       # release one chunk's worth
        assert await asyncio.wait_for(ctx.put(b"more"), timeout=1) is True

    async def test_the_eof_sentinel_is_never_held_back(self, reg):
        """A full window must not delay EOF — the consumer waits on it."""
        ctx = make(reg)
        await _fill_window(ctx)
        assert await asyncio.wait_for(ctx.put(None), timeout=1) is True

    def test_finish_records_the_first_outcome_only(self, reg):
        ctx = make(reg)
        ctx.finish("done")
        first = ctx.finished_at
        ctx.finish("error", "later")
        assert ctx.finished_at == first

    def test_status_shape(self, reg):
        ctx = make(reg)
        ctx.total, ctx.sent = 100, 40
        assert ctx.as_status()["sent"] == 40
        assert set(ctx.as_status()) == {
            "id", "direction", "filename", "total", "sent", "state", "error",
        }


class TestApi:
    def test_status_of_an_unknown_transfer_is_not_an_error(self, client):
        body = client.get("/api/transfers/never-existed").json()
        assert body["state"] == "unknown"

    def test_cancel_of_an_unknown_transfer_is_a_no_op(self, client):
        assert client.post("/api/transfers/never-existed/cancel").json() == {"ok": True}
