"""Map incoming protocol commands to agent actions and build responses.

Every action is one row in :data:`_HANDLERS`: the payload model it expects and
the coroutine that carries it out. The payload is validated against that model
before the handler runs, so a handler receives typed fields rather than indexing
a raw dict — a contract mismatch becomes one clear error instead of a ``KeyError``
surfacing as "KeyError: 'new_name'" in the UI.

Instance-scoped payloads carry the declared :class:`InstanceSpec` (the hub
attaches it), so the agent never keeps its own copy of instance config.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from pydantic import BaseModel, ValidationError

from minemanager_agent import archive, files, transfer
from minemanager_agent.supervisor import Supervisor
from minemanager_shared.protocol import (
    Action,
    Command,
    ConsoleSendData,
    FilesDeleteData,
    FilesExtractData,
    FilesFetchData,
    FilesListData,
    FilesReadData,
    FilesRenameData,
    FilesUploadData,
    FilesWriteData,
    InstanceStatesData,
    LogsTailData,
    PowerData,
    Response,
    TransferStartData,
    UpdateApplyData,
)

Handler = Callable[[BaseModel, Supervisor], Awaitable[dict]]


async def _off(fn, *args):
    """Run a blocking file operation off the event loop."""
    return await asyncio.to_thread(fn, *args)


# -- power ------------------------------------------------------------------
async def _power_start(d: PowerData, sup: Supervisor) -> dict:
    return await sup.start(d.instance)


async def _power_stop(d: PowerData, sup: Supervisor) -> dict:
    return await sup.stop(d.instance)


async def _power_restart(d: PowerData, sup: Supervisor) -> dict:
    return await sup.restart(d.instance)


async def _power_kill(d: PowerData, sup: Supervisor) -> dict:
    return await sup.kill(d.instance)


# -- console / logs ---------------------------------------------------------
async def _console_send(d: ConsoleSendData, sup: Supervisor) -> dict:
    return await sup.send(d.instance, d.line)


async def _logs_tail(d: LogsTailData, sup: Supervisor) -> dict:
    return await _off(files.tail_lines, d.instance.root_dir, d.path, d.lines)


# -- files (all jailed to the instance root) --------------------------------
async def _files_list(d: FilesListData, sup: Supervisor) -> dict:
    return {"entries": await _off(files.list_dir, d.instance.root_dir, d.path)}


async def _files_read(d: FilesReadData, sup: Supervisor) -> dict:
    limit = d.max_bytes or files.DEFAULT_EDITOR_MAX_BYTES
    return await _off(files.read_for_editor, d.instance.root_dir, d.path, limit)


async def _files_write(d: FilesWriteData, sup: Supervisor) -> dict:
    return await _off(files.write_file, d.instance.root_dir, d.path, d.content)


async def _files_upload(d: FilesUploadData, sup: Supervisor) -> dict:
    return await _off(files.write_bytes, d.instance.root_dir, d.path, d.content_b64)


async def _files_delete(d: FilesDeleteData, sup: Supervisor) -> dict:
    return await _off(files.delete, d.instance.root_dir, d.path, d.recursive)


async def _files_fetch(d: FilesFetchData, sup: Supervisor) -> dict:
    cap = d.cap or files.DEFAULT_TRANSFER_CAP_BYTES
    return await _off(files.fetch, d.instance.root_dir, d.path, cap)


async def _files_rename(d: FilesRenameData, sup: Supervisor) -> dict:
    return await _off(files.rename, d.instance.root_dir, d.path, d.new_name)


async def _files_extract(d: FilesExtractData, sup: Supervisor) -> dict:
    return await _off(archive.extract, d.instance.root_dir, d.path, d.overwrite)


# -- updater / transfers ----------------------------------------------------
async def _update_apply(d: UpdateApplyData, sup: Supervisor) -> dict:
    return await sup.apply_update(
        d.instance, d.jar_name, d.download.model_dump(mode="json"),
        allow_create=d.allow_create,
    )


async def _transfer_start(d: TransferStartData, sup: Supervisor) -> dict:
    if sup.identity is None:
        raise RuntimeError("agent identity not ready for transfers")
    return await transfer.handle(sup.identity, d, d.instance.root_dir)


# -- introspection ----------------------------------------------------------
async def _instance_states(d: InstanceStatesData, sup: Supervisor) -> dict:
    return {"states": await sup.states_for(d.ids)}


_HANDLERS: dict[str, tuple[type[BaseModel], Handler]] = {
    Action.power_start.value: (PowerData, _power_start),
    Action.power_stop.value: (PowerData, _power_stop),
    Action.power_restart.value: (PowerData, _power_restart),
    Action.power_kill.value: (PowerData, _power_kill),
    Action.console_send.value: (ConsoleSendData, _console_send),
    Action.logs_tail.value: (LogsTailData, _logs_tail),
    Action.files_list.value: (FilesListData, _files_list),
    Action.files_read.value: (FilesReadData, _files_read),
    Action.files_write.value: (FilesWriteData, _files_write),
    Action.files_upload.value: (FilesUploadData, _files_upload),
    Action.files_delete.value: (FilesDeleteData, _files_delete),
    Action.files_fetch.value: (FilesFetchData, _files_fetch),
    Action.files_rename.value: (FilesRenameData, _files_rename),
    Action.files_extract.value: (FilesExtractData, _files_extract),
    Action.update_apply.value: (UpdateApplyData, _update_apply),
    Action.transfer_start.value: (TransferStartData, _transfer_start),
    Action.instance_states.value: (InstanceStatesData, _instance_states),
}


def _brief(exc: ValidationError) -> str:
    """One readable line from a validation failure, not a wall of JSON."""
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'payload'}: {e['msg']}"
        for e in exc.errors()[:4]
    )


async def handle(cmd: Command, sup: Supervisor) -> Response:
    entry = _HANDLERS.get(cmd.action)
    if entry is None:
        return Response.failure(cmd.id, f"unknown action: {cmd.action}")
    model, handler = entry

    try:
        data = model.model_validate(cmd.data)
    except ValidationError as exc:
        return Response.failure(cmd.id, f"invalid payload for {cmd.action}: {_brief(exc)}")

    try:
        return Response.success(cmd.id, await handler(data, sup))
    except FileNotFoundError as exc:
        return Response.failure(cmd.id, f"not found: {exc}")
    except files.JailError as exc:
        return Response.failure(cmd.id, f"path rejected: {exc}")
    except archive.UnsupportedArchive as exc:
        return Response.failure(cmd.id, str(exc))
    except Exception as exc:  # noqa: BLE001 - surface any handler error to the hub
        return Response.failure(cmd.id, f"{type(exc).__name__}: {exc}")
