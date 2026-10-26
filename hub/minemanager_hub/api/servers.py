"""Create a brand-new server on a node.

The hub resolves the download from the software's provider and declares the
instance; the agent makes the directory, installs the jar, runs it once to
generate its configuration, and optionally starts it. Nothing here duplicates
the updater or the providers.

The declared row is written before the agent is asked to build anything, so the
instance is visible while provisioning runs, and removed again if the agent
fails. A half-built server is never left declared.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from minemanager_hub import roots, serverjar
from minemanager_hub.agents.registry import registry
from minemanager_hub.api.nodes import instance_out
from minemanager_hub.api.proxy import call, spec_of
from minemanager_hub.api.schemas import InstanceOut, ServerCreate
from minemanager_hub.db.models import Instance, Node
from minemanager_hub.db.session import session_scope
from minemanager_hub.providers import ProviderError, get_provider
from minemanager_shared.protocol import Action, InstanceCreateData, UpdateDownload

log = logging.getLogger("minemanager.hub")

router = APIRouter(prefix="/api", tags=["servers"])

# Download, first run, and an optional start, each with its own budget on the
# agent. Must exceed their sum.
CREATE_TIMEOUT_S = 900.0

NO_ROOT_DETAIL = (
    "this node has no root directory configured, so new servers cannot be created on "
    "it. Set MM_ROOT_DIR on the agent to the directory that should hold its servers "
    "(e.g. /srv/minecraft) and restart it."
)


@router.post("/nodes/{node_id}/servers", response_model=InstanceOut, status_code=201)
async def create_server(node_id: str, body: ServerCreate) -> InstanceOut:
    """Download, install and initialise a new server under the node's root."""
    software = body.type.value

    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        node_root = node.root_dir
        taken = {i.name for i in db.query(Instance).filter(Instance.node_id == node_id)}

    # Offline first: the root is reported by the agent, so a node whose agent
    # has never connected has no root *because* it is offline. Saying "set
    # MM_ROOT_DIR" there sends the operator after the wrong thing.
    conn = registry.get(node_id)
    if conn is None:
        raise HTTPException(409, "agent for this node is offline")

    if not node_root:
        raise HTTPException(409, NO_ROOT_DETAIL)

    if body.name in taken:
        raise HTTPException(409, f"this node already has an instance called {body.name!r}")

    try:
        root_dir = roots.server_dir(node_root, body.name)
    except roots.RootError as exc:
        raise HTTPException(400, str(exc)) from exc

    provider = get_provider(software)
    if provider is None:
        raise HTTPException(404, f"no version provider for software {software!r}")
    try:
        download = await provider.resolve(body.version, body.build)
    except ProviderError as exc:
        raise HTTPException(400, f"could not resolve download: {exc}") from exc

    jar_name = serverjar.jar_for_type(software)
    with session_scope() as db:
        inst = Instance(
            node_id=node_id,
            name=body.name,
            type=software,
            root_dir=root_dir,
            start_command=serverjar.build_start_command(software, body.memory),
            jar_path=jar_name,
            restart_policy=body.restart_policy.value,
            start_with_node=body.run_after,
        )
        db.add(inst)
        db.flush()
        instance_id = inst.id
        spec = spec_of(inst)

    payload = InstanceCreateData(
        instance=spec,
        jar_name=jar_name,
        accept_eula=body.accept_eula,
        run_after=body.run_after,
        download=UpdateDownload(
            url=download.url,
            filename=download.filename,
            size=download.size,
            checksum=download.checksum,
            checksum_algo=download.checksum_algo,
            version=download.version,
            build=download.build,
        ),
    )

    try:
        result = await call(
            conn, Action.instance_create.value, payload,
            instance_id=instance_id, timeout=CREATE_TIMEOUT_S,
        )
    except HTTPException as exc:
        # The declared row goes, so the console that would explain this goes
        # with it. The agent folds the server's last terminal output into its
        # error for that reason; log the whole thing before dropping the row,
        # since the operator may only have the journal to go on.
        log.warning("creating %s on node %s failed: %s", body.name, node_id, exc.detail)
        _forget(instance_id)
        raise

    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:                      # deleted while we were provisioning
            raise HTTPException(409, "instance was removed while it was being created")
        inst.version = download.version
        inst.build = download.build
        if not result.get("started"):
            # Only an actually-running server stays armed; a creation that
            # stopped after its configuration run must not come back on reboot.
            inst.start_with_node = False
        out = instance_out(inst)

    if result.get("start_error"):
        log.warning("%s was created but did not start: %s", body.name, result["start_error"])
    return out


def _forget(instance_id: str) -> None:
    """Drop the declared row for a creation that failed on the agent."""
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            db.delete(inst)
