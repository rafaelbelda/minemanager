"""Jailed file operations, confined to an instance's root directory.

Every path from the hub is treated as untrusted and resolved against the
instance root; anything that escapes the root (via ``..`` or symlinks) is
rejected. This is the agent-side enforcement of the "files jailed to node root"
rule from the plan.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import zipfile
from pathlib import Path

_BINARY_SNIFF_BYTES = 8192
# Conservative in-memory guard so building a directory zip can't OOM the agent
# even when a huge directory slips past the transfer cap on uncompressed size.
_ZIP_UNCOMPRESSED_GUARD = 256 * 1024 * 1024


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


def read_for_editor(root: str | Path, rel: str, max_bytes: int = 5_000_000) -> dict:
    """Read a file for the text editor, refusing binary content.

    Binary is detected by a NUL byte in the first chunk (the standard heuristic).
    Binary files return ``{"binary": True}`` with no content — they must not be
    shown as text, only downloaded. Oversized text files are refused so the
    editor never has to load a huge file into the browser.
    """
    target = _resolve_within(root, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    size = target.stat().st_size
    with target.open("rb") as fh:
        head = fh.read(_BINARY_SNIFF_BYTES)
    if b"\x00" in head:
        return {"binary": True, "size": size, "path": rel}
    if size > max_bytes:
        raise ValueError(f"file too large to open in the editor (> {max_bytes} bytes)")
    return {
        "binary": False,
        "content": target.read_text(encoding="utf-8", errors="replace"),
        "size": size,
        "path": rel,
    }


def fetch(root: str | Path, rel: str, cap: int) -> dict:
    """Return a file's bytes (base64), or a directory zipped (base64).

    Bounded by ``cap`` so a normal-sized download stays within the WS frame /
    memory budget; larger transfers are the streaming path (separate feature).
    Symlinks are skipped when zipping a directory (avoids escaping the jail and
    symlink loops).
    """
    target = _resolve_within(root, rel)
    if target.is_dir():
        buf = io.BytesIO()
        uncompressed = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(target.rglob("*")):
                if p.is_symlink() or not p.is_file():
                    continue
                uncompressed += p.stat().st_size
                if uncompressed > _ZIP_UNCOMPRESSED_GUARD:
                    raise ValueError("directory is too large to archive here — use the large transfer")
                zf.write(p, arcname=str(p.relative_to(target)))
        data = buf.getvalue()
        if len(data) > cap:
            raise ValueError(f"zip is larger than the transfer cap ({cap} bytes) — use the large transfer")
        name = (target.name or Path(root).resolve().name or "download") + ".zip"
        return {"filename": name, "is_dir": True, "size": len(data),
                "content_b64": base64.b64encode(data).decode()}

    if not target.exists():
        raise FileNotFoundError(rel)
    size = target.stat().st_size
    if size > cap:
        raise ValueError(f"file is larger than the transfer cap ({cap} bytes) — use the large transfer")
    return {"filename": target.name, "is_dir": False, "size": size,
            "content_b64": base64.b64encode(target.read_bytes()).decode()}


def rename(root: str | Path, rel: str, new_name: str) -> dict:
    """Rename an entry within its own directory (new_name is a bare filename)."""
    src = _resolve_within(root, rel)
    if not src.exists():
        raise FileNotFoundError(rel)
    if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
        raise ValueError("invalid name")
    root_path = Path(root).resolve()
    dst = _resolve_within(root, str(Path(rel).parent / new_name))
    if dst == root_path:
        raise JailError("refusing to rename the instance root")
    if dst.exists():
        raise FileExistsError(new_name)
    src.rename(dst)
    return {"path": str(dst.relative_to(root_path)).replace(os.sep, "/"), "renamed": True}


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
