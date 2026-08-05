"""Thin async wrapper over the ``tmux`` CLI.

Each managed instance runs in its own detached tmux session named
``<prefix>-<instance_id>``. tmux gives us a real pty (clean stdin via
``send-keys``) and, as a bonus, lets a human ``tmux attach`` to debug a server
directly. Process supervision (restart policy, crash detection) lives one layer
up in :mod:`minemanager_agent.supervisor`.
"""

from __future__ import annotations

import asyncio
import os
import shlex

# Dedicated tmux server socket, so our sessions live in the agent's own tmux
# server (and thus its systemd cgroup) — this is what lets a shutdown handler
# stop the servers before systemd force-kills the group, and avoids clobbering a
# user's tmux. All commands go through this socket.
DEFAULT_SOCKET = "minemanager"
_SOCKET = os.environ.get("MM_TMUX_SOCKET", DEFAULT_SOCKET)


def socket_name() -> str:
    """The tmux socket currently in use."""
    return _SOCKET


def use_socket(name: str) -> None:
    """Pin the socket for the rest of this process.

    The agent pins socket *and* session prefix to the values it last ran with, so
    an edited env var cannot detach it from servers it is already supervising —
    see :meth:`AgentConfig.pin_runtime_identity`. Read at call time by
    :func:`_run`, so this applies to every subsequent tmux invocation.
    """
    global _SOCKET
    _SOCKET = name


# Occupies the pane between creating a session and respawning the real command
# into it (see new_session). It must print *nothing* — the default interactive
# shell writes a prompt, which then shows up in the mirrored pane output and
# gets replayed to the operator as if the server had printed it. Blocking on a
# read from the pty is silent and adds no dependency: tmux already requires
# /bin/sh to run any command at all.
_PLACEHOLDER = "sh -c 'read _'"


class TmuxError(Exception):
    pass


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "-L", _SOCKET,
        *args,
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


async def list_sessions(prefix: str) -> list[str]:
    """Names of our live sessions (``<prefix>-*``). Empty if tmux/no server."""
    return [n for n in await list_all_sessions() if n.startswith(prefix + "-")]


async def has_session(name: str) -> bool:
    try:
        code, _, _ = await _run("has-session", "-t", name)
    except FileNotFoundError:
        return False  # tmux not installed → nothing is running under it
    return code == 0


async def new_session(name: str, workdir: str, command: str, mirror_path: str | None = None) -> None:
    """Start ``command`` in a fresh detached session rooted at ``workdir``,
    optionally mirroring everything its pane prints to ``mirror_path``.

    When ``command`` exits, the session ends — that transition is how the
    supervisor detects a stop/crash.

    The launch is deliberately **two steps** — an empty session, then
    ``respawn-pane`` — rather than the obvious ``new-session <command>``. A pipe
    can only be attached to a pane that already exists, so with the one-step
    form the command has already run (and, when it fails at once, already died
    and taken its session with it) before :func:`pipe_pane` can attach: the
    mirror captures nothing in exactly the case it exists for. Creating the pane
    idle lets the pipe be in place *before* anything runs. Verified against tmux
    3.6: the pipe survives the respawn, ``send-keys`` still reaches the new
    process, and the session still ends when it exits.
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

    code, _, err = await _run("respawn-pane", "-k", "-t", name, "-c", workdir, command)
    if code != 0:
        # Leave nothing behind: the idle shell would otherwise keep the session
        # alive, and every liveness check we have would call it a running server.
        await kill_session(name)
        raise TmuxError(f"failed to start session {name}: {err.strip()}")


async def pipe_pane(name: str, path: str) -> bool:
    """Mirror everything the session's pane prints into ``path``.

    This is the *only* way to keep output a server writes before (or instead of)
    ``logs/latest.log`` — a JVM that dies on ``UnsupportedClassVersionError``, a
    port already in use, a bad ``-Xmx`` — because when the command exits tmux
    destroys the session and its scrollback with it, leaving nothing to
    ``capture-pane`` by the time the supervisor notices. The pipe writes as the
    output happens, so the file outlives the session.

    ``-o`` makes this a no-op if a pipe is already open, so it is safe to call
    on an adopted session. Never raises: losing diagnostics must not stop a
    server from launching.
    """
    try:
        code, _, _ = await _run("pipe-pane", "-o", "-t", name, f"cat >> {shlex.quote(path)}")
    except FileNotFoundError:
        return False
    return code == 0


async def send_keys(name: str, line: str) -> None:
    """Type a line into the session's stdin (as if entered at the console)."""
    if not await has_session(name):
        raise TmuxError(f"no such session: {name}")
    # Literal (-l) text, then a separate Enter, so control chars aren't parsed.
    # "--" terminates option parsing: without it a console line starting with
    # "-" is read by tmux as flags instead of as text.
    code, _, err = await _run("send-keys", "-t", name, "-l", "--", line)
    if code != 0:
        raise TmuxError(f"send-keys failed: {err.strip()}")
    await _run("send-keys", "-t", name, "Enter")


async def kill_session(name: str) -> None:
    if await has_session(name):
        await _run("kill-session", "-t", name)


async def kill_server() -> None:
    """Tear down our whole tmux server (dedicated socket), leaving nothing behind
    after a shutdown. Tolerates 'no server running'."""
    try:
        await _run("kill-server")
    except FileNotFoundError:
        pass


async def tmux_available() -> bool:
    try:
        code, _, _ = await _run("-V")
        return code == 0
    except FileNotFoundError:
        return False
