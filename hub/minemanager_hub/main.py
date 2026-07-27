"""FastAPI application object for the MineManager hub.

To run the server, use the launcher that honors MM_HOST/MM_PORT:
    python -m minemanager_hub          # or the installed `minemanager-hub`

The bare ``uvicorn minemanager_hub.main:app`` CLI ignores MM_HOST/MM_PORT (it
uses uvicorn's own 127.0.0.1:8000 default); if you use it for --reload during
dev, pass ``--host``/``--port`` explicitly.

In production this sits behind Authelia + WireGuard; it does not authenticate
end users itself.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from minemanager_hub import __version__
from minemanager_hub.api import agent_ws, control, nodes, transfers, versions
from minemanager_hub.config import get_settings
from minemanager_hub.db.session import init_db
from minemanager_hub.providers.http import aclose as close_provider_http


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield
    await close_provider_http()


app = FastAPI(title="MineManager Hub", version=__version__, lifespan=lifespan)

# CORS for UI dev only (empty in production — UI is served same-origin).
_origins = get_settings().cors_origins
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Agent transport (agents dial in here).
app.include_router(agent_ws.router, prefix="/ws")
# REST + UI event stream.
app.include_router(nodes.router)
app.include_router(control.router)
app.include_router(versions.router)
app.include_router(transfers.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


class _SpaStatic:
    """StaticFiles SPA fallback that refuses WebSocket scopes.

    Mounted at ``/`` it is the catch-all for anything the routers didn't match.
    Plain ``StaticFiles`` only handles HTTP and ``assert``s on a websocket scope,
    turning a mistyped/misrouted WS path (e.g. an agent pointed at ``/ws`` instead
    of ``/ws/agent``) into an opaque 500. Here we reject stray websockets cleanly
    (close 1008) and serve the SPA for HTTP.
    """

    def __init__(self, directory) -> None:
        self._static = StaticFiles(directory=directory, html=True)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await receive()  # consume websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._static(scope, receive, send)


# Static web UI, served same-origin so the browser needs no CORS and the reverse
# proxy authenticates UI and API together. Mounted last so every /api, /ws and
# /docs route is matched first; skipped entirely when the directory is absent
# (e.g. an API-only deployment).
_web_dir = get_settings().web_dir
if _web_dir.is_dir():
    app.mount("/", _SpaStatic(_web_dir), name="web")
