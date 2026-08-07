"""FastAPI application object for the MineManager hub.

To run the server, use the launcher that honors MM_HOST/MM_PORT:
    python -m minemanager_hub          # or the installed `minemanager-hub`

(The bare ``uvicorn minemanager_hub.main:app`` CLI is a separate entry point
that never sees our settings, so it binds uvicorn's own 127.0.0.1:8000 default.
Nothing here can change that. Use the launcher above; if you want the uvicorn
CLI for ``--reload`` during development, pass ``--host``/``--port`` yourself.)

In production this sits behind Auth Service + WireGuard; it does not authenticate
end users itself.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from minemanager_hub import __version__
from minemanager_hub.api import agent_ws, control, nodes, transfers, versions
from minemanager_hub.config import get_settings
from minemanager_hub.db.session import init_db
from minemanager_hub.providers.http import aclose as close_provider_http
from minemanager_hub.security import webguard

log = logging.getLogger("minemanager.hub")


def _configure_logging() -> None:
    root = logging.getLogger("minemanager")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    settings = get_settings()
    log.info(
        "hub %s starting: data_dir=%s host=%s port=%s cors=%s",
        __version__, settings.data_dir, settings.host, settings.port,
        settings.cors_origins or "same-origin only",
    )
    # A too-narrow MM_ALLOWED_HOSTS presents as a total outage with no other clue.
    log.info(
        "guards: allowed_hosts=%s docs=%s api_clients_without_origin=%s",
        "any (checks disabled)" if webguard.ANY_HOST in settings.allowed_hosts
        else ", ".join(sorted(settings.allowed_hosts)),
        "on" if settings.enable_docs else "off",
        "allowed" if settings.allow_api_clients else "blocked",
    )
    if settings.host not in ("127.0.0.1", "localhost", "::1") and \
            webguard.ANY_HOST not in settings.allowed_hosts and \
            not (settings.allowed_hosts - {"localhost", "127.0.0.1", "::1", "[::1]"}):
        log.warning(
            "bound to %s but MM_ALLOWED_HOSTS is still loopback-only — every request "
            "arriving under this hub's real hostname will be rejected with HTTP 400. "
            "Set MM_ALLOWED_HOSTS to the name clients use.",
            settings.host,
        )

    init_db()                                # logs the DB path + node count
    log.info("web: %s", _web_dir_status)
    yield
    await close_provider_http()


_settings = get_settings()

# Resolved before the app exists so `lifespan` can log it: the mount itself has
# to happen after every router is registered, but the *decision* does not.
_web_dir = _settings.web_dir
_web_dir_mounted = _web_dir.is_dir()
_web_dir_status = (
    f"serving UI from {_web_dir}" if _web_dir_mounted
    else f"UI NOT mounted - {_web_dir} is not a directory (API-only)"
)

app = FastAPI(
    title="MineManager Hub",
    version=__version__,
    lifespan=lifespan,
    # Opt-in (MM_ENABLE_DOCS=1): with no app-layer auth they publish a complete
    # map of the attack surface to anyone who can reach the hub.
    docs_url="/docs" if _settings.enable_docs else None,
    redoc_url="/redoc" if _settings.enable_docs else None,
    openapi_url="/openapi.json" if _settings.enable_docs else None,
)

# CORS for UI dev only (empty in production — UI is served same-origin).
_origins = get_settings().cors_origins
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Added after CORSMiddleware so it runs *outside* it: a rejected cross-site
# request must not be handed CORS headers on the way out.
app.add_middleware(
    webguard.OriginGuard,
    extra_origins=set(_settings.cors_origins),
    allow_api_clients=_settings.allow_api_clients,
)
app.add_middleware(webguard.HostGuard, allowed=_settings.allowed_hosts)

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

    Mounted at ``/`` it catches anything the routers did not match. Plain
    ``StaticFiles`` ``assert``s on a websocket scope, turning a misrouted WS path
    (an agent pointed at ``/ws`` rather than ``/ws/agent``) into an opaque 500.
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
if _web_dir_mounted:
    app.mount("/", _SpaStatic(_web_dir), name="web")
