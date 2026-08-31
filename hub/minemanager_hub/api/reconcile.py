"""Restore declared run state when an agent (re)connects.

The hub owns desired state; agents lose runtime state on restart. When an agent
process starts, send power.start to that node's instances marked
start_with_node.

Starts are deliberately:

Agent-restart-only: not on every reconnect. A live agent supervises its own
servers, so a redial after a dropped link -- or after a hub restart, which
makes every agent redial at once -- has nothing to re-enact, and re-enacting
it would restart a server deliberately left down. The handshake carries a
per-process session_id for this.
Start-only: never stop instances whose flag is false.
Idempotent: power.start adopts existing sessions and restores logs.
Serialised: stagger starts to avoid loading all JVMs at once.
Failure-safe: failed starts leave start_with_node armed; the agent
reports the failure through the event stream.

No new protocol action is required, so older agents are supported.

The reverse direction lives here too: note_clean_exit() disarms an instance the
agent watched shut itself down, so "stop" typed at a console means what the UI's
Stop button means rather than lasting until the next reboot.
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
# settle window; it does not wait for the world to load. Generous anyway — the
# node has usually just booted, and a timeout here costs a server its restore.
_START_TIMEOUT_S = 60.0


def note_clean_exit(event: Event) -> None:
    """Disarm an instance the agent saw shut itself down on purpose.

    The split is deliberate: the agent reports an *observation* ("this exited
    cleanly, after having come up"), and the decision to change declared state
    is taken here, because the hub owns declared state. The agent never writes
    it.

    Safe against the paths that also end a session, none of which set the flag:
    a UI Stop already clears it through the REST route, a UI Restart must not
    clear it, and a graceful agent shutdown emits no state events at all — which
    matters most, since disarming there would mean nothing came back after a
    reboot, the exact failure this whole feature exists to prevent.
    """
    if event.action != Action.ev_state_changed.value or not event.instance_id:
        return
    if not event.data.get("clean_exit"):
        return
    with session_scope() as db:
        inst = db.get(Instance, event.instance_id)
        if inst is not None and inst.start_with_node:
            inst.start_with_node = False
            log.info("%s was shut down from its console; will not start with the node", inst.name)


def pending_for(node_id: str) -> list[str]:
    """Ids of the node's instances declared to be running, in restore order.

    By name, so the order is deterministic on every platform and an operator who
    cares can impose one by naming (``01-proxy``, ``02-lobby``). Creation order
    would read better but cannot be relied on: ``created_at`` comes from
    ``datetime.now()``, whose resolution is coarser than the time it takes to
    declare two instances, and a tie there falls back to a random uuid.

    This is not dependency ordering, and does not try to be. Velocity does not
    need its backends at boot — it just refuses connections to one until it is
    up — so nothing here has to start before anything else.
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
    # Re-read rather than carrying the row across the stagger: an operator may
    # have disarmed or deleted the instance while we were sleeping.
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

    ``agent_restarted`` is the gate: false means the agent process on the other
    end never died, so its own supervisor has been minding those servers the
    whole time and there is nothing here to restore.

    Runs as a background task off the agent's WebSocket handler and never
    raises: each ``power.start`` is answered by that handler's own frame pump,
    so awaiting this inline would deadlock every command until it timed out.
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
            # Checked every pass, not just once: a reconnect that raced this one
            # owns the node now and is running its own restore, so continuing
            # would send a second set of starts down the replaced socket.
            if registry.get(node_id) is not conn:
                log.info("node %s: autostart abandoned, connection replaced", node_id)
                return
            await _start(conn, instance_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Nothing awaits this task, so an escaping exception would surface only
        # as "task exception was never retrieved" at GC time, long after.
        log.exception("node %s: autostart failed", node_id)
