"""Resolving an instance to its live agent, and forwarding commands to it.

Every instance-scoped route goes through here, so there is one place that maps
an instance id to (connection, declared spec) and one place that turns agent
failures into HTTP status codes.

Command payloads are built by constructing the action's model from
``minemanager_shared.protocol`` rather than by assembling a dict, so a field
renamed in the protocol breaks here — at the call site, in the hub — instead of
reaching the agent and failing there.
"""

from __future__ import annotations

from fastapi import HTTPException

from minemanager_hub.agents.registry import AgentConnection, CommandTimeout, registry
from minemanager_hub.db.models import Instance
from minemanager_hub.db.session import session_scope
from minemanager_shared.protocol import InstanceCommand, InstanceSpec


def agent_and_spec(instance_id: str) -> tuple[AgentConnection, InstanceSpec]:
    """Resolve an instance to its live agent connection + declared spec.

    Raises 404 when the instance is unknown, 409 when its node is offline.
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
        )
        node_id = inst.node_id
    conn = registry.get(node_id)
    if conn is None:
        raise HTTPException(409, "agent for this instance is offline")
    return conn, spec


async def call(
    conn: AgentConnection,
    action: str,
    payload: InstanceCommand,
    *,
    instance_id: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Send a validated payload to an agent and unwrap its response."""
    try:
        resp = await conn.call(
            action,
            instance_id=instance_id,
            data=payload.model_dump(mode="json"),
            timeout=timeout,
        )
    except CommandTimeout as exc:
        raise HTTPException(504, str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not resp.ok:
        raise HTTPException(502, resp.error or "agent reported an error")
    return resp.data


async def proxy(
    instance_id: str,
    action: str,
    payload_cls: type[InstanceCommand],
    *,
    timeout: float = 30.0,
    **fields,
) -> dict:
    """Resolve, build the payload, and forward — the whole path for a route."""
    conn, spec = agent_and_spec(instance_id)
    payload = payload_cls(instance=spec, **fields)
    return await call(conn, action, payload, instance_id=instance_id, timeout=timeout)
