"""Restore declared run state when an agent (re)connects.

The hub owns desired state; agents lose runtime state on restart. When an agent
process starts, send power.start to that node's instances marked
start_with_node. Starts are:

- Agent-restart-only, keyed on the handshake's per-process session_id. A redial
  of a live agent has nothing to re-enact.
- Start-only: never stop instances whose flag is false.
- Idempotent: power.start adopts existing sessions and restores logs.
- Serialised: staggered, to avoid loading every JVM at once.
- Failure-safe: a failed start leaves start_with_node armed, and the agent
  reports the failure through the event stream.

note_clean_exit() is the reverse direction: it disarms an instance the agent
watched shut itself down, so a console "stop" means what the UI's Stop means.
"""

from __future__ import annotations

import asyncio
import logging

from minemanager_hub.agents.registry import AgentConnection, CommandTimeout, registry
from minemanager_hub.api.proxy import spec_of
from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Instance
from minemanager_hub.db.session import session_scope
from minemanager_shared.protocol import Action, Event, PowerData

log = logging.getLogger("minemanager.hub")

# A start returns once the agent has created the session and waited out its
# settle window; it does not wait for the world to load. Generous, because a
# timeout here costs a server its restore.
_START_TIMEOUT_S = 60.0


def note_clean_exit(event: Event, node_id: str) -> None:
    """Disarm an instance the agent saw shut itself down on purpose.

    The agent reports an observation; the decision to change declared state is
    taken here, because the hub owns declared state. Only the reporting node's
    own instances are touched.

    The other paths that end a session do not set ``clean_exit``: a UI Stop
    clears the flag through its REST route, a UI Restart must not clear it, and
    a graceful agent shutdown emits no state events at all.
    """
    if event.action != Action.ev_state_changed.value or not event.instance_id:
        return
    if not event.data.get("clean_exit"):
        return
    with session_scope() as db:
        inst = db.get(Instance, event.instance_id)
        if inst is not None and inst.node_id == node_id and inst.start_with_node:
            inst.start_with_node = False
            log.info("%s was shut down from its console; will not start with the node", inst.name)


def pending_for(node_id: str) -> list[str]:
    """Ids of the node's instances declared to be running, in restore order.

    Ordered by name, so it is deterministic and an operator can impose an order
    by naming (``01-proxy``, ``02-lobby``). ``created_at`` is too coarse to
    order by.

    Not dependency ordering: Velocity refuses connections to a backend until it
    is up, so nothing here has to start before anything else.
    """
    with session_scope() as db:
        rows = (
            db.query(Instance)
            .filter(Instance.node_id == node_id, Instance.start_with_node.is_(True))
            .order_by(Instance.name, Instance.id)
            .all()
        )
        return [inst.id for inst in rows]


async def _start(conn: AgentConnection, instance_id: str) -> None:
    # Re-read rather than carry the row across the stagger: the instance may
    # have been disarmed or deleted meanwhile.
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None or not inst.start_with_node:
            return
        name, spec = inst.name, spec_of(inst)

    try:
        resp = await conn.call(
            Action.power_start.value,
            instance_id=instance_id,
            data=PowerData(instance=spec).model_dump(mode="json"),
            timeout=_START_TIMEOUT_S,
        )
    except (CommandTimeout, ConnectionError) as exc:
        log.warning("autostart %s: %s", name, exc)
        return

    if not resp.ok:
        log.warning("autostart %s: agent refused: %s", name, resp.error)
    elif resp.data.get("already_running"):
        log.info("autostart %s: already running; adopted", name)
    else:
        log.info("autostart %s: started", name)


async def restore(
    node_id: str, conn: AgentConnection, *, agent_restarted: bool = True
) -> None:
    """Start everything this node declares should be running.

    ``agent_restarted`` is the gate: false means the agent process never died,
    so its own supervisor has been minding those servers.

    Runs as a background task off the agent's WebSocket handler. Each
    ``power.start`` is answered by that handler's frame pump, so awaiting this
    inline would deadlock every command until it timed out.
    """
    if not agent_restarted:
        log.info("node %s: reconnect of a live agent; leaving run state alone", node_id)
        return
    if not get_settings().autostart:
        return
    try:
        pending = pending_for(node_id)
        if not pending:
            return
        log.info("node %s: restoring %d declared-running instance(s)", node_id, len(pending))
        stagger = get_settings().autostart_stagger_s
        for index, instance_id in enumerate(pending):
            if index:
                await asyncio.sleep(stagger)
            # Checked every pass: a reconnect that raced this one owns the node
            # and runs its own restore.
            if registry.get(node_id) is not conn:
                log.info("node %s: autostart abandoned, connection replaced", node_id)
                return
            await _start(conn, instance_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Nothing awaits this task, so an escaping exception would surface only
        # at GC time.
        log.exception("node %s: autostart failed", node_id)
