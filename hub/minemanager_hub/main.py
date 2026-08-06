"""FastAPI application object for the MineManager hub.

To run the server, use the launcher that honors MM_HOST/MM_PORT:
    python -m minemanager_hub          # or the installed `minemanager-hub`

The bare ``uvicorn minemanager_hub.main:app`` CLI ignores MM_HOST/MM_PORT (it
uses uvicorn's own 127.0.0.1:8000 default); if you use it for --reload during
dev, pass ``--host``/``--port`` explicitly.

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
from minemanager_hub.security import vault, webguard

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
    # State the resolved configuration
    log.info(
        "hub %s starting: data_dir=%s host=%s port=%s cors=%s",
        __version__, settings.data_dir, settings.host, settings.port,
        settings.cors_origins or "same-origin only",
    )
    # Say exactly which requests will be accepted: a too-narrow MM_ALLOWED_HOSTS
    # presents as a total outage, and there is no other clue why.
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
    vault.verify_existing_secrets_readable()  # exits if the key can't read them
    log.info("web: %s", _web_dir_status)
    yield
    await close_provider_http()


_settings = get_settings()

app = FastAPI(
    title="MineManager Hub",
    version=__version__,
    lifespan=lifespan,
    # Interactive docs are opt-in (MM_ENABLE_DOCS=1). With no app-layer auth they
    # publish a complete, accurate map of the attack surface to anyone who can
    # reach the hub — including a page that got there via DNS rebinding (S-17).
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

# Same-origin enforcement. Added after CORSMiddleware so it runs *outside* it:
# a rejected cross-site request must not be handed CORS headers on the way out.
# These are the only controls left once a hostile page is in the operator's
# browser — CORS covers neither WebSockets nor bodyless POSTs. See webguard.
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
    _web_dir_status = f"serving UI from {_web_dir}"
else:
    _web_dir_status = f"UI NOT mounted - {_web_dir} is not a directory (API-only)"
