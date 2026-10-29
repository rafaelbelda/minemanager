"""Java runtimes per node: list, register, install, remove.

What is on a node is the agent's to say, so every listing asks it. The hub
stores only what the operator declared (manually registered paths) and sends
those along to be probed with the scan. Installs follow the server-jar pattern:
the hub resolves the download, the agent fetches, verifies and unpacks it.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from minemanager_hub.agents.registry import AgentConnection, registry
from minemanager_hub.api.proxy import call
from minemanager_hub.api.schemas import JavaInstall, JavaRegister
from minemanager_hub.db.models import Instance, JavaRuntime, Node
from minemanager_hub.db.session import session_scope
from minemanager_hub.providers import ProviderError
from minemanager_hub.providers.adoptium import resolve_jre
from minemanager_shared import java as compat
from minemanager_shared.protocol import (
    Action,
    JavaInstallData,
    JavaListData,
    JavaProbeData,
    JavaRemoveData,
    UpdateDownload,
)

router = APIRouter(prefix="/api", tags=["java"])

# Probing runs at most one short-lived `java` per runtime that lacks a release file.
LIST_TIMEOUT_S = 60.0
# Must exceed the agent's download budget plus unpacking.
INSTALL_TIMEOUT_S = 650.0

# Platforms Temurin ships .tar.gz runtimes for, which is what the agent unpacks.
_INSTALLABLE_OS = ("linux", "alpine-linux")


def _node_state(node_id: str) -> tuple[list[str], dict[Optional[str], list[str]]]:
    """(registered paths, java_home -> names of instances using it)."""
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        manual = [r.java_home for r in node.java_runtimes]
        used: dict[Optional[str], list[str]] = {}
        for inst in db.query(Instance).filter(Instance.node_id == node_id):
            used.setdefault(inst.java_home, []).append(inst.name)
    return manual, used


def _online(node_id: str) -> AgentConnection:
    conn = registry.get(node_id)
    if conn is None:
        raise HTTPException(409, "agent for this node is offline")
    return conn


async def _discover(conn: AgentConnection, manual: list[str]) -> dict:
    return await call(conn, Action.java_list.value, JavaListData(extra=manual),
                      timeout=LIST_TIMEOUT_S)


def _recommended(runtimes: list[dict], req: compat.Requirement | None) -> Optional[str]:
    """The runtime to suggest: the best major, and among equals the node's
    default first, then a managed one."""
    usable = [r for r in runtimes if not r.get("error") and r.get("major")]
    if req is None:
        return None
    best = compat.pick(req, [r["major"] for r in usable])
    if best is None:
        return None
    same = [r for r in usable if r["major"] == best]
    same.sort(key=lambda r: (not r.get("default"), r.get("source") != "managed"))
    return same[0]["home"]


@router.get("/java/compat")
def java_compat() -> dict:
    """The version table and the releases MineManager installs."""
    return {"managed": list(compat.MANAGED_FEATURES), "rules": compat.table()}


@router.get("/nodes/{node_id}/java")
async def list_java(node_id: str, software: Optional[str] = None,
                    version: Optional[str] = None) -> dict:
    """Runtimes on a node. With ``software`` and ``version``, each one also says
    whether it can run that server, and one is recommended."""
    manual, used = _node_state(node_id)
    req = compat.requirement(software or "", version)
    conn = registry.get(node_id)

    if conn is None:
        runtimes = [{"home": h, "source": "manual", "probed": False} for h in manual]
        platform, managed_dir = None, None
    else:
        found = await _discover(conn, manual)
        runtimes, platform = found.get("runtimes", []), found.get("platform")
        managed_dir = found.get("managed_dir")

    default_users = used.get(None, [])
    for r in runtimes:
        r["used_by"] = used.get(r["home"], []) + (default_users if r.get("default") else [])
        if req is not None and r.get("major") and not r.get("error"):
            r["compatible"] = compat.satisfies(req, r["major"])

    return {
        "online": conn is not None,
        "runtimes": runtimes,
        "platform": platform,
        "managed_dir": managed_dir,
        "installable": bool(platform and platform.get("arch")
                            and platform.get("os") in _INSTALLABLE_OS),
        "managed": list(compat.MANAGED_FEATURES),
        "requirement": None if req is None else {
            "minimum": req.minimum, "recommended": req.recommended, "legacy": req.legacy,
        },
        "recommended_home": _recommended(runtimes, req),
    }


@router.post("/nodes/{node_id}/java", status_code=201)
async def register_java(node_id: str, body: JavaRegister) -> dict:
    """Register a runtime by path. It must work on the node now."""
    _node_state(node_id)
    conn = _online(node_id)
    probed = await call(conn, Action.java_probe.value, JavaProbeData(java_home=body.java_home))
    runtime = probed.get("runtime") or {}
    if runtime.get("error"):
        raise HTTPException(400, f"{body.java_home}: {runtime['error']}")
    try:
        with session_scope() as db:
            db.add(JavaRuntime(node_id=node_id, java_home=body.java_home))
    except IntegrityError as exc:
        raise HTTPException(409, f"{body.java_home} is already registered") from exc
    return {"runtime": {**runtime, "source": "manual"}}


@router.delete("/nodes/{node_id}/java", status_code=204)
async def remove_java(node_id: str, java_home: str) -> None:
    """Forget a registered path, or delete a runtime MineManager installed.

    A registered path is only forgotten: the directory is not MineManager's.
    An installed one is refused while any instance is set to it.
    """
    _, used = _node_state(node_id)
    with session_scope() as db:
        row = db.query(JavaRuntime).filter_by(node_id=node_id, java_home=java_home).first()
        if row is not None:
            db.delete(row)
            return

    users = used.get(java_home, [])
    if users:
        raise HTTPException(409, f"in use by {', '.join(sorted(users))}; "
                                 "switch them to another Java first")
    conn = _online(node_id)
    await call(conn, Action.java_remove.value, JavaRemoveData(java_home=java_home))


@router.post("/nodes/{node_id}/java/install")
async def install_java(node_id: str, body: JavaInstall) -> dict:
    """Install (or update in place) a Temurin JRE on the node."""
    manual, _ = _node_state(node_id)
    conn = _online(node_id)
    platform = (await _discover(conn, manual)).get("platform") or {}
    if platform.get("os") not in _INSTALLABLE_OS:
        raise HTTPException(400, f"Java cannot be installed on {platform.get('os')!r} nodes")
    try:
        download = await resolve_jre(body.feature, platform["os"], platform.get("arch"))
    except ProviderError as exc:
        raise HTTPException(400, f"could not resolve Java {body.feature}: {exc}") from exc

    payload = JavaInstallData(
        feature=body.feature,
        download=UpdateDownload(
            url=download.url,
            filename=download.filename,
            size=download.size,
            checksum=download.checksum,
            checksum_algo=download.checksum_algo,
            version=download.version,
        ),
    )
    return await call(conn, Action.java_install.value, payload, timeout=INSTALL_TIMEOUT_S)
