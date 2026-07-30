"""Map incoming protocol commands to agent actions and build responses.

Instance-scoped commands carry the declared :class:`InstanceSpec` in
``cmd.data['instance']`` (the hub attaches it), so the agent never keeps its own
copy of instance config.
"""

from __future__ import annotations

import asyncio

from minemanager_agent import archive, files, rcon, transfer
from minemanager_agent.supervisor import Supervisor
from minemanager_shared.protocol import Action, Command, InstanceSpec, Response, RunState


def _spec(cmd: Command) -> InstanceSpec:
    raw = cmd.data.get("instance")
    if not raw:
        raise ValueError("command missing instance spec")
    return InstanceSpec.model_validate(raw)


async def _off(fn, *args):

    return await asyncio.to_thread(fn, *args)


async def handle(cmd: Command, sup: Supervisor) -> Response:
    try:
        return await _dispatch(cmd, sup)
    except FileNotFoundError as exc:
        return Response.failure(cmd.id, f"not found: {exc}")
    except files.JailError as exc:
        return Response.failure(cmd.id, f"path rejected: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface any handler error to the hub
        return Response.failure(cmd.id, f"{type(exc).__name__}: {exc}")


async def _dispatch(cmd: Command, sup: Supervisor) -> Response:
    action = cmd.action

    # -- power --------------------------------------------------------------
    if action == Action.power_start.value:
        return Response.success(cmd.id, await sup.start(_spec(cmd)))
    if action == Action.power_stop.value:
        return Response.success(cmd.id, await sup.stop(_spec(cmd)))
    if action == Action.power_restart.value:
        return Response.success(cmd.id, await sup.restart(_spec(cmd)))
    if action == Action.power_kill.value:
        return Response.success(cmd.id, await sup.kill(_spec(cmd)))

    # -- console ------------------------------------------------------------
    if action == Action.console_send.value:
        return Response.success(cmd.id, await sup.send(_spec(cmd), cmd.data["line"]))

    # -- files (jailed to instance root) ------------------------------------
    if action == Action.files_list.value:
        root = _spec(cmd).root_dir
        entries = await _off(files.list_dir, root, cmd.data.get("path", "."))
        return Response.success(cmd.id, {"entries": entries})
    if action == Action.files_read.value:
        root = _spec(cmd).root_dir
        max_bytes = int(cmd.data.get("max_bytes") or files.DEFAULT_EDITOR_MAX_BYTES)
        return Response.success(
            cmd.id, await _off(files.read_for_editor, root, cmd.data["path"], max_bytes)
        )
    if action == Action.files_write.value:
        root = _spec(cmd).root_dir
        return Response.success(
            cmd.id, await _off(files.write_file, root, cmd.data["path"], cmd.data["content"])
        )
    if action == Action.files_upload.value:
        root = _spec(cmd).root_dir
        return Response.success(
            cmd.id, await _off(files.write_bytes, root, cmd.data["path"], cmd.data["content_b64"])
        )
    if action == Action.files_delete.value:
        root = _spec(cmd).root_dir
        return Response.success(
            cmd.id,
            await _off(files.delete, root, cmd.data["path"], cmd.data.get("recursive", False)),
        )
    if action == Action.files_mkdir.value:
        root = _spec(cmd).root_dir
        return Response.success(cmd.id, await _off(files.mkdir, root, cmd.data["path"]))

    if action == Action.files_fetch.value:
        root = _spec(cmd).root_dir
        cap = int(cmd.data.get("cap") or files.DEFAULT_TRANSFER_CAP_BYTES)
        return Response.success(cmd.id, await _off(files.fetch, root, cmd.data["path"], cap))

    if action == Action.files_rename.value:
        root = _spec(cmd).root_dir
        return Response.success(
            cmd.id, await _off(files.rename, root, cmd.data["path"], cmd.data["new_name"])
        )

    if action == Action.files_extract.value:
        root = _spec(cmd).root_dir
        try:
            result = await _off(
                archive.extract, root, cmd.data["path"], bool(cmd.data.get("overwrite", False))
            )
        except archive.UnsupportedArchive as exc:
            return Response.failure(cmd.id, str(exc))
        return Response.success(cmd.id, result)

    # -- logs / console backfill --------------------------------------------
    if action == Action.logs_tail.value:
        root = _spec(cmd).root_dir
        path = cmd.data.get("path") or "logs/latest.log"
        lines = int(cmd.data.get("lines", 200))
        return Response.success(cmd.id, await _off(files.tail_lines, root, path, lines))

    # -- version updater ----------------------------------------------------
    if action == Action.update_apply.value:
        spec = _spec(cmd)
        jar_name = cmd.data.get("jar_name")
        download = cmd.data.get("download")
        if not jar_name or not download:
            return Response.failure(cmd.id, "update.apply requires jar_name and download")
        result = await sup.apply_update(
            spec, jar_name, download, allow_create=bool(cmd.data.get("allow_create", False))
        )
        return Response.success(cmd.id, result)

    # -- large-file streaming transfer --------------------------------------
    if action == Action.transfer_start.value:
        spec = _spec(cmd)
        if sup.identity is None:
            return Response.failure(cmd.id, "agent identity not ready for transfers")
        result = await transfer.handle(sup.identity, cmd.data, spec.root_dir)
        return Response.success(cmd.id, result)

    # -- rcon (secondary) ---------------------------------------------------
    if action == Action.rcon_command.value:
        spec = _spec(cmd)
        if not spec.rcon_port or not spec.rcon_password:
            return Response.failure(cmd.id, "RCON not configured for this instance")
        out = await rcon.execute(
            spec.rcon_host, spec.rcon_port, spec.rcon_password, cmd.data["command"]
        )
        return Response.success(cmd.id, {"output": out})

    # -- introspection ------------------------------------------------------
    if action == Action.instance_status.value:
        state = sup.states().get(cmd.instance_id or "", RunState.unknown)
        return Response.success(cmd.id, {"state": state.value})
    if action == Action.instance_states.value:
        ids = cmd.data.get("ids") or []
        return Response.success(cmd.id, {"states": await sup.states_for(ids)})
    if action == Action.node_info.value:
        return Response.success(
            cmd.id,
            {"uptime_s": sup.uptime_s(), "instances": {k: v.value for k, v in sup.states().items()}},
        )

    return Response.failure(cmd.id, f"unknown action: {action}")
