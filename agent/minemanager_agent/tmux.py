"""Thin async wrapper over the ``tmux`` CLI.

Each managed instance runs in its own detached tmux session named
``<SESSION_PREFIX>-<instance_id>``. tmux gives us a real pty (clean stdin via
``send-keys``) and lets a human ``tmux attach`` to debug a server directly.
Process supervision (restart policy, crash detection) lives one layer up in
:mod:`minemanager_agent.supervisor`.

Both names below are constants, not configuration: together they are a running
server's identity. Changing either while servers are up makes every existing
session invisible to the agent.
"""

from __future__ import annotations

import asyncio
import os
import shlex


SOCKET = "minemanager"
SESSION_PREFIX = "mm"


# Occupies the pane between creating a session and respawning the real command
# into it (see new_session). It must print nothing: a shell prompt would show up
# in the mirrored pane and be replayed as if the server had printed it.
_PLACEHOLDER = "sh -c 'read _'"

# tmux runs every pane command and pipe through the server's default-shell,
# taken from $SHELL or the passwd entry. The agent's system user has
# /usr/sbin/nologin there, which would refuse every command, so pin a real one.
_ENV = {**os.environ, "SHELL": "/bin/sh"}


class TmuxError(Exception):
    pass


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "-L", SOCKET,
        *args,
        env=_ENV,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def list_all_sessions() -> list[str]:
    """Every session on our socket, whatever its prefix. Empty if tmux/no server."""
    try:
        code, out, _ = await _run("list-sessions", "-F", "#{session_name}")
    except FileNotFoundError:
        return []
    if code != 0:
        return []  # "no server running" → no sessions
    return out.splitlines()


async def list_sessions() -> list[str]:
    """Names of our live sessions (``<SESSION_PREFIX>-*``)."""
    return [n for n in await list_all_sessions() if n.startswith(SESSION_PREFIX + "-")]


async def has_session(name: str) -> bool:
    try:
        code, _, _ = await _run("has-session", "-t", name)
    except FileNotFoundError:
        return False  # tmux not installed → nothing is running under it
    return code == 0


async def new_session(
    name: str,
    workdir: str,
    command: str,
    mirror_path: str | None = None,
    status_path: str | None = None,
) -> None:
    """Start ``command`` in a fresh detached session rooted at ``workdir``,
    optionally mirroring everything its pane prints to ``mirror_path``.

    When ``command`` exits the session ends, which is how the supervisor
    detects a stop or crash.

    With ``status_path``, the exit status is appended to the shell line and so
    written from inside the pane, before it closes. A session that had to be
    killed leaves no file, which reads as "not an orderly shutdown".
    """
    if await has_session(name):
        raise TmuxError(f"session already exists: {name}")
    try:
        code, _, err = await _run("new-session", "-d", "-s", name, "-c", workdir, _PLACEHOLDER)
    except FileNotFoundError as exc:
        raise TmuxError("tmux is not installed on this node") from exc
    if code != 0:
        raise TmuxError(f"failed to start session {name}: {err.strip()}")

    if mirror_path is not None:
        await pipe_pane(name, mirror_path)

    # Appended rather than wrapped, so the operator's command is still what the
    # shell sees first and a shell-dependent start line keeps working.
    launched = command if status_path is None else (
        f'{command}; printf %s "$?" > {shlex.quote(status_path)}'
    )
    code, _, err = await _run("respawn-pane", "-k", "-t", name, "-c", workdir, launched)
    if code != 0:
        # Leave nothing behind: an idle shell would keep the session alive and
        # read as a running server.
        await kill_session(name)
        raise TmuxError(f"failed to start session {name}: {err.strip()}")


async def pipe_pane(name: str, path: str) -> bool:
    # Mirror everything the session's pane prints into ``path``.

    try:
        code, _, _ = await _run("pipe-pane", "-o", "-t", name, f"cat >> {shlex.quote(path)}")
    except FileNotFoundError:
        return False
    return code == 0


async def stop_pipe_pane(name: str) -> None:
    """Close the pane mirror opened by :func:`pipe_pane`."""
    try:
        await _run("pipe-pane", "-t", name)
    except FileNotFoundError:
        pass


async def send_keys(name: str, line: str) -> None:
    """Type a line into the session's stdin (as if entered at the console)."""
    if not await has_session(name):
        raise TmuxError(f"no such session: {name}")
    # Literal (-l) text, then a separate Enter, so control chars aren't parsed.
    # "--" terminates option parsing, so a line starting with "-" is text.
    code, _, err = await _run("send-keys", "-t", name, "-l", "--", line)
    if code != 0:
        raise TmuxError(f"send-keys failed: {err.strip()}")
    await _run("send-keys", "-t", name, "Enter")


async def kill_session(name: str) -> None:
    if await has_session(name):
        await _run("kill-session", "-t", name)


async def kill_server() -> None:
    """Tear down our whole tmux server. Tolerates 'no server running'."""
    try:
        await _run("kill-server")
    except FileNotFoundError:
        pass
