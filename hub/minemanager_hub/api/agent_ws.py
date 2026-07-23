"""The WebSocket endpoint agents dial into.

Flow per connection:
1. Agent sends a :class:`Hello` (enrollment token on first enroll, else the
   long-lived credential + node_id).
2. Hub authenticates, issues a credential on first enroll, replies :class:`Welcome`.
3. Hub registers an :class:`AgentConnection` and pumps frames: responses resolve
   waiting command futures; events are published to UI subscribers.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from minemanager_hub.agents.registry import AgentConnection, registry
from minemanager_hub.db.models import Node
from minemanager_hub.db.session import session_scope
from minemanager_hub.security import tokens
from minemanager_shared.protocol import (
    Event,
    Hello,
    Response,
    Welcome,
    parse_frame,
)
from minemanager_shared.version import PROTOCOL_VERSION

router = APIRouter()


def _authenticate(hello: Hello) -> tuple[str | None, str | None, str | None]:
    """Return ``(node_id, issued_credential, error)``.

    ``issued_credential`` is non-None only on a successful first enrollment.
    """
    if hello.protocol.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
        return None, None, f"protocol mismatch: hub={PROTOCOL_VERSION} agent={hello.protocol}"

    with session_scope() as db:
        # Reconnect path: node_id + credential.
        if hello.node_id and hello.credential:
            node = db.get(Node, hello.node_id)
            if node and tokens.verify_token(hello.credential, node.credential_hash):
                node.agent_version = hello.agent_version
                node.hostname = hello.hostname or node.hostname
                return node.id, None, None
            return None, None, "invalid credential"

        # First-enroll path: enrollment token identifies a pending node.
        if hello.enrollment_token:
            token_hash = tokens.hash_token(hello.enrollment_token)
            node = (
                db.query(Node)
                .filter(Node.enroll_token_hash == token_hash)
                .one_or_none()
            )
            if node is None:
                return None, None, "invalid enrollment token"
            if node.enroll_expires_at and node.enroll_expires_at < time.time():
                return None, None, "enrollment token expired"

            credential = tokens.generate_token()
            node.credential_hash = tokens.hash_token(credential)
            node.enroll_token_hash = None
            node.enroll_expires_at = None
            node.agent_version = hello.agent_version
            node.hostname = hello.hostname
            return node.id, credential, None

    return None, None, "missing credentials"


@router.websocket("/agent")
async def agent_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    # --- handshake ---
    try:
        raw = await ws.receive_json()
        frame = parse_frame(raw)
    except Exception:
        await ws.close(code=1002)
        return

    if not isinstance(frame, Hello):
        await ws.send_json(Welcome(ok=False, error="expected hello frame").model_dump(mode="json"))
        await ws.close(code=1002)
        return

    node_id, credential, error = _authenticate(frame)
    if error or node_id is None:
        await ws.send_json(Welcome(ok=False, error=error or "auth failed").model_dump(mode="json"))
        await ws.close(code=1008)
        return

    await ws.send_json(
        Welcome(ok=True, node_id=node_id, credential=credential).model_dump(mode="json")
    )

    conn = AgentConnection(node_id=node_id, send=ws.send_json)
    registry.register(conn)
    _touch_last_seen(node_id)

    # --- frame pump ---
    try:
        while True:
            raw = await ws.receive_json()
            try:
                frame = parse_frame(raw)
            except Exception:
                continue
            if isinstance(frame, Response):
                conn.resolve(frame)
            elif isinstance(frame, Event):
                registry.publish(node_id, frame)
                _touch_last_seen(node_id)
    except WebSocketDisconnect:
        pass
    finally:
        registry.unregister(node_id)


def _touch_last_seen(node_id: str) -> None:
    from datetime import datetime, timezone

    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is not None:
            node.last_seen = datetime.now(timezone.utc)
