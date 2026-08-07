"""Agent side of large-file streaming transfers.

The agent always dials out, so it opens the HTTP data connection to the hub:
  - download: stream the file (a directory is zipped to a temp file first, so the
    size is known) up to the hub via a streaming POST;
  - upload: pull the body from the hub via a streaming GET and write it to a temp
    file, then atomically move it into place.

Everything is chunked and file I/O runs in a worker thread, so memory stays flat
regardless of file size and the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import os
import uuid
import zipfile
from pathlib import Path

import httpx

from minemanager_agent import files

CHUNK = 256 * 1024


class TransferError(Exception):
    pass


def _build_zip(src_dir: Path, dest_zip: Path) -> None:
    """Zip a directory to a temp file (blocking — run in a thread)."""
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            zf.write(p, arcname=str(p.relative_to(src_dir)))


async def _run_download(http_base: str, tid: str, root: str, path: str, headers: dict) -> dict:
    src = files._resolve_within(root, path)     # jailed
    tmp_zip: Path | None = None
    if src.is_dir():
        work = Path(root) / files.HIDDEN_DIR
        work.mkdir(parents=True, exist_ok=True)
        tmp_zip = work / f"dl-{uuid.uuid4().hex}.zip"
        await asyncio.to_thread(_build_zip, src, tmp_zip)
        source, filename = tmp_zip, (src.name or "download") + ".zip"
    elif src.is_file():
        source, filename = src, src.name
    else:
        raise TransferError(f"not found: {path}")

    size = source.stat().st_size

    async def body():
        with source.open("rb") as f:
            while True:
                b = await asyncio.to_thread(f.read, CHUNK)
                if not b:
                    break
                yield b

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.post(
                f"{http_base}/api/internal/transfer/{tid}",
                content=body(),
                headers={**headers, "x-mm-total": str(size), "x-mm-filename": filename,
                         "content-type": "application/octet-stream"},
            )
            resp.raise_for_status()
    finally:
        if tmp_zip and tmp_zip.exists():
            try:
                tmp_zip.unlink()
            except OSError:
                pass
    return {"ok": True, "size": size, "filename": filename}


async def _run_upload(http_base: str, tid: str, root: str, path: str, headers: dict) -> dict:
    target = files._resolve_within(root, path)   # jailed
    work = Path(root) / files.HIDDEN_DIR
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / f"ul-{uuid.uuid4().hex}.tmp"
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", f"{http_base}/api/internal/transfer/{tid}",
                                     headers=headers) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(CHUNK):
                        await asyncio.to_thread(f.write, chunk)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, target)                  # atomic move into place
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return {"ok": True, "path": path}


async def handle(identity, data: dict, root: str) -> dict:
    """Dispatch a ``transfer.start`` command. ``identity`` carries node_id,
    credential and the hub's http base."""
    headers = {"x-mm-node": identity.node_id, "x-mm-cred": identity.credential}
    tid, direction, path = data["tid"], data["direction"], data["path"]
    if direction == "download":
        return await _run_download(identity.http_base, tid, root, path, headers)
    if direction == "upload":
        return await _run_upload(identity.http_base, tid, root, path, headers)
    # Never fall through: an unrecognised direction must not reach the upload
    # path, which would overwrite `path` with whatever the hub streamed back.
    raise TransferError(f"unknown transfer direction: {direction!r}")
