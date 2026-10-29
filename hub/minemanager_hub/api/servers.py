"""Create a brand-new server on a node.

The hub resolves the download from the software's provider and declares the
instance; the agent makes the directory, installs the jar, runs it once to
generate its configuration, and optionally starts it.

The request does not wait for the build, which takes minutes: it returns 202
with the declared row and the build continues in a task. Progress is the
agent's own ``state.changed`` events; the outcome is ``provision.done`` on the
node's event stream. A failure un-declares the instance again. Cleanup is the
task's, not the caller's, since the task outlives the request.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from minemanager_hub import roots, serverjar
from minemanager_hub.agents.registry import AgentConnection, registry
from minemanager_hub.api.nodes import instance_out
from minemanager_hub.api.proxy import call, spec_of
from minemanager_hub.api.schemas import InstanceOut, ServerCreate
from minemanager_hub.db.models import Instance, Node
from minemanager_hub.db.session import session_scope
from minemanager_hub.providers import Download, ProviderError, get_provider
from minemanager_shared.protocol import (
    Action,
    Event,
    InstanceCreateData,
    ProvisionDoneData,
    UpdateDownload,
)

log = logging.getLogger("minemanager.hub")

router = APIRouter(prefix="/api", tags=["servers"])

# Must exceed the agent's own budgets for a creation: download 300s
# (updater.DOWNLOAD_TIMEOUT_S) + first run 240s (provision.FIRST_RUN_TIMEOUT_S)
# + graceful stop 45s (supervisor._GRACEFUL_STOP_S) + settle windows.
CREATE_TIMEOUT_S = 900.0

NO_ROOT_DETAIL = (
    "this node has no root directory configured, so new servers cannot be created on "
    "it. Set MM_ROOT_DIR on the agent to the directory that should hold its servers "
    "(e.g. /srv/minecraft) and restart it."
)

# Strong references to the in-flight builds: an unreferenced task can be
# garbage-collected mid-await.
_running: set[asyncio.Task] = set()


@router.post("/nodes/{node_id}/servers", response_model=InstanceOut, status_code=202)
async def create_server(node_id: str, body: ServerCreate) -> InstanceOut:
    """Declare a new server and start building it. Returns the declared row."""
    software = body.type.value

    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        node_root = node.root_dir

    # Before the root check: the root is reported by the agent, so a node that
    # has never connected has none yet.
    conn = registry.get(node_id)
    if conn is None:
        raise HTTPException(409, "agent for this node is offline")

    if not node_root:
        raise HTTPException(409, NO_ROOT_DETAIL)

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
    try:
        with session_scope() as db:
            inst = Instance(
                node_id=node_id,
                name=body.name,
                type=software,
                root_dir=root_dir,
                start_command=serverjar.build_start_command(software, body.memory),
                jar_path=jar_name,
                java_home=body.java_home,
                restart_policy=body.restart_policy.value,
                start_with_node=body.run_after,
            )
            db.add(inst)
            db.flush()
            instance_id = inst.id
            spec = spec_of(inst)
            out = instance_out(inst)
    except IntegrityError as exc:
        # uq_instance_node_name. Caught rather than pre-read, so two concurrent
        # requests cannot both pass the check.
        raise HTTPException(
            409, f"this node already has an instance called {body.name!r}"
        ) from exc

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

    task = asyncio.create_task(
        _build(conn, node_id, instance_id, body.name, payload, download)
    )
    _running.add(task)
    task.add_done_callback(_running.discard)
    return out


async def _build(
    conn: AgentConnection,
    node_id: str,
    instance_id: str,
    name: str,
    payload: InstanceCreateData,
    download: Download,
) -> None:
    """Ask the agent to build the declared instance, and report the outcome.

    Catches everything: there is no caller to raise to, and an escaping
    exception would leave the instance declared but never built.
    """
    try:
        result = await call(
            conn, Action.instance_create.value, payload,
            instance_id=instance_id, timeout=CREATE_TIMEOUT_S,
        )
    except HTTPException as exc:
        # call() maps timeout, dropped agent and agent-reported failure to these.
        _fail(node_id, instance_id, name, str(exc.detail))
        return
    except asyncio.CancelledError:
        _fail(node_id, instance_id, name, "the hub stopped while this was being built")
        raise
    except Exception as exc:                          # noqa: BLE001 - see above

        _fail(node_id, instance_id, name, f"{type(exc).__name__}: {exc}")
        return

    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:                      # deleted while we were building
            log.warning("%s was built but had been deleted meanwhile", name)
            return
        inst.version = download.version
        inst.build = download.build
        if not result.get("started"):
            # Only a running server stays armed: one left stopped after its
            # configuration run must not come back on reboot.
            inst.start_with_node = False

    if result.get("start_error"):
        log.warning("%s was created but did not start: %s", name, result["start_error"])
    _publish(node_id, instance_id, ProvisionDoneData(ok=True, name=name))


def _fail(node_id: str, instance_id: str, name: str, detail: str) -> None:
    """Un-declare a creation that failed, and say why.

    The row goes and takes its console with it, so ``detail`` carries the
    agent's reason (including the server's last terminal output). Logged as
    well: nobody may be listening for the event.
    """
    log.warning("creating %s on node %s failed: %s", name, node_id, detail)
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            db.delete(inst)
    _publish(node_id, instance_id, ProvisionDoneData(ok=False, name=name, error=detail))


def _publish(node_id: str, instance_id: str, data: ProvisionDoneData) -> None:
    registry.publish(node_id, Event(
        action=Action.ev_provision_done.value,
        instance_id=instance_id,
        data=data.model_dump(mode="json"),
    ))
