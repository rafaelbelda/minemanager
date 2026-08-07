"""``Host`` and ``Origin`` guards for the hub.

Check ``Host`` and ``Origin``.

``MM_ALLOW_API_CLIENTS`` decides whether requests with no ``Origin`` (curls, scripts) 
may make state-changing calls; it is off by default, so the safe posture
needs no configuration and the convenient one is a deliberate choice.

Exempt paths authenticate themselves with a node credential rather than an
origin, and are used by non-browser clients that legitimately send no ``Origin``:
the agent transport and the internal transfer data plane.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

log = logging.getLogger("minemanager.hub")

# Methods that change state. GET/HEAD/OPTIONS are safe to serve cross-origin:
# the browser still cannot *read* the response without CORS.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Authenticated by the per-node credential (see transfers._auth_agent and the
# agent handshake), and reached by clients that never send an Origin.
_EXEMPT_PREFIXES = ("/ws/agent", "/api/internal/")

# Wildcard entry that turns the Host allowlist off entirely.
ANY_HOST = "*"


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return ""


def _hostname(value: str) -> str:
    # Bare lowercase hostname from a ``Host`` header or an origin's netloc.
    value = (value or "").strip().lower()
    if value.startswith("["):                      # [::1]:8730 — IPv6 literal
        return value.partition("]")[0].lstrip("[")
    if value.count(":") > 1:                       # ::1 — bare IPv6, no port
        return value
    return value.partition(":")[0]


def origin_allowed(origin: str, host_header: str, extra: set[str]) -> bool:
    """True when ``origin`` is same-origin with ``Host``, or explicitly allowed.

    The scheme is deliberately ignored for the same-origin comparison: behind a
    TLS-terminating reverse proxy the browser sends ``https://…`` while the hub
    itself sees plain http, so comparing schemes would reject the real UI.
    ``extra`` (from ``MM_CORS_ORIGINS``) is matched exactly, scheme included,
    since those are explicitly configured.
    """
    if origin in extra:
        return True
    parsed = urlparse(origin)
    if not parsed.netloc:
        return False
    return _hostname(parsed.netloc) == _hostname(host_header) and bool(host_header)


async def _reject(scope, receive, send, status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        await receive()                            # consume websocket.connect
        await send({"type": "websocket.close", "code": 1008})
        return
    body = detail.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class HostGuard:
    """Reject requests whose ``Host`` is not one we expect (anti DNS-rebinding)."""

    def __init__(self, app, allowed: set[str]) -> None:
        self.app = app
        # Falsy entries are dropped: one that survived normalisation would match
        # a request with no Host header at all, which is the one case this guard
        # must never allow.
        self.allowed = (
            None if ANY_HOST in allowed
            else {h for h in (_hostname(a) for a in allowed) if h}
        )
        self._warned: set[str] = set()

    async def __call__(self, scope, receive, send):
        if self.allowed is not None and scope["type"] in ("http", "websocket"):
            host = _hostname(_header(scope, b"host"))
            if not host or host not in self.allowed:
                # Log once per distinct host: a misconfigured MM_ALLOWED_HOSTS
                # otherwise looks like a total outage with no explanation.
                if host not in self._warned:
                    self._warned.add(host)
                    log.warning(
                        "rejected a request with Host=%r (allowed: %s). If this is a legitimate "
                        "address for this hub, add it to MM_ALLOWED_HOSTS.",
                        host or "<empty>", ", ".join(sorted(self.allowed)) or "<none>",
                    )
                await _reject(scope, receive, send, 400,
                              f"Host {host or '<empty>'!r} is not allowed. Set MM_ALLOWED_HOSTS.")
                return
        await self.app(scope, receive, send)


class OriginGuard:
    """Enforce same-origin on state-changing requests and on UI WebSockets."""

    def __init__(self, app, extra_origins: set[str], allow_api_clients: bool) -> None:
        self.app = app
        self.extra = extra_origins
        self.allow_api_clients = allow_api_clients

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind in ("http", "websocket"):
            path = scope.get("path", "")
            exempt = path.startswith(_EXEMPT_PREFIXES)
            checked = kind == "websocket" or scope.get("method", "") in _UNSAFE_METHODS
            if checked and not exempt:
                origin = _header(scope, b"origin")
                if origin:
                    if not origin_allowed(origin, _header(scope, b"host"), self.extra):
                        log.warning("rejected %s %s from cross-site Origin %r",
                                    scope.get("method", kind), path, origin)
                        await _reject(scope, receive, send, 403,
                                      f"Origin {origin!r} is not allowed for this hub.")
                        return
                elif not self.allow_api_clients:
                    await _reject(
                        scope, receive, send, 403,
                        "This request carries no Origin header, so it did not come from the web "
                        "UI. Non-browser clients (curl, scripts, CI) may perform state-changing "
                        "requests only when MM_ALLOW_API_CLIENTS=1 is set on the hub.",
                    )
                    return
        await self.app(scope, receive, send)
