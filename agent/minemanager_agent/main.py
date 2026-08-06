"""Agent entrypoint.

Run (dev):
    MM_HUB_URL=ws://127.0.0.1:8730/ws/agent MM_ENROLL_TOKEN=<token> \
        python -m minemanager_agent.main

Under systemd this is the ExecStart target — the only daemon on the node.

On SIGTERM/SIGINT (systemctl stop, restart, or a system reboot) the agent
gracefully stops every running server — console stop, wait for a clean world
save (bounded) — then tears down its tmux server and exits. Stopping the agent
always stops its servers: simpler and more predictable than trying to keep them
running across the agent's own lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket

from minemanager_agent import tmux
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


async def _warn_about_orphans(prefix: str) -> None:
    """Sanity check: live sessions on our socket that our prefix will not match.

    ``pin_runtime_identity`` is what actually *prevents* a mid-flight prefix or
    socket change from orphaning servers. This stays as a backstop, because a
    session can end up unmatched by routes it does not cover — a hand-started
    ``tmux -L minemanager`` session, or a ``runtime.json`` that was deleted or
    edited by hand.
    """
    try:
        names = await tmux.list_all_sessions()
    except Exception:  # noqa: BLE001 - never block startup on a diagnostic
        return
    orphans = [n for n in names if not n.startswith(prefix + "-")]
    if orphans:
        log.warning(
            "%d tmux session(s) on socket '%r' do not match session prefix '%r' and will NOT be "
            "managed or stopped by this agent: '%s' - check MM_SESSION_PREFIX/MM_TMUX_SOCKET, or "
            "stop them with: tmux -L '%s' kill-session -t <name>",
            len(orphans), tmux.socket_name(), prefix, ", ".join(orphans), tmux.socket_name(),
        )


async def _amain() -> None:
    config = AgentConfig()
    config.ensure_dirs()
    config.check_transport_security()   # exits on plaintext to a remote hub
    # Pin prefix/socket before the supervisor is built from them, so an edited
    # env var cannot detach this agent from servers it is already supervising.
    await config.pin_runtime_identity()
    config.log_resolved()
    await _warn_about_orphans(config.session_prefix)
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

    # Always stop servers gracefully — including when `serve` died on its own
    # (a crashed connection loop must never leave worlds running unsupervised).
    try:
        n = await conn.supervisor.shutdown_all(graceful_s=_SHUTDOWN_GRACE_S)
        log.info("agent stopping — gracefully stopped %d server(s)", n)
    except Exception:
        log.exception("graceful shutdown failed; servers may still be running")
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
