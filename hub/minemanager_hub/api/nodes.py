"""REST CRUD for nodes and their instances.

Creating a node mints a one-time enrollment token (returned once). Instances
are declared here; the agent enacts them.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

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
from minemanager_hub.db.models import Instance, Node, Secret
from minemanager_hub.db.session import session_scope
from minemanager_hub.security import tokens

router = APIRouter(prefix="/api", tags=["nodes"])


def _delete_secrets_for(db, scope: str, scope_ids: list[str]) -> None:
    """Drop encrypted secrets belonging to deleted nodes/instances."""
    if not scope_ids:
        return
    (
        db.query(Secret)
        .filter(Secret.scope == scope, Secret.scope_id.in_(scope_ids))
        .delete(synchronize_session=False)
    )


def _node_out(node: Node) -> NodeOut:
    return NodeOut(
        id=node.id,
        name=node.name,
        hostname=node.hostname,
        agent_version=node.agent_version,
        online=registry.is_online(node.id),
        last_seen=node.last_seen,
        enrolled=node.credential_hash is not None,
    )


def _instance_out(inst: Instance) -> InstanceOut:
    return InstanceOut(
        id=inst.id,
        node_id=inst.node_id,
        name=inst.name,
        type=inst.type,
        root_dir=inst.root_dir,
        start_command=inst.start_command,
        jar_path=inst.jar_path,
        java_home=inst.java_home,
        desired_running=inst.desired_running,
        auto_restart=inst.auto_restart,
        version=inst.version,
        build=inst.build,
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
def reenroll_node(node_id: str) -> EnrollmentOut:
    """Mint a fresh enrollment token (e.g. after re-imaging a box)."""
    settings = get_settings()
    token = tokens.generate_token()
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        node.enroll_token_hash = tokens.hash_token(token)
        node.enroll_expires_at = time.time() + settings.enrollment_ttl_s
        node.credential_hash = None
    return EnrollmentOut(
        node_id=node_id, enrollment_token=token, expires_in_s=settings.enrollment_ttl_s
    )


@router.delete("/nodes/{node_id}", status_code=204)
def delete_node(node_id: str) -> None:
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        # Secrets are scoped by (scope, scope_id) with no FK, so nothing cascades to them
        instance_ids = [i.id for i in node.instances]
        _delete_secrets_for(db, "node", [node_id])
        _delete_secrets_for(db, "instance", instance_ids)
        db.delete(node)


# --- Instances -------------------------------------------------------------
@router.get("/nodes/{node_id}/instances", response_model=list[InstanceOut])
def list_instances(node_id: str) -> list[InstanceOut]:
    with session_scope() as db:
        if db.get(Node, node_id) is None:
            raise HTTPException(404, "node not found")
        rows = db.query(Instance).filter(Instance.node_id == node_id).all()
        return [_instance_out(i) for i in rows]


@router.post("/nodes/{node_id}/instances", response_model=InstanceOut, status_code=201)
def create_instance(node_id: str, body: InstanceCreate) -> InstanceOut:
    with session_scope() as db:
        if db.get(Node, node_id) is None:
            raise HTTPException(404, "node not found")
        inst = Instance(
            node_id=node_id,
            name=body.name,
            type=body.type.value,
            root_dir=body.root_dir,
            start_command=body.start_command,
            jar_path=body.jar_path or None,
            java_home=body.java_home or None,
            auto_restart=body.auto_restart,
        )
        db.add(inst)
        db.flush()
        return _instance_out(inst)


@router.patch("/instances/{instance_id}", response_model=InstanceOut)
def update_instance(instance_id: str, body: InstanceUpdate) -> InstanceOut:
    """Partial update of an instance's declared spec.

    Only fields present in the request are changed. Changes to ``root_dir`` or
    ``start_command`` take effect on the instance's next start — a running
    session keeps the spec it was launched with.
    """
    changes = body.model_dump(exclude_unset=True)
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(404, "instance not found")
        if "type" in changes and changes["type"] is not None:
            changes["type"] = changes["type"].value  # enum -> stored string
        for field, value in changes.items():
            setattr(inst, field, value)
        db.flush()
        return _instance_out(inst)


@router.delete("/instances/{instance_id}", status_code=204)
def delete_instance(instance_id: str) -> None:
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(404, "instance not found")
        _delete_secrets_for(db, "instance", [instance_id])
        db.delete(inst)
