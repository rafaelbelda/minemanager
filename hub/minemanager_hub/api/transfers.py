"""Streaming large-file transfer endpoints (multi-GB, memory-bounded).

Data path (agent always dials out, so it initiates the HTTP data connection):

  download  browser GET  ─┐                          ┌─ agent POST /internal/transfer/{tid}
                          ├─►  hub bridges via a  ◄───┤   (streams the file up to the hub)
  upload    browser POST ─┘     bounded queue         └─ agent GET  /internal/transfer/{tid}
                                                          (pulls the body, writes to disk)

The hub tells the agent to connect via a ``transfer.start`` control-WS command.
Progress is polled from ``GET /api/transfers/{tid}``; cancellation sets an event
that tears both hops down. Backpressure across each TCP hop keeps memory flat.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from minemanager_hub.agents.registry import CommandTimeout, registry
from minemanager_hub.api.control import _agent_and_spec
from minemanager_hub.db.models import Node
from minemanager_hub.db.session import session_scope
from minemanager_hub.security import tokens
from minemanager_hub.transfers import CHUNK, transfers
from minemanager_shared.protocol import Action

router = APIRouter(prefix="/api", tags=["transfers"])

_HEADER_TIMEOUT = 300.0          # wait for the agent to connect + send headers
_CMD_TIMEOUT = 12 * 3600.0       # the control command lives for the whole transfer


def _auth_agent(request: Request, expected_node: str) -> None:
    node_id = request.headers.get("x-mm-node")
    cred = request.headers.get("x-mm-cred")
    if node_id != expected_node or not cred:
        raise HTTPException(403, "transfer auth failed")
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None or not tokens.verify_token(cred, node.credential_hash):
            raise HTTPException(403, "transfer auth failed")


# --- browser-facing --------------------------------------------------------
@router.get("/instances/{instance_id}/files/download-stream")
async def download_stream(instance_id: str, path: str, tid: str) -> StreamingResponse:
    conn, spec = _agent_and_spec(instance_id)
    ctx = transfers.create(tid, node_id=conn.node_id, direction="download", path=path)

    # Ask the agent to push; the command resolves only when the push completes.
    fut = asyncio.ensure_future(conn.call(
        Action.transfer_start.value, instance_id=instance_id,
        data={"tid": tid, "direction": "download", "path": path, "instance": spec},
        timeout=_CMD_TIMEOUT,
    ))
    fut.add_done_callback(lambda f: f.exception())  # swallow late errors

    # Wait until the agent's data connection has provided size + filename.
    waiter = asyncio.ensure_future(ctx.header_ready.wait())
    done, _ = await asyncio.wait({waiter, fut}, timeout=_HEADER_TIMEOUT,
                                 return_when=asyncio.FIRST_COMPLETED)
    if waiter not in done:
        waiter.cancel()
        transfers.remove(tid)
        if fut in done and fut.exception() is None and not fut.result().ok:
            raise HTTPException(502, fut.result().error or "transfer failed to start")
        raise HTTPException(504, "agent did not start the transfer")

    headers = {"content-disposition": f"attachment; filename*=UTF-8''{quote(ctx.filename or 'download')}"}
    if ctx.total:
        headers["content-length"] = str(ctx.total)

    async def gen():
        ctx.state = "active"
        try:
            while True:
                chunk = await ctx.queue.get()
                if chunk is None:
                    break
                ctx.sent += len(chunk)
                yield chunk
            ctx.finish("cancelled" if ctx.cancel.is_set() else "done")
        except asyncio.CancelledError:
            ctx.cancel.set()
            ctx.finish("cancelled")
            raise
        # ctx is kept briefly (finished_at) so the progress poll sees the result.

    return StreamingResponse(
        gen(),
        media_type="application/zip" if ctx.filename.endswith(".zip") else "application/octet-stream",
        headers=headers,
    )


@router.post("/instances/{instance_id}/files/upload-stream")
async def upload_stream(instance_id: str, path: str, tid: str, request: Request) -> dict:
    conn, spec = _agent_and_spec(instance_id)
    ctx = transfers.create(tid, node_id=conn.node_id, direction="upload", path=path)
    ctx.filename = path.rsplit("/", 1)[-1]
    try:
        ctx.total = int(request.headers.get("content-length") or 0)
    except ValueError:
        ctx.total = 0

    fut = asyncio.ensure_future(conn.call(
        Action.transfer_start.value, instance_id=instance_id,
        data={"tid": tid, "direction": "upload", "path": path,
              "total": ctx.total, "instance": spec},
        timeout=_CMD_TIMEOUT,
    ))

    async def pump_body():
        ctx.state = "active"
        try:
            async for chunk in request.stream():
                if not await ctx.put(chunk):
                    break
                ctx.sent += len(chunk)
        finally:
            await ctx.put(None)  # sentinel: EOF (or abort)

    pump = asyncio.ensure_future(pump_body())
    try:
        resp = await fut                       # agent's final ack (write complete)
    except (CommandTimeout, ConnectionError) as exc:
        ctx.cancel.set()
        ctx.finish("error", str(exc))
        raise HTTPException(502, f"upload failed: {exc}") from exc
    finally:
        pump.cancel()
    if not resp.ok:
        ctx.finish("error", resp.error)
        raise HTTPException(502, resp.error or "agent rejected the upload")
    ctx.finish("done")
    return {"ok": True, "path": path}


@router.get("/transfers/{tid}")
def transfer_status(tid: str) -> dict:
    ctx = transfers.get(tid)
    if ctx is None:
        # Either not started yet or already swept — the pill grace-handles this.
        return {"id": tid, "state": "unknown", "total": 0, "sent": 0}
    return ctx.as_status()


@router.post("/transfers/{tid}/cancel")
async def transfer_cancel(tid: str) -> dict:
    #Cancel an in-flight transfer
    
    ctx = transfers.get(tid)
    if ctx is not None:
        ctx.cancel.set()
        ctx.finish("cancelled")
        try:
            ctx.queue.put_nowait(None)   # unblock a consumer parked on get()
        except asyncio.QueueFull:
            pass
    return {"ok": True}


# --- agent-facing (internal data connections) ------------------------------
@router.post("/internal/transfer/{tid}")
async def internal_push(tid: str, request: Request) -> dict:
    """Agent streams a file up to the hub (the download data path)."""
    ctx = transfers.get(tid)
    if ctx is None or ctx.direction != "download":
        raise HTTPException(404, "no such transfer")
    _auth_agent(request, ctx.node_id)

    ctx.filename = request.headers.get("x-mm-filename") or ctx.filename or "download"
    try:
        ctx.total = int(request.headers.get("x-mm-total") or 0)
    except ValueError:
        ctx.total = 0
    ctx.header_ready.set()

    try:
        async for chunk in request.stream():
            # Cancellable put: backpressure when the browser is keeping up, but
            # a prompt exit if it disconnected and nothing is draining any more.
            if not await ctx.put(chunk):
                break
    finally:
        await ctx.put(None)
    return {"ok": True}


@router.get("/internal/transfer/{tid}")
async def internal_pull(tid: str, request: Request) -> StreamingResponse:
    """Agent pulls the body from the hub (the upload data path)."""
    ctx = transfers.get(tid)
    if ctx is None or ctx.direction != "upload":
        raise HTTPException(404, "no such transfer")
    _auth_agent(request, ctx.node_id)
    ctx.header_ready.set()

    async def gen():
        while True:
            chunk = await ctx.queue.get()
            if chunk is None or ctx.cancel.is_set():
                break
            yield chunk

    headers = {"content-length": str(ctx.total)} if ctx.total else {}
    return StreamingResponse(gen(), media_type="application/octet-stream", headers=headers)
