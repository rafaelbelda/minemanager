"""Thin async wrapper over the ``tmux`` CLI.

Each managed instance runs in its own detached tmux session named
``<prefix>-<instance_id>``. tmux gives us a real pty (clean stdin via
``send-keys``) and, as a bonus, lets a human ``tmux attach`` to debug a server
directly. Process supervision (restart policy, crash detection) lives one layer
up in :mod:`minemanager_agent.supervisor`.
"""

from __future__ import annotations

import asyncio


class TmuxError(Exception):
    pass


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def has_session(name: str) -> bool:
    try:
        code, _, _ = await _run("has-session", "-t", name)
    except FileNotFoundError:
        return False  # tmux not installed → nothing is running under it
    return code == 0


async def new_session(name: str, workdir: str, command: str) -> None:
    """Start ``command`` in a fresh detached session rooted at ``workdir``.

    When ``command`` exits, the session ends — that transition is how the
    supervisor detects a stop/crash.
    """
    if await has_session(name):
        raise TmuxError(f"session already exists: {name}")
    code, _, err = await _run("new-session", "-d", "-s", name, "-c", workdir, command)
    if code != 0:
        raise TmuxError(f"failed to start session {name}: {err.strip()}")


async def send_keys(name: str, line: str) -> None:
    """Type a line into the session's stdin (as if entered at the console)."""
    if not await has_session(name):
        raise TmuxError(f"no such session: {name}")
    # Literal (-l) text, then a separate Enter, so control chars aren't parsed.
    code, _, err = await _run("send-keys", "-t", name, "-l", line)
    if code != 0:
        raise TmuxError(f"send-keys failed: {err.strip()}")
    await _run("send-keys", "-t", name, "Enter")


async def kill_session(name: str) -> None:
    if await has_session(name):
        await _run("kill-session", "-t", name)


async def tmux_available() -> bool:
    try:
        code, _, _ = await _run("-V")
        return code == 0
    except FileNotFoundError:
        return False
