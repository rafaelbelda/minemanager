"""Agent entrypoint.

Run (dev):
    MM_HUB_URL=ws://127.0.0.1:8730/ws/agent MM_ENROLL_TOKEN=<token> \
        python -m minemanager_agent.main

Under systemd this is the ExecStart target — the only daemon on the node.
"""

from __future__ import annotations

import asyncio
import logging
import socket

from minemanager_agent.config import AgentConfig
from minemanager_agent.connection import Connection


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _amain() -> None:
    config = AgentConfig()
    config.ensure_dirs()
    conn = Connection(config, hostname=socket.gethostname())
    await conn.run_forever()


def main() -> None:
    _configure_logging()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
