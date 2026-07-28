"""Agent entrypoint.

Run (dev):
    MM_HUB_URL=ws://127.0.0.1:8730/ws/agent MM_ENROLL_TOKEN=<token> \
        python -m minemanager_agent.main

Under systemd this is the ExecStart target — the only daemon on the node.

On SIGTERM/SIGINT the agent decides whether to gracefully stop the game servers:
- if the **system** is shutting down (reboot/poweroff), it sends each server a
  clean stop and waits for the world to save before exiting, so nothing is
  force-killed mid-write;
- for a plain agent restart/stop, the servers are left running (they live in
  their own tmux sessions and survive the agent), so upgrading the agent never
  interrupts gameplay.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket

from minemanager_agent.config import AgentConfig
from minemanager_agent.connection import Connection

log = logging.getLogger("minemanager.agent")

# Generous but bounded window for worlds to save on shutdown (well under the
# unit's TimeoutStopSec). Sessions are stopped concurrently.
_SHUTDOWN_GRACE_S = 120.0


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _system_is_stopping() -> bool:
    """True when the whole machine is shutting down (systemd manager 'stopping').

    This is how we tell a reboot/poweroff from a plain agent restart. Bounded so
    a slow/hung systemctl can never stall shutdown; on any uncertainty we return
    False (leave servers running) and rely on the JVM shutdown-hook safety net.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-system-running",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return False
    return out.decode(errors="replace").strip() == "stopping"


async def _amain() -> None:
    config = AgentConfig()
    config.ensure_dirs()
    conn = Connection(config, hostname=socket.gethostname())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows dev — Ctrl+C still raises KeyboardInterrupt

    serve = asyncio.create_task(conn.run_forever())
    serve.add_done_callback(lambda _: stop.set())   # also wake if serve ever exits
    await stop.wait()

    try:
        # Only a system shutdown gracefully stops the servers; a plain agent
        # restart leaves them running (they survive in tmux).
        if not serve.done() and await _system_is_stopping():
            n = await conn.supervisor.shutdown_all(graceful_s=_SHUTDOWN_GRACE_S)
            log.info("system shutting down — gracefully stopped %d server(s)", n)
        else:
            log.info("agent stopping; servers left running")
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve


def main() -> None:
    _configure_logging()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
