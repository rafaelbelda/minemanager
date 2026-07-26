"""Jailed file operations, confined to an instance's root directory.

Every path from the hub is treated as untrusted and resolved against the
instance root; anything that escapes the root (via ``..`` or symlinks) is
rejected. This is the agent-side enforcement of the "files jailed to node root"
rule from the plan.
"""

from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path


class JailError(Exception):
    """Raised when a requested path would escape the instance root."""


def _resolve_within(root: str | Path, rel: str) -> Path:
    """Resolve ``rel`` against ``root`` and ensure it stays inside ``root``."""
    root_path = Path(root).resolve()
    # Reject absolute inputs outright; everything is relative to the root.
    candidate = (root_path / rel.lstrip("/\\")).resolve()
    if candidate != root_path and root_path not in candidate.parents:
        raise JailError(f"path escapes instance root: {rel!r}")
    return candidate


def list_dir(root: str | Path, rel: str = ".") -> list[dict]:
    target = _resolve_within(root, rel)
    if not target.is_dir():
        raise NotADirectoryError(rel)
    root_path = Path(root).resolve()
    entries: list[dict] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        st = child.stat()
        entries.append(
            {
                "name": child.name,
                "path": str(child.relative_to(root_path)).replace(os.sep, "/"),
                "is_dir": child.is_dir(),
                "size": st.st_size,
                "modified": st.st_mtime,
            }
        )
    return entries


def read_file(root: str | Path, rel: str, max_bytes: int = 5_000_000) -> str:
    target = _resolve_within(root, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    if target.stat().st_size > max_bytes:
        raise ValueError(f"file too large to read inline (> {max_bytes} bytes)")
    return target.read_text(encoding="utf-8", errors="replace")


def tail_lines(
    root: str | Path,
    rel: str,
    lines: int = 200,
    max_bytes: int = 1_000_000,
) -> dict:
    """Return the last ``lines`` lines of a text file (for console backfill).

    Reads at most the final ``max_bytes`` of the file so a huge log never loads
    fully. A missing file is not an error — it just yields no lines (a server
    that has never produced ``logs/latest.log`` is a normal state).
    """
    target = _resolve_within(root, rel)
    if not target.is_file():
        return {"lines": [], "path": rel, "missing": True}

    size = target.stat().st_size
    start = max(0, size - max_bytes)
    with target.open("rb") as fh:
        fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    # If we started mid-file, drop the partial first line.
    if start > 0:
        text = text.split("\n", 1)[-1]
    out = text.splitlines()
    return {"lines": out[-lines:], "path": rel, "truncated": start > 0}


def write_file(root: str | Path, rel: str, content: str) -> dict:
    target = _resolve_within(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": rel, "size": target.stat().st_size}


def write_bytes(root: str | Path, rel: str, b64: str) -> dict:
    target = _resolve_within(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(b64))
    return {"path": rel, "size": target.stat().st_size}


def delete(root: str | Path, rel: str, recursive: bool = False) -> dict:
    target = _resolve_within(root, rel)
    if target == Path(root).resolve():
        raise JailError("refusing to delete the instance root")
    if target.is_dir():
        if recursive:
            shutil.rmtree(target)
        else:
            target.rmdir()  # fails if non-empty — intentional
    else:
        target.unlink()
    return {"path": rel, "deleted": True}


def mkdir(root: str | Path, rel: str) -> dict:
    target = _resolve_within(root, rel)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": rel, "created": True}
