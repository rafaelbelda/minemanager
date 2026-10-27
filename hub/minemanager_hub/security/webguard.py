"""Request guards for the hub: proxy trust, ``Host``, ``Origin``, headers.

:class:`ProxyGuard` is the authentication boundary. The hub has no user auth of
its own, so everything but the agent paths must arrive through the reverse
proxy that does it, proven by a shared secret only that proxy adds.

``MM_ALLOW_API_CLIENTS`` decides whether requests with no ``Origin`` (curl,
scripts) may make state-changing calls. Off by default.

Agent paths authenticate themselves with a node credential rather than an
origin or the proxy, and are used by non-browser clients that legitimately send
no ``Origin``: the agent transport and the internal transfer data plane.
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import urlparse

log = logging.getLogger("minemanager.hub")

# Methods that change state. GET/HEAD/OPTIONS are safe to serve cross-origin:
# the browser still cannot read the response without CORS.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

PROXY_SECRET_HEADER = "x-minemanager-proxy-secret"

MIN_PROXY_SECRET_LEN = 32

# Wildcard entry that turns the Host allowlist off entirely.
ANY_HOST = "*"


def is_agent_path(path: str) -> bool:
    """Paths authenticated by a node credential rather than the proxy."""
    return path == "/ws/agent" or path.startswith("/api/internal/transfer/")


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return ""


def _hostname(value: str) -> str:
    """Bare lowercase hostname from a ``Host`` header or an origin's netloc."""
    value = (value or "").strip().lower()
    value = value.rpartition("@")[2]               # drop any userinfo
    if value.startswith("["):                      # [::1]:8730, IPv6 literal
        return value.partition("]")[0].lstrip("[")
    if value.count(":") > 1:                       # ::1, bare IPv6, no port
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
            exempt = is_agent_path(path)
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


_LOOPBACK_BINDS = frozenset({"127.0.0.1", "localhost", "::1"})


def check_proxy_config(settings) -> None:
    """Refuse to start in a configuration that has no authentication boundary.

    A hub bound beyond loopback with no proxy secret is reachable, with full
    control, by anything on that network; the reverse proxy in front of it
    protects only the requests that happen to go through it.
    """
    secret = settings.proxy_secret
    if secret is not None and len(secret) < MIN_PROXY_SECRET_LEN:
        raise SystemExit(
            f"MM_PROXY_SECRET is too short ({len(secret)} characters; at least "
            f"{MIN_PROXY_SECRET_LEN}). Generate one with: openssl rand -hex 32"
        )
    if settings.proxy_user_header and not secret:
        raise SystemExit(
            "MM_PROXY_USER_HEADER is set without MM_PROXY_SECRET. Any client could then "
            "send that header itself and name any user; set MM_PROXY_SECRET too."
        )
    if secret or settings.host in _LOOPBACK_BINDS:
        return
    if settings.allow_unauthenticated:
        log.warning("MM_ALLOW_UNAUTHENTICATED is set: every host that can reach %s has "
                    "full control of this hub", settings.host)
        return
    raise SystemExit(
        f"refusing to start: MM_HOST={settings.host} is not loopback and MM_PROXY_SECRET is "
        f"unset, so anything that can reach that address would bypass the reverse proxy's "
        f"authentication and control every node. Set MM_PROXY_SECRET and have the proxy "
        f"send it (see deploy/README.md), or set MM_ALLOW_UNAUTHENTICATED=1 to accept that."
    )


class ProxyGuard:
    """Admit only requests carrying the proxy's shared secret.

    Agent paths are exempt (node credential). ``user_header`` is trusted only
    after the secret matched.
    """

    def __init__(self, app, secret: str | None, user_header: str | None = None) -> None:
        self.app = app
        self.secret = secret.encode("utf-8") if secret else None   # vs raw header bytes
        self.user_header = user_header.strip().lower().encode("latin-1") if user_header else None
        self._warned = False

    async def __call__(self, scope, receive, send):
        if (
            self.secret is None
            or scope.get("type") not in ("http", "websocket")
            or is_agent_path(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        presented = _header(scope, PROXY_SECRET_HEADER.encode()).encode("latin-1")
        if not hmac.compare_digest(presented, self.secret):
            if not self._warned:
                self._warned = True
                log.warning(
                    "rejected a request that did not come through the authenticating proxy "
                    "(missing or wrong %s). If every request fails this way, the proxy is not "
                    "setting the header to MM_PROXY_SECRET.", PROXY_SECRET_HEADER,
                )
            await _reject(scope, receive, send, 401,
                          "This hub only accepts requests forwarded by its authenticating "
                          "reverse proxy.")
            return

        if self.user_header is not None:
            user = _header(scope, self.user_header)
            if not user:
                await _reject(scope, receive, send, 401,
                              "The reverse proxy forwarded this request without an "
                              "authenticated user.")
                return
            if scope.get("method", "") in _UNSAFE_METHODS:
                log.info("%s %s by %r", scope["method"], scope.get("path", ""), user)

        await self.app(scope, receive, send)


_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
)

# Swagger UI/ReDoc need CDN and inline scripts.
_NO_CSP_PATHS = ("/docs", "/redoc")


class SecurityHeaders:
    """Browser hardening headers on every HTTP response, including rejections."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        extra = list(_SECURITY_HEADERS)
        if not scope.get("path", "").startswith(_NO_CSP_PATHS):
            extra.append((b"content-security-policy", _CSP.encode()))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in extra if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
