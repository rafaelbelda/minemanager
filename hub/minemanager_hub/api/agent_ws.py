"""The WebSocket endpoint agents dial into.

Flow per connection:
1. Agent sends a :class:`Hello` carrying *everything* it holds — a stored
   ``node_id`` + credential and/or an enrollment token.
2. Hub arbitrates (see :func:`_authenticate`: a still-valid enrollment token
   wins, otherwise the stored credential), issues a credential when an
   enrollment succeeds, and replies :class:`Welcome`.
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

    ``issued_credential`` is non-None only when an enrollment just succeeded.

    Precedence: **a still-valid enrollment token wins over a stored credential.**
    Presenting one is an explicit operator action (the node was just created or
    re-minted), and it is the only way to recover a node whose stored credential
    has gone stale. An *already-used* token falls through to the stored
    credential, so a one-time token left behind in ``agent.env`` is harmless.

    The agent sends everything it holds and lets this function arbitrate;
    deciding it agent-side let a stale ``identity.json`` silently shadow
    ``MM_ENROLL_TOKEN``, with no way to recover but deleting a file that no error
    message ever named.
    """
    if hello.protocol.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
        return None, None, f"protocol mismatch: hub={PROTOCOL_VERSION} agent={hello.protocol}"

    enroll_error: str | None = None

    with session_scope() as db:
        # 1. Enrollment token, if one was supplied and is still usable.
        if hello.enrollment_token:
            token_hash = tokens.hash_token(hello.enrollment_token)
            node = (
                db.query(Node)
                .filter(Node.enroll_token_hash == token_hash)
                .one_or_none()
            )
            if node is None:
                enroll_error = "invalid enrollment token"
            elif node.enroll_expires_at and node.enroll_expires_at < time.time():
                enroll_error = "enrollment token expired"
            else:
                credential = tokens.generate_token()
                node.credential_hash = tokens.hash_token(credential)
                node.enroll_token_hash = None
                node.enroll_expires_at = None
                node.agent_version = hello.agent_version
                node.hostname = hello.hostname
                return node.id, credential, None

        # 2. Otherwise (or if the token was unusable) the stored identity.
        if hello.node_id and hello.credential:
            node = db.get(Node, hello.node_id)
            if node and tokens.verify_token(hello.credential, node.credential_hash):
                node.agent_version = hello.agent_version
                node.hostname = hello.hostname or node.hostname
                return node.id, None, None
            detail = f" ({enroll_error})" if enroll_error else ""
            return None, None, (
                f"stored credential for node {hello.node_id} was rejected{detail} - that node "
                "may have been deleted or re-enrolled. Mint a fresh enrollment token and set "
                "MM_ENROLL_TOKEN, or delete the agent's identity.json"
            )

    if enroll_error:
        return None, None, enroll_error
    return None, None, "no enrollment token and no stored identity - set MM_ENROLL_TOKEN"


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
    _touch_last_seen(node_id, force=True)

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
        # Only tear down the registry entry if it is still *ours*. Half-open TCP
        # is normal here (WireGuard, sleeping laptops, NAT): the agent can
        # reconnect and register a replacement before this socket's disconnect is
        # noticed, and unregistering unconditionally would evict the live
        # connection and strand a healthy node as "offline".
        if registry.get(node_id) is conn:
            registry.unregister(node_id)
            _LAST_SEEN_WRITE.pop(node_id, None)
        else:
            conn.fail_all(ConnectionError("agent connection replaced by a newer one"))


# last_seen is refreshed from the event stream, which carries one event *per
# console line*. Writing (and committing) on every one turned a chatty server
# into hundreds of serialized SQLite fsyncs per second on the event loop, so
# only the first write in each window actually touches the DB.
_LAST_SEEN_THROTTLE_S = 10.0
_LAST_SEEN_WRITE: dict[str, float] = {}


def _touch_last_seen(node_id: str, *, force: bool = False) -> None:
    from datetime import datetime, timezone

    now = time.monotonic()
    if not force and now - _LAST_SEEN_WRITE.get(node_id, 0.0) < _LAST_SEEN_THROTTLE_S:
        return
    _LAST_SEEN_WRITE[node_id] = now

    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is not None:
            node.last_seen = datetime.now(timezone.utc)
