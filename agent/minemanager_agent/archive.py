"""Archive extraction, jailed to the instance root.

Extraction happens entirely on the node (disk-to-disk, streamed member by
member), so it is memory-bounded and supports large archives without the
transfer channel. Every member path is validated against the jail to defeat
zip-slip / path-traversal. Supported: ZIP, TAR(.GZ/.BZ2/.XZ)/TGZ, single-file
GZ, and RAR when the node has the ``rarfile`` lib + ``unrar`` tool.
"""

from __future__ import annotations

import gzip
import shutil
import tarfile
import zipfile
from pathlib import Path

from minemanager_agent.files import JailError, _resolve_within

_MAX_CONFLICTS = 200


class UnsupportedArchive(Exception):
    """The archive format isn't supported on this node."""


def detect_format(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(".tar.gz") or n.endswith(".tgz") or n.endswith(".tar") \
            or n.endswith(".tar.bz2") or n.endswith(".tar.xz"):
        return "tar"
    if n.endswith(".gz"):          # single-file gzip (checked after .tar.gz)
        return "gz"
    if n.endswith(".rar"):
        return "rar"
    return None


def is_archive(name: str) -> bool:
    return detect_format(name) is not None


def _safe_target(root: str | Path, dest_rel: str, member: str) -> tuple[Path, str]:
    """Resolve an archive member to a jailed target path, rejecting traversal."""
    m = member.replace("\\", "/").lstrip("/")
    if not m or ".." in Path(m).parts:
        raise JailError(f"unsafe archive member: {member!r}")
    joined = m if dest_rel in ("", ".") else f"{dest_rel}/{m}"
    target = _resolve_within(root, joined)   # raises JailError if it escapes root
    return target, joined


def extract(root: str | Path, rel: str, overwrite: bool = False) -> dict:
    """Extract ``rel`` into its own directory. Returns ``{"extracted": bool,
    "conflicts": [...], "count": n}``; when conflicts exist and ``overwrite`` is
    false, nothing is written and the conflicting paths are returned."""
    archive = _resolve_within(root, rel)
    if not archive.is_file():
        raise FileNotFoundError(rel)
    fmt = detect_format(archive.name)
    if fmt is None:
        raise ValueError(f"unsupported archive format: {archive.name}")

    dest_rel = str(Path(rel).parent).replace("\\", "/")
    if dest_rel in ("", "."):
        dest_rel = "."

    if fmt == "zip":
        return _do_zip(archive, root, dest_rel, overwrite)
    if fmt == "tar":
        return _do_tar(archive, root, dest_rel, overwrite)
    if fmt == "gz":
        return _do_gz(archive, root, dest_rel, overwrite)
    return _do_rar(archive, root, dest_rel, overwrite)


def _finish(plan, overwrite, write_one) -> dict:
    """Shared conflict-check + execution over a plan of (target, rel, is_dir)."""
    conflicts = [rel for target, rel, is_dir in plan if not is_dir and target.exists()]
    if conflicts and not overwrite:
        return {"extracted": False, "conflicts": conflicts[:_MAX_CONFLICTS],
                "conflict_count": len(conflicts)}
    count = 0
    for target, rel, is_dir in plan:
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_one(target, rel)
            count += 1
    return {"extracted": True, "count": count}


def _do_zip(archive: Path, root, dest_rel: str, overwrite: bool) -> dict:
    with zipfile.ZipFile(archive) as zf:
        plan, by_rel = [], {}
        for info in zf.infolist():
            target, joined = _safe_target(root, dest_rel, info.filename)
            plan.append((target, joined, info.is_dir()))
            by_rel[joined] = info

        def write_one(target: Path, joined: str) -> None:
            with zf.open(by_rel[joined]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

        return _finish(plan, overwrite, write_one)


def _do_tar(archive: Path, root, dest_rel: str, overwrite: bool) -> dict:
    with tarfile.open(archive, "r:*") as tf:
        plan, by_rel = [], {}
        for m in tf.getmembers():
            if not (m.isfile() or m.isdir()):
                continue  # skip symlinks / hardlinks / devices for safety
            target, joined = _safe_target(root, dest_rel, m.name)
            plan.append((target, joined, m.isdir()))
            by_rel[joined] = m

        def write_one(target: Path, joined: str) -> None:
            src = tf.extractfile(by_rel[joined])
            if src is None:
                return
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

        return _finish(plan, overwrite, write_one)


def _do_gz(archive: Path, root, dest_rel: str, overwrite: bool) -> dict:
    out_name = archive.name[:-3] or (archive.stem + ".out")   # strip ".gz"
    target, joined = _safe_target(root, dest_rel, out_name)
    if target.exists() and not overwrite:
        return {"extracted": False, "conflicts": [joined], "conflict_count": 1}
    with gzip.open(archive, "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return {"extracted": True, "count": 1}


def _do_rar(archive: Path, root, dest_rel: str, overwrite: bool) -> dict:
    try:
        import rarfile  # optional; needs the `unrar` tool at runtime
    except ImportError as exc:
        raise UnsupportedArchive(
            "RAR is not supported on this node (the rarfile library is not installed)"
        ) from exc
    try:
        rf = rarfile.RarFile(str(archive))
    except rarfile.Error as exc:  # includes missing unrar tool
        raise UnsupportedArchive(f"RAR is not supported on this node ({exc})") from exc

    with rf:
        plan, by_rel = [], {}
        for info in rf.infolist():
            target, joined = _safe_target(root, dest_rel, info.filename)
            plan.append((target, joined, info.isdir()))
            by_rel[joined] = info

        def write_one(target: Path, joined: str) -> None:
            with rf.open(by_rel[joined]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

        return _finish(plan, overwrite, write_one)
