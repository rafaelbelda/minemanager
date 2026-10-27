"""REST CRUD for nodes and their instances.

Creating a node mints a one-time enrollment token (returned once). Instances
are declared here; the agent enacts them.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from minemanager_hub import roots, serverjar
from minemanager_hub.agents.registry import registry
from minemanager_hub.api.schemas import (
    EnrollmentOut,
    InstanceCreate,
    InstanceOut,
    InstanceUpdate,
    NodeCreate,
    NodeOut,
)
from minemanager_hub.config import get_settings
from minemanager_hub.db.models import Instance, Node
from minemanager_hub.db.session import session_scope
from minemanager_hub.security import tokens

router = APIRouter(prefix="/api", tags=["nodes"])


def _node_out(node: Node) -> NodeOut:
    return NodeOut(
        id=node.id,
        name=node.name,
        hostname=node.hostname,
        agent_version=node.agent_version,
        online=registry.is_online(node.id),
        last_seen=node.last_seen,
        enrolled=node.credential_hash is not None,
        root_dir=node.root_dir,
    )


def instance_out(inst: Instance) -> InstanceOut:
    return InstanceOut(
        id=inst.id,
        node_id=inst.node_id,
        name=inst.name,
        type=inst.type,
        root_dir=inst.root_dir,
        start_command=inst.start_command,
        jar_path=inst.jar_path,
        java_home=inst.java_home,
        start_with_node=inst.start_with_node,
        restart_policy=inst.restart_policy,
        version=inst.version,
        build=inst.build,
        outside_node_root=roots.is_stranded(inst.node.root_dir, inst.root_dir),
    )


# --- Nodes -----------------------------------------------------------------
@router.get("/nodes", response_model=list[NodeOut])
def list_nodes() -> list[NodeOut]:
    with session_scope() as db:
        return [_node_out(n) for n in db.query(Node).order_by(Node.created_at).all()]


@router.post("/nodes", response_model=EnrollmentOut, status_code=201)
def create_node(body: NodeCreate) -> EnrollmentOut:
    settings = get_settings()
    token = tokens.generate_token()
    with session_scope() as db:
        node = Node(
            name=body.name,
            enroll_token_hash=tokens.hash_token(token),
            enroll_expires_at=time.time() + settings.enrollment_ttl_s,
        )
        db.add(node)
        db.flush()
        node_id = node.id
    return EnrollmentOut(
        node_id=node_id,
        enrollment_token=token,
        expires_in_s=settings.enrollment_ttl_s,
    )


@router.post("/nodes/{node_id}/reenroll", response_model=EnrollmentOut)
async def reenroll_node(node_id: str) -> EnrollmentOut:
    """Mint a fresh enrollment token and revoke the current credential."""
    settings = get_settings()
    token = tokens.generate_token()
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        node.enroll_token_hash = tokens.hash_token(token)
        node.enroll_expires_at = time.time() + settings.enrollment_ttl_s
        node.credential_hash = None
    await registry.disconnect(node_id)
    return EnrollmentOut(
        node_id=node_id, enrollment_token=token, expires_in_s=settings.enrollment_ttl_s
    )


@router.delete("/nodes/{node_id}", status_code=204)
async def delete_node(node_id: str) -> None:
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        db.delete(node)
    await registry.disconnect(node_id)


def _checked_root(node_root: str | None, instance_root: str) -> str:
    """Keep a declared instance inside its node's root, when it has one."""
    try:
        return roots.check_under(node_root, instance_root)
    except roots.RootError as exc:
        raise HTTPException(400, str(exc)) from exc


# --- Instances -------------------------------------------------------------
@router.get("/nodes/{node_id}/instances", response_model=list[InstanceOut])
def list_instances(node_id: str) -> list[InstanceOut]:
    with session_scope() as db:
        if db.get(Node, node_id) is None:
            raise HTTPException(404, "node not found")
        rows = db.query(Instance).filter(Instance.node_id == node_id).all()
        return [instance_out(i) for i in rows]


@router.post("/nodes/{node_id}/instances", response_model=InstanceOut, status_code=201)
def create_instance(node_id: str, body: InstanceCreate) -> InstanceOut:
    """Declare an instance that already exists on the node."""
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        root_dir = _checked_root(node.root_dir, body.root_dir)
        inst = Instance(
            node_id=node_id,
            name=body.name,
            type=body.type.value,
            root_dir=root_dir,
            start_command=body.start_command or serverjar.build_start_command(
                body.type.value, body.memory
            ),
            jar_path=body.jar_path or None,
            java_home=body.java_home or None,
            restart_policy=body.restart_policy.value,
        )
        db.add(inst)
        db.flush()
        return instance_out(inst)


@router.patch("/instances/{instance_id}", response_model=InstanceOut)
def update_instance(instance_id: str, body: InstanceUpdate) -> InstanceOut:
    # Partial update of an instance's declared spec.

    changes = body.model_dump(mode="json", exclude_unset=True)
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(404, "instance not found")
        if "root_dir" in changes:
            changes["root_dir"] = _checked_root(inst.node.root_dir, changes["root_dir"])
        for field, value in changes.items():
            setattr(inst, field, value)
        db.flush()
        return instance_out(inst)


@router.delete("/instances/{instance_id}", status_code=204)
def delete_instance(instance_id: str) -> None:
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(404, "instance not found")
        db.delete(inst)
