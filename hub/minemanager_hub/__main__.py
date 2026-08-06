"""Run the hub honoring MM_HOST / MM_PORT (and the rest of the env config).

    python -m minemanager_hub        # or the installed `minemanager-hub` script

This is the launcher that actually binds to the configured host/port. The bare
``uvicorn minemanager_hub.main:app`` CLI does NOT read MM_HOST/MM_PORT — it uses
uvicorn's own defaults (127.0.0.1:8000) — so use this entrypoint (or pass
``--host``/``--port`` to the uvicorn CLI explicitly).
"""

from __future__ import annotations

import uvicorn

from minemanager_hub.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "minemanager_hub.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        # Bound what an agent can push in a single frame. uvicorn already
        # defaults to 16 MB, but state it explicitly and tie it to the transfer
        # cap so raising MM_TRANSFER_CAP_BYTES cannot silently start rejecting
        # legitimate base64 responses (base64 is 4/3 of the payload, plus JSON
        # envelope), while a compromised node still cannot push arbitrary size.
        ws_max_size=max(16 * 1024 * 1024, settings.transfer_cap_bytes * 2),
    )


if __name__ == "__main__":
    main()
