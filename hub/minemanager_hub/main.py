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

from minemanager_hub import __version__
from minemanager_hub.api import agent_ws, control, nodes
from minemanager_hub.config import get_settings
from minemanager_hub.db.session import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}
