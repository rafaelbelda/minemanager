"""Control-plane routes: proxy power/console/file commands to the owning agent,
manage per-instance secrets, and stream a node's live events to the UI.

Each command looks up the instance's node, finds its live agent connection, and
forwards the command over the WebSocket, awaiting the correlated response.
"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect

from minemanager_hub.agents.registry import AgentConnection, CommandTimeout, registry
from minemanager_hub.api.schemas import (
    ConsoleSend,
    FileExtract,
    FileRename,
    FileUpload,
    FileWrite,
    SecretSet,
)
from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Instance, Secret
from minemanager_hub.db.session import session_scope
from minemanager_hub.security import vault
from minemanager_shared.protocol import Action, InstanceSpec

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


def _lookup_secret(db, instance_id: str, key: str) -> str | None:
    row = (
        db.query(Secret)
        .filter(Secret.scope == "instance", Secret.scope_id == instance_id, Secret.key == key)
        .one_or_none()
    )
    return vault.decrypt(row.ciphertext) if row is not None else None


def _agent_and_spec(instance_id: str, *, with_rcon_password: bool = False) -> tuple[AgentConnection, dict]:
    """Resolve an instance to its live agent connection + declared spec.

    The spec is attached to every instance-scoped command so the agent stays
    stateless about config — but the **RCON password is only included when the
    command actually needs it** (``rcon.command``). It used to ride along on
    every action, so browsing a directory shipped the plaintext password across
    the wire dozens of times for no functional reason. `PLAN.md §5` names in-app
    secrets as the one asset this system owns; minimising how often they move is
    part of that.
    """
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(404, "instance not found")
        spec = InstanceSpec(
            id=inst.id,
            type=inst.type,
            name=inst.name,
            root_dir=inst.root_dir,
            start_command=inst.start_command,
            java_home=inst.java_home,
            auto_restart=inst.auto_restart,
            rcon_host=inst.rcon_host,
            rcon_port=inst.rcon_port,
            rcon_password=(
                _lookup_secret(db, instance_id, "rcon_password") if with_rcon_password else None
            ),
        ).model_dump(mode="json")
        node_id = inst.node_id
    conn = registry.get(node_id)
    if conn is None:
        raise HTTPException(409, "agent for this instance is offline")
    return conn, spec


async def _proxy(
    instance_id: str, action: str, data: dict | None = None, *, with_rcon_password: bool = False
) -> dict:
    conn, spec = _agent_and_spec(instance_id, with_rcon_password=with_rcon_password)
    payload = dict(data or {})
    payload["instance"] = spec
    try:
        resp = await conn.call(action, instance_id=instance_id, data=payload)
    except CommandTimeout as exc:
        raise HTTPException(504, str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not resp.ok:
        raise HTTPException(502, resp.error or "agent reported an error")
    return resp.data


# --- Batch run-state (fast initial load) -----------------------------------
@router.get("/nodes/{node_id}/instance-states")
async def node_instance_states(node_id: str) -> dict:
    """Real run-state for all of a node's instances, straight from the agent's
    tmux view. Lets the UI show stopped/running immediately on load instead of
    waiting for the first heartbeat. Offline node → empty (UI keeps 'unknown')."""
    conn = registry.get(node_id)
    if conn is None:
        return {"states": {}}
    with session_scope() as db:
        ids = [i.id for i in db.query(Instance).filter(Instance.node_id == node_id).all()]
    if not ids:
        return {"states": {}}
    try:
        resp = await conn.call(Action.instance_states.value, data={"ids": ids}, timeout=15)
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

    result = await _proxy(instance_id, action.value)

    # Record desired state for start/stop, but only once the agent has actually
    # accepted the command — writing it first left the DB claiming "should be
    # running" after a 409 (agent offline), 502 or 504.
    # NOTE (v1 gap): the hub does not yet push desired state back to an agent on
    # reconnect, so this is bookkeeping for the UI, not reconciliation. See PLAN.md.
    if op in ("start", "stop"):
        with session_scope() as db:
            inst = db.get(Instance, instance_id)
            if inst is not None:
                inst.desired_running = op == "start"

    return result


# --- Console ---------------------------------------------------------------
@router.post("/instances/{instance_id}/console")
async def console_send(instance_id: str, body: ConsoleSend) -> dict:
    return await _proxy(instance_id, Action.console_send.value, {"line": body.line})


@router.get("/instances/{instance_id}/console/history")
async def console_history(instance_id: str, lines: int = 200) -> dict:
    """Recent console lines from the instance's log, to backfill a fresh UI
    session before the live stream takes over. Read-only; best-effort."""
    lines = max(1, min(lines, 1000))
    return await _proxy(instance_id, Action.logs_tail.value, {"lines": lines})


# --- Files -----------------------------------------------------------------
@router.get("/instances/{instance_id}/files")
async def files_list(instance_id: str, path: str = ".") -> dict:
    return await _proxy(instance_id, Action.files_list.value, {"path": path})


@router.get("/instances/{instance_id}/files/read")
async def files_read(instance_id: str, path: str) -> dict:
    return await _proxy(
        instance_id, Action.files_read.value,
        {"path": path, "max_bytes": get_settings().editor_max_bytes},
    )


@router.post("/instances/{instance_id}/files/write")
async def files_write(instance_id: str, body: FileWrite) -> dict:
    return await _proxy(
        instance_id, Action.files_write.value, {"path": body.path, "content": body.content}
    )


@router.delete("/instances/{instance_id}/files")
async def files_delete(instance_id: str, path: str, recursive: bool = False) -> dict:
    return await _proxy(
        instance_id, Action.files_delete.value, {"path": path, "recursive": recursive}
    )


@router.post("/instances/{instance_id}/files/upload")
async def files_upload(instance_id: str, body: FileUpload) -> dict:
    """Simple (non-streaming) upload for normal-sized files. Larger files use the
    streaming transfer path (separate feature)."""
    cap = get_settings().transfer_cap_bytes
    approx_bytes = len(body.content_b64) * 3 // 4
    if approx_bytes > cap:
        raise HTTPException(413, f"file exceeds the {cap}-byte simple-upload limit — use the large transfer")
    return await _proxy(
        instance_id, Action.files_upload.value,
        {"path": body.path, "content_b64": body.content_b64},
    )


@router.get("/instances/{instance_id}/files/download")
async def files_download(instance_id: str, path: str) -> Response:
    """Download a file, or a directory as a zip. Bytes come from the agent
    (base64) and are returned as a browser file download."""
    data = await _proxy(
        instance_id, Action.files_fetch.value,
        {"path": path, "cap": get_settings().transfer_cap_bytes},
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
    return await _proxy(
        instance_id, Action.files_rename.value,
        {"path": body.path, "new_name": body.new_name},
    )


@router.post("/instances/{instance_id}/files/extract")
async def files_extract(instance_id: str, body: FileExtract) -> dict:
    return await _proxy(
        instance_id, Action.files_extract.value,
        {"path": body.path, "overwrite": body.overwrite},
    )


# --- Secrets ---------------------------------------------------------------
@router.put("/instances/{instance_id}/secrets", status_code=204)
def set_secret(instance_id: str, body: SecretSet) -> None:
    """Store/replace an encrypted secret for this instance. Write-only via API."""
    with session_scope() as db:
        if db.get(Instance, instance_id) is None:
            raise HTTPException(404, "instance not found")
        existing = (
            db.query(Secret)
            .filter(
                Secret.scope == "instance",
                Secret.scope_id == instance_id,
                Secret.key == body.key,
            )
            .one_or_none()
        )
        ciphertext = vault.encrypt(body.value)
        if existing is None:
            db.add(
                Secret(
                    scope="instance",
                    scope_id=instance_id,
                    key=body.key,
                    ciphertext=ciphertext,
                )
            )
        else:
            existing.ciphertext = ciphertext


@router.get("/instances/{instance_id}/secrets")
def list_secret_keys(instance_id: str) -> dict:
    """List which secret keys are set — never returns plaintext values."""
    with session_scope() as db:
        rows = (
            db.query(Secret)
            .filter(Secret.scope == "instance", Secret.scope_id == instance_id)
            .all()
        )
        return {"keys": [r.key for r in rows]}


# --- Live event stream (UI) ------------------------------------------------
@router.websocket("/nodes/{node_id}/events")
async def node_events(ws: WebSocket, node_id: str) -> None:
    """Forward a node's live agent events (console/log/state) to a UI client.

    The client receives every event for the node (each carries ``instance_id``,
    so the UI filters client-side). We race the event stream against a receive
    so a client disconnect is noticed promptly even when no events are flowing,
    and the subscription is always torn down.
    """
    await ws.accept()
    stream = registry.stream(node_id)

    async def _watch_disconnect() -> None:
        # The UI isn't expected to send anything; this returns on disconnect.
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
        await stream.aclose()  # runs the subscription's cleanup deterministically
