"""The WebSocket endpoint agents dial into.

Flow per connection:
1. Agent sends a :class:`Hello` carrying everything it holds: a stored
   ``node_id`` + credential and/or an enrollment token.
2. Hub arbitrates (see :func:`_authenticate`: a still-valid enrollment token
   wins, otherwise the stored credential), issues a credential when an
   enrollment succeeds, and replies :class:`Welcome`.
3. Hub registers an :class:`AgentConnection` and pumps frames: responses resolve
   waiting command futures; events are published to UI subscribers.
4. If this handshake is a new agent process (see :func:`_claim_agent_session`),
   the node's declared run state is restored in parallel: instances marked
   ``start_with_node`` are started again. A reconnect of an agent that never
   died restores nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from minemanager_hub import roots
from minemanager_hub.agents.registry import AgentConnection, registry
from minemanager_hub.api import reconcile
from minemanager_hub.db.models import Instance, Node
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

log = logging.getLogger("minemanager.hub")

router = APIRouter()


def _authenticate(hello: Hello) -> tuple[str | None, str | None, str | None]:
    # Return ``(node_id, issued_credential, error)``.
    # "issued_credential" is non-None only when an enrollment just succeeded.
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
                node.root_dir = hello.root_dir
                return node.id, credential, None

        # 2. Otherwise (or if the token was unusable) the stored identity.
        if hello.node_id and hello.credential:
            node = db.get(Node, hello.node_id)
            if node and tokens.verify_token(hello.credential, node.credential_hash):
                node.agent_version = hello.agent_version
                node.hostname = hello.hostname or node.hostname
                # Every handshake: the hub must not hold a root the agent no
                # longer enforces.
                node.root_dir = hello.root_dir
                return node.id, None, None
            detail = f" ({enroll_error})" if enroll_error else ""
            return None, None, (
                f"stored credential for node {hello.node_id} was rejected{detail}. "
                "Mint a fresh enrollment token and set MM_ENROLL_TOKEN, or delete the agent's identity.json"
            )

    if enroll_error:
        return None, None, enroll_error
    return None, None, "no enrollment token and no stored identity - set MM_ENROLL_TOKEN"


def _warn_about_stranded_instances(node_id: str) -> None:
    """Say which declared instances this node's root does not cover.

    Setting MM_ROOT_DIR on a node that already has servers elsewhere cuts off
    power, console and files for them. The REST layer reports it per instance
    (``InstanceOut.outside_node_root``); this is the journal line.
    """
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None or not node.root_dir:
            return                      # no root: nothing is enforced anywhere
        node_root = node.root_dir
        stranded = sorted(
            i.name for i in db.query(Instance).filter(Instance.node_id == node_id)
            if roots.is_stranded(node_root, i.root_dir)
        )
    if stranded:
        log.warning(
            "node %s reports root %s, which does not contain %s. The agent will refuse "
            "every command for %s until each one's root directory is moved under %s or "
            "MM_ROOT_DIR is widened.",
            node_id, node_root, ", ".join(stranded),
            "it" if len(stranded) == 1 else "them", node_root,
        )


def _claim_agent_session(node_id: str, session_id: str | None) -> bool:
    """Record the connecting agent process; report whether it is a new one.

    True means the agent restarted, the only thing that should trigger a
    restore: while an agent stays up it supervises its own servers.

    A missing ``session_id`` is an agent predating the field, treated as a new
    process: restoring twice is recoverable, never restoring is not.
    """
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:                       # deleted between auth and here
            return False
        if session_id is None:
            node.agent_session_id = None
            return True
        restarted = node.agent_session_id != session_id
        node.agent_session_id = session_id
        return restarted


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

    restarted = _claim_agent_session(node_id, frame.session_id)
    _warn_about_stranded_instances(node_id)   # after the root was mirrored

    conn = AgentConnection(node_id=node_id, send=ws.send_json)
    registry.register(conn)
    _touch_last_seen(node_id, force=True)

    # Restore declared run state alongside the pump, never before it: every
    # ``power.start`` it sends is answered by a response frame *this loop* has
    # to read, so awaiting the restore here would deadlock it until timeout.
    restore = asyncio.create_task(
        reconcile.restore(node_id, conn, agent_restarted=restarted)
    )

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
                # Read before fan-out: an event can carry a declared-state
                # consequence, and subscribers are UI sockets that cannot
                # apply it.
                reconcile.note_clean_exit(frame)
                registry.publish(node_id, frame)
                _touch_last_seen(node_id)
    except WebSocketDisconnect:
        pass
    finally:
        # Nothing will read this socket again, so a restore still working
        # through its stagger would send starts nobody can answer.
        restore.cancel()
        # Only tear down the registry entry if it is still ours. Half-open TCP
        # is normal here (WireGuard, sleeping laptops, NAT), so a reconnect can
        # register a replacement before this disconnect is noticed.
        if registry.get(node_id) is conn:
            registry.unregister(node_id)
            _LAST_SEEN_WRITE.pop(node_id, None)
        else:
            conn.fail_all(ConnectionError("agent connection replaced by a newer one"))


# last_seen is refreshed from the event stream, which carries one event per
# console line, so only the first write per window lands.
_LAST_SEEN_THROTTLE_S = 10.0
_LAST_SEEN_WRITE: dict[str, float] = {}


def _touch_last_seen(node_id: str, *, force: bool = False) -> None:
    now = time.monotonic()
    if not force and now - _LAST_SEEN_WRITE.get(node_id, 0.0) < _LAST_SEEN_THROTTLE_S:
        return
    _LAST_SEEN_WRITE[node_id] = now

    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is not None:
            node.last_seen = datetime.now(timezone.utc)
