"""FastAPI application entrypoint for the MineManager hub.

Run (dev):
    MM_DATA_DIR=./_devdata uvicorn minemanager_hub.main:app --reload --port 8730

In production this sits behind Authelia + WireGuard; it does not authenticate
end users itself.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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


# Static web UI, served same-origin so the browser needs no CORS and the reverse
# proxy authenticates UI and API together. Mounted last so every /api, /ws and
# /docs route is matched first; skipped entirely when the directory is absent
# (e.g. an API-only deployment).
_web_dir = get_settings().web_dir
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
