"""Create a new server inside the node root.

The sequence is: make the directory, install the jar through
:mod:`minemanager_agent.updater`, run the server once so it writes its own
configuration, accept the EULA, and optionally start it for real.

The configuration run deliberately happens *before* ``eula.txt`` is written.
Paper and Vanilla stop at the EULA gate after writing ``server.properties``,
which is what keeps the first run from generating a world; writing the accepted
EULA first would let that same run carry straight on into world generation.
Velocity has no such gate, so it is stopped once ``velocity.toml`` appears.

Anything that fails leaves nothing behind: the session is killed, everything
written into the directory is removed, and the directory itself goes too unless
it was already there. The directory is required to be empty up front precisely
so that "everything in it" is unambiguous.

A failure also carries the server's last terminal output in its message. The
hub drops the declared instance when creation fails, so there is no console left
to look at -- and for a launch that dies before its logger starts, a JVM version
mismatch being the common case, the pane is the only record there ever was.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from pathlib import Path

from minemanager_agent import files, tmux, updater
from minemanager_shared.protocol import EULA_TYPES, InstanceSpec, UpdateDownload

log = logging.getLogger("minemanager.agent")

#: Files each platform writes on its first run, and which must exist afterwards.
CONFIG_FILES = {
    "paper": ("server.properties",),
    "vanilla": ("server.properties",),
    "velocity": ("velocity.toml",),
}

#: Written for the platforms in ``EULA_TYPES``; see there for which, and why the
#: list is shared with the hub rather than kept here.
EULA_CONTENT = "eula=true\n"

#: Lines of pane output attached to a failure. The interesting part of a JVM
#: refusing to run is always the last few, and this has to fit in an HTTP error.
_ERROR_TAIL_LINES = 20

FIRST_RUN_TIMEOUT_S = 240.0
_POLL_S = 0.5
#: Grace after a proxy's config appears, so it finishes writing before we stop it.
_SETTLE_S = 2.0


class ProvisionError(Exception):
    """Creation could not be completed. Nothing usable was left behind."""


async def _failed(sup, spec: InstanceSpec, message: str) -> ProvisionError:
    """``message``, with the pane's last output appended when there is any.

    Returned rather than raised so the call site keeps its ``raise``.
    """
    lines = await sup.pty_tail(spec.id)
    if not lines:
        return ProvisionError(message)
    tail = "\n".join(lines[-_ERROR_TAIL_LINES:])
    return ProvisionError(f"{message}\n--- last terminal output ---\n{tail}")


async def create(
    sup,
    spec: InstanceSpec,
    jar_name: str,
    download: UpdateDownload,
    *,
    accept_eula: bool = False,
    run_after: bool = False,
) -> dict:
    """Provision a new server and report what was done."""
    if sup.node_root is None:
        raise ProvisionError(
            "this node has no root directory configured (MM_ROOT_DIR), so servers "
            "cannot be created on it"
        )
    kind = spec.type.value
    if kind not in CONFIG_FILES:
        raise ProvisionError(f"unsupported server type: {kind}")
    if spec.type in EULA_TYPES and not accept_eula:
        raise ProvisionError("the Minecraft EULA must be accepted before creating a server")

    root = files.resolve_instance_root(sup.node_root, spec.root_dir)
    existed = root.exists()
    if existed and not root.is_dir():
        raise ProvisionError(f"{root} exists and is not a directory")
    # Empty is the precondition the cleanup path relies on: everything in the
    # directory afterwards is something this flow put there.
    if existed and any(root.iterdir()):
        raise ProvisionError(f"{root} already exists and is not empty")

    with sup.provisioning(spec.id):
        try:
            root.mkdir(parents=True, exist_ok=True)
            install = await updater.apply_update(
                str(root), jar_name, download.model_dump(mode="json"), allow_create=True
            )
            generated = await _generate_config(sup, spec, root, kind)
            eula = _write_eula(root, spec)
        except Exception:
            await _clean_up(sup, spec, root, remove=not existed)
            raise

    started, start_error = False, None
    if run_after:
        # Past this point the server exists and is configured, so a failure to
        # start is reported rather than undoing the whole creation.
        try:
            await sup.start(spec)
            started = True
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            start_error = f"{type(exc).__name__}: {exc}"
            log.warning("created %s but could not start it: %s", spec.name, start_error)

    return {
        "ok": True,
        "root_dir": str(root),
        "jar": install["jar"],
        "size": install["size"],
        "version": download.version,
        "build": download.build,
        "config_files": generated,
        "eula_accepted": eula,
        "started": started,
        "start_error": start_error,
    }


async def _generate_config(sup, spec: InstanceSpec, root: Path, kind: str) -> list[str]:
    """Run the jar once so it writes its initial configuration, then stop it."""
    expected = [root / name for name in CONFIG_FILES[kind]]
    have_all = lambda: all(p.exists() for p in expected)  # noqa: E731

    result = await sup.start(spec)
    if result.get("launch_failed") and not have_all():
        raise await _failed(sup, spec, "the server exited immediately on its first run")

    session = sup.session_name(spec.id)
    deadline = time.monotonic() + FIRST_RUN_TIMEOUT_S
    finished = False
    while time.monotonic() < deadline:
        if not await tmux.has_session(session):
            finished = True          # stopped at its own EULA gate, as expected
            break
        if have_all():
            await asyncio.sleep(_SETTLE_S)
            finished = True          # a proxy keeps running; stop it ourselves
            break
        await asyncio.sleep(_POLL_S)

    with contextlib.suppress(Exception):
        await sup.stop(spec)

    if not finished:
        raise await _failed(
            sup, spec,
            f"the server did not finish generating its configuration within "
            f"{FIRST_RUN_TIMEOUT_S:.0f}s",
        )
    missing = [p.name for p in expected if not p.exists()]
    if missing:
        raise await _failed(sup, spec, f"the first run produced no {', '.join(missing)}")
    return [p.name for p in expected]


def _write_eula(root: Path, spec: InstanceSpec) -> bool:
    """Record the operator's acceptance. Only Mojang's servers read this."""
    if spec.type not in EULA_TYPES:
        return False
    files.resolve_within(root, "eula.txt").write_text(EULA_CONTENT, encoding="utf-8")
    return True


async def _clean_up(sup, spec: InstanceSpec, root: Path, *, remove: bool) -> None:
    """Leave no half-built server behind.

    ``remove`` says whether the directory itself was ours to make. Its contents
    are ours either way -- it was empty when this started -- and they always go:
    leaving a jar, a ``server.properties`` and a half-generated world in a
    directory the operator pre-created (to set ownership, say) would block every
    retry on "already exists and is not empty", with no declared instance left
    to delete it through.
    """
    with contextlib.suppress(Exception):
        await sup.kill(spec)
    if remove:
        shutil.rmtree(root, ignore_errors=True)
        return
    with contextlib.suppress(OSError):
        for entry in root.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
