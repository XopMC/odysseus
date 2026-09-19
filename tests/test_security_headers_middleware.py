# tests/test_security_headers_middleware.py
"""
Focused regression coverage for `SecurityHeadersMiddleware`
(core/middleware.py), added alongside the HSTS + Permissions-Policy
hardening:

  1. HSTS is emitted only when explicitly enabled for HTTPS requests,
     including requests arriving through a reverse proxy.
  2. Disabled HSTS is omitted instead of clearing another service's
     hostname-wide policy with `max-age=0`.
  3. `Permissions-Policy` locks down camera/geolocation but preserves
     same-origin microphone access (`microphone=(self)`), so the app's
     own voice/STT flow (`getUserMedia({ audio: true })`) keeps working.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware


def _build_app():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    return app


def _client(base_url="http://testserver"):
    return TestClient(_build_app(), base_url=base_url)


def test_hsts_absent_on_plain_http():
    response = _client().get("/")

    assert "strict-transport-security" not in response.headers


def test_hsts_absent_by_default_for_direct_https_requests():
    response = _client(base_url="https://testserver").get("/")

    assert "strict-transport-security" not in response.headers


def test_hsts_absent_by_default_via_x_forwarded_proto_https():
    response = _client().get("/", headers={"X-Forwarded-Proto": "https"})

    assert "strict-transport-security" not in response.headers


def test_hsts_is_not_cleared_for_explicit_dual_http_https_mode(monkeypatch):
    monkeypatch.setenv("HSTS_ENABLED", "false")
    response = _client(base_url="https://testserver").get("/")

    assert "strict-transport-security" not in response.headers


def test_hsts_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("HSTS_ENABLED", "true")
    response = _client(base_url="https://testserver").get("/")

    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_permissions_policy_locks_camera_and_geolocation_but_allows_self_microphone():
    response = _client().get("/")

    policy = response.headers["permissions-policy"]
    assert policy == "camera=(), microphone=(self), geolocation=()"

    # Explicitly pin the contract the reviewer flagged: an empty allowlist
    # would also block the app's own same-origin voice/STT button.
    assert "microphone=()" not in policy
    assert "microphone=(self)" in policy
