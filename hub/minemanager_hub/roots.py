"""Where a node's instances are allowed to live.

The hub checks this so an operator gets a 400 instead of a confusing agent
error, and so the UI can compose a new server's directory. It is not the
boundary: the hub cannot see the node's filesystem, so it cannot resolve
symlinks, and the check that counts is
:func:`minemanager_agent.files.resolve_instance_root`, which runs on the machine
holding the files. Both have to agree.

All nodes are Linux, so paths are POSIX here regardless of what the hub runs on.
"""

from __future__ import annotations

from pathlib import PurePosixPath


class RootError(ValueError):
    """A path is not usable as an instance root on this node."""


def normalise(path: str) -> str:
    """An absolute, cleaned-up POSIX path, or raise :class:`RootError`."""
    raw = (path or "").strip()
    if not raw:
        raise RootError("path is empty")
    if "\0" in raw:
        raise RootError("path contains a NUL byte")
    if not raw.startswith("/"):
        raise RootError(f"path must be absolute: {raw!r}")
    # ".." cannot be resolved honestly without the real filesystem, so it is
    # refused outright rather than collapsed into something that looks safe.
    parts = [p for p in PurePosixPath(raw).parts if p != "."]
    if ".." in parts:
        raise RootError(f"path may not contain '..': {raw!r}")
    return str(PurePosixPath(*parts))


def server_dir(node_root: str, name: str) -> str:
    """The directory a new server called ``name`` gets on this node."""
    return normalise(f"{normalise(node_root)}/{name}")


def is_stranded(node_root: str | None, instance_root: str) -> bool:
    """Whether the agent will refuse every command for this instance.

    The agent gates each instance-scoped command on the same containment check,
    so an instance declared before its node gained a root — or outside the root
    it gained — stops answering entirely. Reported so the UI can say that,
    instead of the operator meeting it as "path rejected" on every button.
    """
    try:
        check_under(node_root, instance_root)
    except RootError:
        return True
    return False


def check_under(node_root: str | None, instance_root: str) -> str:
    """Normalise ``instance_root`` and require it to sit under ``node_root``.

    ``node_root`` of None is a node whose agent has no root configured: nothing
    is enforced there, by the hub or by the agent.
    """
    path = normalise(instance_root)
    if node_root is None:
        return path
    root = normalise(node_root)
    if PurePosixPath(root) not in PurePosixPath(path).parents:
        raise RootError(f"instance root must be inside this node's root {root}: {path!r}")
    return path
