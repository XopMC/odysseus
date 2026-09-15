from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_https_overlay_keeps_uvicorn_private_and_enables_secure_cookies():
    compose = (ROOT / "docker-compose.https.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${APP_HTTP_PORT:-5131}:7000"' in compose
    assert 'SECURE_COOKIES: "true"' in compose
    assert "network_mode: host" in compose
    assert '${HTTPS_PORT:-5130}' in compose


def test_https_proxy_preserves_streaming():
    caddyfile = (ROOT / "deploy/https/Caddyfile").read_text(encoding="utf-8")
    assert "tls /etc/odysseus-tls/cert.pem /etc/odysseus-tls/key.pem" in caddyfile
    assert "reverse_proxy" in caddyfile
    assert "flush_interval -1" in caddyfile
    assert "X-Forwarded-Proto https" in caddyfile
    assert "http_redirect" in caddyfile
    assert "protocols h1 h2" in caddyfile


def test_public_route_installer_is_port_scoped_and_persistent():
    script = (ROOT / "scripts/install-public-port-route.sh").read_text(encoding="utf-8")
    assert '--sport "$PORT"' in script
    assert 'fwmark "$PACKET_MARK"' in script
    assert "systemctl enable --now odysseus-public-port-route.service" in script
    assert "iptables -t mangle -F" not in script
