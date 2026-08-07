"""Version/build catalog endpoints and the update trigger.

The catalog (available versions/builds) is served by the hub from software
providers; the actual binary download + jar swap happens on the agent. This
module is provider-agnostic — it never mentions Paper/Vanilla/Velocity.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from minemanager_hub import serverjar
from minemanager_hub.api.proxy import agent_and_spec, call
from minemanager_hub.api.schemas import UpdateRequest
from minemanager_hub.db.models import Instance
from minemanager_hub.db.session import session_scope
from minemanager_hub.providers import ProviderError, get_provider
from minemanager_shared.protocol import Action, UpdateApplyData, UpdateDownload

router = APIRouter(prefix="/api", tags=["versions"])

# Must comfortably exceed the agent's *whole* update budget.
UPDATE_TIMEOUT_S = 650.0


def _provider_or_404(software: str):
    provider = get_provider(software)
    if provider is None:
        raise HTTPException(404, f"no version provider for software {software!r}")
    return provider


# --- Catalog ---------------------------------------------------------------
@router.get("/providers/{software}")
def provider_info(software: str) -> dict:
    p = _provider_or_404(software)
    return {"software": p.software, "label": p.label, "has_builds": p.has_builds}


@router.get("/providers/{software}/versions")
async def list_versions(software: str) -> dict:
    p = _provider_or_404(software)
    try:
        versions = await p.versions()
    except ProviderError as exc:
        raise HTTPException(502, f"could not fetch versions: {exc}") from exc
    return {
        "software": p.software,
        "label": p.label,
        "has_builds": p.has_builds,
        "versions": [{"id": v.id, "label": v.label, "channel": v.channel} for v in versions],
    }


@router.get("/providers/{software}/versions/{version}/builds")
async def list_builds(software: str, version: str) -> dict:
    p = _provider_or_404(software)
    try:
        builds = await p.builds(version)
    except ProviderError as exc:
        raise HTTPException(502, f"could not fetch builds: {exc}") from exc
    return {
        "software": p.software,
        "version": version,
        "builds": [{"id": b.id, "label": b.label, "channel": b.channel} for b in builds],
    }


# --- Update ----------------------------------------------------------------
@router.post("/instances/{instance_id}/update")
async def update_instance(instance_id: str, body: UpdateRequest) -> dict:
    """Resolve the requested version/build and have the agent install it.

    Guardrails (stopped-check, backup, atomic replace, rollback) live on the
    agent, which owns the process and the files. The hub only resolves the
    download and records the installed version on success. Refuses up front when
    the server executable cannot be identified — see
    :mod:`minemanager_hub.serverjar`.
    """
    conn, spec = agent_and_spec(instance_id)
    # jar_path is not part of the spec the agent receives, so it is read here.
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(404, "instance not found")
        executable = serverjar.resolve(inst.start_command, inst.jar_path)

    if executable.path is None:
        raise HTTPException(400, serverjar.CANNOT_RESOLVE_DETAIL)

    provider = _provider_or_404(spec.type.value)
    try:
        download = await provider.resolve(body.version, body.build)
    except ProviderError as exc:
        raise HTTPException(400, f"could not resolve download: {exc}") from exc

    payload = UpdateApplyData(
        instance=spec,
        jar_name=executable.path,
        allow_create=executable.allow_create,
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
    detail = await call(
        conn, Action.update_apply.value, payload,
        instance_id=instance_id, timeout=UPDATE_TIMEOUT_S,
    )

    # Record what we installed; stands in until a real version detector lands.
    with session_scope() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            inst.version = download.version
            inst.build = download.build

    return {
        "ok": True,
        "jar_source": executable.confidence,   # "explicit" | "parsed"
        "version": download.version,
        "build": download.build,
        "jar": executable.path,
        "detail": detail,
    }
