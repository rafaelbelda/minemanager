"""Map incoming protocol commands to agent actions and build responses.

Instance-scoped commands carry the declared :class:`InstanceSpec` in
``cmd.data['instance']`` (the hub attaches it), so the agent never keeps its own
copy of instance config.
"""

from __future__ import annotations

from minemanager_agent import files, rcon
from minemanager_agent.supervisor import Supervisor
from minemanager_shared.protocol import Action, Command, InstanceSpec, Response, RunState


def _spec(cmd: Command) -> InstanceSpec:
    raw = cmd.data.get("instance")
    if not raw:
        raise ValueError("command missing instance spec")
    return InstanceSpec.model_validate(raw)


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
        return Response.success(cmd.id, {"entries": files.list_dir(root, cmd.data.get("path", "."))})
    if action == Action.files_read.value:
        root = _spec(cmd).root_dir
        return Response.success(cmd.id, {"content": files.read_file(root, cmd.data["path"])})
    if action == Action.files_write.value:
        root = _spec(cmd).root_dir
        return Response.success(cmd.id, files.write_file(root, cmd.data["path"], cmd.data["content"]))
    if action == Action.files_upload.value:
        root = _spec(cmd).root_dir
        return Response.success(cmd.id, files.write_bytes(root, cmd.data["path"], cmd.data["content_b64"]))
    if action == Action.files_delete.value:
        root = _spec(cmd).root_dir
        return Response.success(
            cmd.id, files.delete(root, cmd.data["path"], cmd.data.get("recursive", False))
        )
    if action == Action.files_mkdir.value:
        root = _spec(cmd).root_dir
        return Response.success(cmd.id, files.mkdir(root, cmd.data["path"]))

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
    if action == Action.node_info.value:
        return Response.success(
            cmd.id,
            {"uptime_s": sup.uptime_s(), "instances": {k: v.value for k, v in sup.states().items()}},
        )

    return Response.failure(cmd.id, f"unknown action: {action}")
