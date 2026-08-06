"""Archive extraction, jailed to the instance root.

Extraction happens entirely on the node (disk-to-disk, streamed member by
member), so it is memory-bounded and supports large archives without the
transfer channel. Every member path is validated against the jail to defeat
zip-slip / path-traversal. Supported: ZIP, TAR(.GZ/.BZ2/.XZ)/TGZ, single-file
GZ, and RAR when the node has the ``rarfile`` lib + ``unrar`` tool.
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path

from minemanager_agent.files import JailError, _resolve_within

_MAX_CONFLICTS = 200

# Ceiling on total *uncompressed* output. Uploads are capped around 8 MB, and a
# well-formed archive that size expands to hundreds of GB — filling a node's disk
# is worse than crashing it, because servers then cannot save chunks and the next
# write can corrupt a world. Override with MM_MAX_EXTRACT_BYTES.
MAX_EXTRACT_BYTES = int(os.environ.get("MM_MAX_EXTRACT_BYTES", str(20 * 1024 * 1024 * 1024)))

# Keep some room on the filesystem after extracting, so a large-but-legal archive
# still cannot be the thing that leaves a world with nowhere to save.
_FREE_SPACE_MARGIN = 512 * 1024 * 1024


def _is_regular_zip_member(info: zipfile.ZipInfo) -> bool:
    """Reject anything that is not a plain file or directory.

    ZIP stores the unix mode in the top 16 bits of ``external_attr``. Python's
    ``zipfile`` happens not to materialise symlinks (``zf.open`` + copyfileobj
    writes the *link target string* as file content), so this was safe by
    accident. The tar path has always filtered explicitly; make the guarantee
    explicit here too rather than resting on a library implementation detail.
    """
    fmt = stat.S_IFMT(info.external_attr >> 16)
    # Plenty of writers store permission bits with no file-type field at all
    # (Python's own ``writestr`` stores 0o600), and DOS-created entries store
    # nothing. Only reject when a type *is* recorded and it is not a file or dir —
    # otherwise a legitimate archive would extract to nothing.
    if fmt == 0:
        return True
    return fmt in (stat.S_IFREG, stat.S_IFDIR)


def _check_budget(total: int, root: str | Path) -> None:
    """Refuse an extraction that would blow the size cap or fill the disk."""
    if total > MAX_EXTRACT_BYTES:
        raise ValueError(
            f"archive expands to {total} bytes, over the {MAX_EXTRACT_BYTES}-byte limit "
            f"(MM_MAX_EXTRACT_BYTES)"
        )
    try:
        free = shutil.disk_usage(Path(root)).free
    except OSError:
        return
    if total + _FREE_SPACE_MARGIN > free:
        raise ValueError(
            f"archive expands to {total} bytes but only {free} bytes are free; refusing "
            f"so the node keeps room to save worlds"
        )


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
    preexisting = {target for target, _rel, is_dir in plan if not is_dir and target.exists()}
    conflicts = [rel for target, rel, is_dir in plan if not is_dir and target in preexisting]
    if conflicts and not overwrite:
        return {"extracted": False, "conflicts": conflicts[:_MAX_CONFLICTS],
                "conflict_count": len(conflicts)}
    count = 0
    created: list[Path] = []
    try:
        for target, rel, is_dir in plan:
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                write_one(target, rel)
                if target not in preexisting:
                    created.append(target)
                count += 1
    except Exception:
        for p in reversed(created):
            try:
                p.unlink()
            except OSError:
                pass
        raise
    return {"extracted": True, "count": count}


def _do_zip(archive: Path, root, dest_rel: str, overwrite: bool) -> dict:
    with zipfile.ZipFile(archive) as zf:
        plan, by_rel, total = [], {}, 0
        for info in zf.infolist():
            if not _is_regular_zip_member(info):
                continue          # symlink / device / socket — never materialise it
            target, joined = _safe_target(root, dest_rel, info.filename)
            plan.append((target, joined, info.is_dir()))
            by_rel[joined] = info
            total += info.file_size
        _check_budget(total, root)

        def write_one(target: Path, joined: str) -> None:
            with zf.open(by_rel[joined]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

        return _finish(plan, overwrite, write_one)


def _do_tar(archive: Path, root, dest_rel: str, overwrite: bool) -> dict:
    with tarfile.open(archive, "r:*") as tf:
        plan, by_rel, total = [], {}, 0
        for m in tf.getmembers():
            if not (m.isfile() or m.isdir()):
                continue  # skip symlinks / hardlinks / devices for safety
            target, joined = _safe_target(root, dest_rel, m.name)
            plan.append((target, joined, m.isdir()))
            by_rel[joined] = m
            total += m.size
        _check_budget(total, root)

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
    preexisting = target.exists()
    if preexisting and not overwrite:
        return {"extracted": False, "conflicts": [joined], "conflict_count": 1}
    # A single gzip stream does not declare its uncompressed size up front (the
    # footer's ISIZE is only mod 2^32), so the budget is enforced while copying.
    written = 0
    try:
        with gzip.open(archive, "rb") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_EXTRACT_BYTES:
                    raise ValueError(
                        f"gzip stream exceeds the {MAX_EXTRACT_BYTES}-byte extraction "
                        f"limit (MM_MAX_EXTRACT_BYTES)"
                    )
                dst.write(chunk)
    except Exception:
        if not preexisting:      # never delete a file that was already there
            target.unlink(missing_ok=True)
        raise
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
        plan, by_rel, total = [], {}, 0
        for info in rf.infolist():
            if getattr(info, "is_symlink", lambda: False)():
                continue          # same rule as zip/tar: never materialise links
            target, joined = _safe_target(root, dest_rel, info.filename)
            plan.append((target, joined, info.isdir()))
            by_rel[joined] = info
            total += getattr(info, "file_size", 0) or 0
        _check_budget(total, root)

        def write_one(target: Path, joined: str) -> None:
            with rf.open(by_rel[joined]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

        return _finish(plan, overwrite, write_one)
