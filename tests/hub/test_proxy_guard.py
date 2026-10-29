"""The proxy trust boundary, and the browser headers on every response.

The hub has no user authentication of its own; the reverse proxy in front does
it. Before ProxyGuard, anything that could open a TCP connection to the hub's
port (a Minecraft plugin on the same machine, a WireGuard peer) skipped that
proxy and had full control, because the Host and Origin checks are trivially
satisfied by a non-browser client.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from minemanager_hub.security import webguard
from minemanager_hub.security.webguard import PROXY_SECRET_HEADER, ProxyGuard

SECRET = "s" * 40


def _app(**guard) -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    async def ws_ok(websocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    app = Starlette(routes=[
        Route("/api/nodes", ok, methods=["GET", "POST"]),
        Route("/ws/agentX", ok),
        Route("/api/internal/transfer/{tid}", ok, methods=["GET", "POST"]),
        WebSocketRoute("/ws/agent", ws_ok),
        WebSocketRoute("/api/nodes/n1/events", ws_ok),
    ])
    return TestClient(ProxyGuard(app, **guard))


class TestProxyGuard:
    def test_a_request_without_the_secret_is_refused(self):
        # The attack: curl straight at 127.0.0.1:8730, past the proxy.
        assert _app(secret=SECRET).post("/api/nodes").status_code == 401

    def test_a_wrong_secret_is_refused(self):
        r = _app(secret=SECRET).get("/api/nodes", headers={PROXY_SECRET_HEADER: "s" * 39})
        assert r.status_code == 401

    def test_the_proxy_secret_admits(self):
        r = _app(secret=SECRET).get("/api/nodes", headers={PROXY_SECRET_HEADER: SECRET})
        assert r.status_code == 200

    def test_ui_websockets_need_it_too(self):
        with pytest.raises(WebSocketDisconnect):
            with _app(secret=SECRET).websocket_connect("/api/nodes/n1/events") as ws:
                ws.receive_text()

    def test_agent_paths_are_exempt(self):
        # Agents cannot pass the proxy's interactive login; they authenticate
        # with their node credential instead.
        c = _app(secret=SECRET)
        assert c.post("/api/internal/transfer/t1").status_code == 200
        with c.websocket_connect("/ws/agent") as ws:
            assert ws.receive_text() == "hi"

    def test_the_exemption_is_exact(self):
        assert _app(secret=SECRET).get("/ws/agentX").status_code == 401

    def test_no_secret_configured_is_the_old_behaviour(self):
        assert _app(secret=None).post("/api/nodes").status_code == 200

    def test_a_required_user_header_must_be_present(self):
        c = _app(secret=SECRET, user_header="Remote-User")
        assert c.get("/api/nodes", headers={PROXY_SECRET_HEADER: SECRET}).status_code == 401
        r = c.get("/api/nodes", headers={PROXY_SECRET_HEADER: SECRET, "remote-user": "alice"})
        assert r.status_code == 200

    def test_a_user_header_alone_proves_nothing(self):
        # Identity is believed only alongside the secret, so a client cannot
        # authenticate by naming a user itself.
        c = _app(secret=SECRET, user_header="Remote-User")
        assert c.get("/api/nodes", headers={"remote-user": "admin"}).status_code == 401


def _settings(**kw):
    base = dict(proxy_secret=None, proxy_user_header=None, host="127.0.0.1",
                allow_unauthenticated=False)
    base.update(kw)
    return SimpleNamespace(**base)


class TestStartupChecks:
    def test_loopback_without_a_secret_starts(self):
        webguard.check_proxy_config(_settings())

    def test_a_non_loopback_bind_without_a_secret_refuses_to_start(self):
        with pytest.raises(SystemExit, match="MM_PROXY_SECRET"):
            webguard.check_proxy_config(_settings(host="10.0.0.5"))

    def test_the_explicit_opt_out_is_honoured(self):
        webguard.check_proxy_config(_settings(host="0.0.0.0", allow_unauthenticated=True))

    def test_a_short_secret_is_refused(self):
        with pytest.raises(SystemExit, match="too short"):
            webguard.check_proxy_config(_settings(proxy_secret="changeme"))

    def test_a_user_header_without_a_secret_is_refused(self):
        with pytest.raises(SystemExit, match="MM_PROXY_SECRET"):
            webguard.check_proxy_config(_settings(proxy_user_header="Remote-User"))

    def test_a_good_secret_on_any_bind_starts(self):
        webguard.check_proxy_config(_settings(host="10.0.0.5", proxy_secret=SECRET))


class TestWiring:
    def test_the_real_app_installs_the_guards(self):
        from minemanager_hub.main import app

        classes = [m.cls for m in app.user_middleware]
        # Outermost first: headers, then Host, then the proxy boundary.
        assert classes.index(webguard.SecurityHeaders) < classes.index(webguard.HostGuard)
        assert classes.index(webguard.HostGuard) < classes.index(webguard.ProxyGuard)
        assert classes.index(webguard.ProxyGuard) < classes.index(webguard.OriginGuard)


class TestSecurityHeaders:
    def test_api_responses_carry_them(self, client):
        h = client.get("/api/health").headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        # Pins the UI to its own hub: no other host can be scripted or fetched.
        assert "connect-src 'self'" in h["content-security-policy"]
        assert "script-src 'self'" in h["content-security-policy"]

    def test_guard_rejections_carry_them_too(self, client):
        r = client.get("/api/health", headers={"host": "evil.com"})
        assert r.status_code == 400
        assert r.headers["x-content-type-options"] == "nosniff"
