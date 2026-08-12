"""Host and Origin guards.

With no app-layer auth these are the only controls left once a hostile page is
in the operator's browser, so both known bypasses are pinned here: an empty
``Host`` matching an allowlist entry that normalised to ``""``, and userinfo in
an ``Origin`` impersonating the host.
"""

from __future__ import annotations

import pytest

from minemanager_hub.security.webguard import ANY_HOST, HostGuard, _hostname, origin_allowed

DEFAULT_ALLOWED = {"localhost", "127.0.0.1", "::1", "[::1]"}


class TestHostname:
    @pytest.mark.parametrize("raw,want", [
        ("example.com", "example.com"),
        ("example.com:8730", "example.com"),
        ("EXAMPLE.com", "example.com"),
        ("  example.com  ", "example.com"),
        ("[::1]:8730", "::1"),
        ("[::1]", "::1"),
        ("::1", "::1"),                       # bare IPv6, as written in config
        ("", ""),
        ("user:pass@example.com", "example.com"),
        ("localhost:x@evil.com", "evil.com"),  # userinfo must not become the host
    ])
    def test_normalisation(self, raw, want):
        assert _hostname(raw) == want


class TestHostGuard:
    def test_empty_host_is_never_allowed(self):
        guard = HostGuard(None, DEFAULT_ALLOWED)
        assert "" not in guard.allowed

    def test_ipv6_loopback_survives_normalisation(self):
        assert "::1" in HostGuard(None, DEFAULT_ALLOWED).allowed

    def test_wildcard_disables_the_check(self):
        assert HostGuard(None, {ANY_HOST}).allowed is None

    def test_allowlist_is_normalised(self):
        assert HostGuard(None, {"Example.COM:8730"}).allowed == {"example.com"}


class TestOriginAllowed:
    @pytest.mark.parametrize("origin,host,want", [
        ("http://localhost:8730", "localhost:8730", True),
        ("https://mm.example.com", "mm.example.com", True),   # TLS-terminating proxy
        ("http://[::1]:8730", "[::1]:8730", True),
        ("http://evil.com", "localhost:8730", False),
        ("http://a.localhost", "localhost", False),           # subdomain is not same-origin
        ("http://localhost:x@evil.com", "localhost:8730", False),  # userinfo spoof
        ("", "localhost", False),
        ("null", "localhost", False),
    ])
    def test_same_origin_rule(self, origin, host, want):
        assert origin_allowed(origin, host, set()) is want

    def test_configured_extra_origin_matches_exactly(self):
        extra = {"http://localhost:5173"}
        assert origin_allowed("http://localhost:5173", "mm.example.com", extra) is True
        assert origin_allowed("https://localhost:5173", "mm.example.com", extra) is False

    def test_empty_host_never_matches(self):
        assert origin_allowed("http://localhost", "", set()) is False


class TestThroughTheApp:
    def test_cross_origin_write_rejected(self, client):
        r = client.post("/api/nodes", json={"name": "x"}, headers={"origin": "http://evil.com"})
        assert r.status_code == 403

    def test_same_origin_write_allowed(self, client):
        r = client.post("/api/nodes", json={"name": "x"}, headers={"origin": "http://testserver"})
        assert r.status_code == 201

    def test_disallowed_host_rejected(self, client):
        assert client.get("/api/health", headers={"host": "evil.com"}).status_code == 400

    def test_reads_are_not_origin_checked(self, client):
        """GET cannot be read cross-origin without CORS, so it stays open."""
        assert client.get("/api/health", headers={"origin": "http://evil.com"}).status_code == 200
