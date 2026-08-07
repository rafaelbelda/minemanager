"""Control-plane routes: proxy power/console/file commands to the owning agent
and stream a node's live events to the UI.

Resolution and forwarding live in :mod:`minemanager_hub.api.proxy`; each route
here only names its action and supplies that action's payload fields.
"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect

from minemanager_hub.agents.registry import CommandTimeout, registry
from minemanager_hub.api.proxy import proxy
from minemanager_hub.api.schemas import (
    ConsoleSend,
    FileExtract,
    FileRename,
    FileUpload,
    FileWrite,
)
from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Instance
from minemanager_hub.db.session import session_scope
from minemanager_shared.protocol import (
    Action,
    ConsoleSendData,
    FilesDeleteData,
    FilesExtractData,
    FilesFetchData,
    FilesListData,
    FilesReadData,
    FilesRenameData,
    FilesUploadData,
    FilesWriteData,
    InstanceStatesData,
    LogsTailData,
    PowerData,
)

router = APIRouter(prefix="/api", tags=["control"])


@router.get("/config", tags=["config"])
def ui_config() -> dict:
    """UI-relevant limits (configurable via env). The web app reads this at boot
    so thresholds are never hardcoded in the frontend."""
    s = get_settings()
    return {
        "editor_warn_bytes": s.editor_warn_bytes,
        "editor_max_bytes": s.editor_max_bytes,
        "transfer_cap_bytes": s.transfer_cap_bytes,
    }


# --- Batch run-state (fast initial load) -----------------------------------
@router.get("/nodes/{node_id}/instance-states")
async def node_instance_states(node_id: str) -> dict:
    """Real run-state for a node's instances, so the UI need not wait for the
    first heartbeat. Best-effort: an unreachable agent yields no states."""
    conn = registry.get(node_id)
    if conn is None:
        return {"states": {}}
    with session_scope() as db:
        ids = [i.id for i in db.query(Instance).filter(Instance.node_id == node_id).all()]
    if not ids:
        return {"states": {}}
    try:
        resp = await conn.call(
            Action.instance_states.value,
            data=InstanceStatesData(ids=ids).model_dump(mode="json"),
            timeout=15,
        )
    except (CommandTimeout, ConnectionError):
        return {"states": {}}
    return resp.data if resp.ok else {"states": {}}


# --- Power -----------------------------------------------------------------
@router.post("/instances/{instance_id}/power/{op}")
async def power(instance_id: str, op: str) -> dict:
    action = {
        "start": Action.power_start,
        "stop": Action.power_stop,
        "restart": Action.power_restart,
        "kill": Action.power_kill,
    }.get(op)
    if action is None:
        raise HTTPException(400, f"unknown power op: {op}")

    result = await proxy(instance_id, action.value, PowerData)

    # Recorded only after the agent accepts, so a failed command cannot leave the
    # DB claiming "should be running". Not yet pushed back on reconnect, so this
    # is bookkeeping for the UI rather than reconciliation.
    if op in ("start", "stop"):
        with session_scope() as db:
            inst = db.get(Instance, instance_id)
            if inst is not None:
                inst.desired_running = op == "start"

    return result


# --- Console ---------------------------------------------------------------
@router.post("/instances/{instance_id}/console")
async def console_send(instance_id: str, body: ConsoleSend) -> dict:
    return await proxy(instance_id, Action.console_send.value, ConsoleSendData, line=body.line)


@router.get("/instances/{instance_id}/console/history")
async def console_history(instance_id: str, lines: int = 200) -> dict:
    """Recent console lines from the instance's log, to backfill a fresh UI
    session before the live stream takes over. Read-only; best-effort."""
    lines = max(1, min(lines, 1000))
    return await proxy(instance_id, Action.logs_tail.value, LogsTailData, lines=lines)


# --- Files -----------------------------------------------------------------
@router.get("/instances/{instance_id}/files")
async def files_list(instance_id: str, path: str = ".") -> dict:
    return await proxy(instance_id, Action.files_list.value, FilesListData, path=path)


@router.get("/instances/{instance_id}/files/read")
async def files_read(instance_id: str, path: str) -> dict:
    return await proxy(
        instance_id, Action.files_read.value, FilesReadData,
        path=path, max_bytes=get_settings().editor_max_bytes,
    )


@router.post("/instances/{instance_id}/files/write")
async def files_write(instance_id: str, body: FileWrite) -> dict:
    return await proxy(
        instance_id, Action.files_write.value, FilesWriteData,
        path=body.path, content=body.content,
    )


@router.delete("/instances/{instance_id}/files")
async def files_delete(instance_id: str, path: str, recursive: bool = False) -> dict:
    return await proxy(
        instance_id, Action.files_delete.value, FilesDeleteData,
        path=path, recursive=recursive,
    )


@router.post("/instances/{instance_id}/files/upload")
async def files_upload(instance_id: str, body: FileUpload) -> dict:
    """Simple (non-streaming) upload for normal-sized files. Larger files use the
    streaming transfer path (separate feature)."""
    cap = get_settings().transfer_cap_bytes
    approx_bytes = len(body.content_b64) * 3 // 4
    if approx_bytes > cap:
        raise HTTPException(413, f"file exceeds the {cap}-byte simple-upload limit — use the large transfer")
    return await proxy(
        instance_id, Action.files_upload.value, FilesUploadData,
        path=body.path, content_b64=body.content_b64,
    )


@router.get("/instances/{instance_id}/files/download")
async def files_download(instance_id: str, path: str) -> Response:
    """Download a file, or a directory as a zip. Bytes come from the agent
    (base64) and are returned as a browser file download."""
    data = await proxy(
        instance_id, Action.files_fetch.value, FilesFetchData,
        path=path, cap=get_settings().transfer_cap_bytes,
    )
    raw = base64.b64decode(data["content_b64"])
    filename = data.get("filename") or "download"
    return Response(
        content=raw,
        media_type="application/zip" if data.get("is_dir") else "application/octet-stream",
        headers={"content-disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/instances/{instance_id}/files/rename")
async def files_rename(instance_id: str, body: FileRename) -> dict:
    return await proxy(
        instance_id, Action.files_rename.value, FilesRenameData,
        path=body.path, new_name=body.new_name,
    )


@router.post("/instances/{instance_id}/files/extract")
async def files_extract(instance_id: str, body: FileExtract) -> dict:
    return await proxy(
        instance_id, Action.files_extract.value, FilesExtractData,
        path=body.path, overwrite=body.overwrite,
    )


# --- Live event stream (UI) ------------------------------------------------
@router.websocket("/nodes/{node_id}/events")
async def node_events(ws: WebSocket, node_id: str) -> None:
    """Forward a node's live agent events (console/state) to a UI client."""
    await ws.accept()
    stream = registry.stream(node_id)

    async def _watch_disconnect() -> None:
        # The UI never sends; this returns only on disconnect.
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            return

    watcher = asyncio.ensure_future(_watch_disconnect())
    try:
        while True:
            nxt = asyncio.ensure_future(stream.__anext__())
            done, _ = await asyncio.wait(
                {nxt, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if watcher in done:
                nxt.cancel()
                break
            event = nxt.result()
            await ws.send_json(event.model_dump(mode="json"))
    except (WebSocketDisconnect, StopAsyncIteration):
        pass
    finally:
        watcher.cancel()
        await stream.aclose()
