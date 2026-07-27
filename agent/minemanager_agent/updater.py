"""Transactional server-binary updater.

Replaces *only* the server jar, never worlds/plugins/mods/config/player data —
those all live elsewhere in the instance root and are never touched. The swap is
transactional:

  1. download the new jar to a temp file (bounded by time and size, checksum
     verified when the provider supplied one),
  2. back up the current jar,
  3. atomically replace it (``os.replace`` on the same filesystem),
  4. on any failure, leave the original in place (the atomic replace guarantees
     it) and, defensively, restore from the backup if the jar went missing.

The download runs in a worker thread so it never blocks the agent's event loop.
Uses only the stdlib — the agent keeps a minimal dependency surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from minemanager_agent.files import _resolve_within  # jailing helper (reused)

_UA = "minemanager-agent/0.1 (+https://github.com/rafaelbelda/minemanager)"

DOWNLOAD_TIMEOUT_S = 300.0        # wall-clock budget for the whole download
SOCKET_TIMEOUT_S = 60.0           # per-read/connect stall timeout
MAX_BYTES = 500 * 1024 * 1024     # hard size cap (safety)
_CHUNK = 65536
_KEEP_BACKUPS = 5


class UpdateError(Exception):
    pass


def _download(url: str, dest: Path, *, algo: str | None, expected: str | None,
              timeout: float, max_bytes: int) -> int:
    """Blocking streamed download with time + size guards and checksum verify."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    hasher = hashlib.new(algo) if algo else None
    started = time.monotonic()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT_S) as resp, dest.open("wb") as fh:
            while True:
                if time.monotonic() - started > timeout:
                    raise UpdateError("download exceeded the time budget")
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UpdateError("download exceeds the size limit")
                fh.write(chunk)
                if hasher:
                    hasher.update(chunk)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 - network/IO → uniform error
        raise UpdateError(f"download failed: {exc}") from exc

    if hasher and expected:
        got = hasher.hexdigest().lower()
        if got != expected.lower():
            raise UpdateError(f"checksum mismatch (expected {expected[:12]}…, got {got[:12]}…)")
    return total


def _prune_backups(backups: Path, jar_base: str, keep: int) -> None:
    files = sorted(backups.glob(f"{jar_base}.*.bak"))
    for old in files[:-keep] if len(files) > keep else []:
        try:
            old.unlink()
        except OSError:
            pass


async def apply_update(root: str, jar_name: str, download: dict[str, Any]) -> dict:
    """Perform the transactional jar swap. Returns a result dict or raises
    :class:`UpdateError`."""
    root_p = Path(root)
    if not root_p.is_dir():
        raise UpdateError(f"instance root does not exist: {root}")

    jar_path = _resolve_within(root, jar_name)   # jailed: jar_name cannot escape root
    work = root_p / ".minemanager"
    backups = work / "backups"
    backups.mkdir(parents=True, exist_ok=True)

    tmp = work / f".download-{uuid.uuid4().hex}.tmp"
    backup_path: Path | None = None
    replaced = False
    ts = time.strftime("%Y%m%d-%H%M%S")
    jar_base = Path(jar_name).name

    try:
        size = await asyncio.to_thread(
            _download,
            download["url"], tmp,
            algo=download.get("checksum_algo"),
            expected=download.get("checksum"),
            timeout=DOWNLOAD_TIMEOUT_S,
            max_bytes=MAX_BYTES,
        )

        # Back up the current jar (if any) before touching it.
        if jar_path.exists():
            backup_path = backups / f"{jar_base}.{ts}.bak"
            shutil.copy2(jar_path, backup_path)

        # Atomic swap on the same filesystem.
        os.replace(tmp, jar_path)
        replaced = True
        _prune_backups(backups, jar_base, _KEEP_BACKUPS)

        return {
            "ok": True,
            "jar": jar_name,
            "size": size,
            "installed": {"version": download.get("version"), "build": download.get("build")},
            "backup": str(backup_path.relative_to(root_p)) if backup_path else None,
        }
    except Exception as exc:
        # Rollback: the atomic replace guarantees the original is intact unless
        # it succeeded; if the jar somehow went missing, restore from backup.
        if not replaced and backup_path and backup_path.exists() and not jar_path.exists():
            try:
                shutil.copy2(backup_path, jar_path)
            except OSError:
                pass
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(str(exc)) from exc
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
