from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_dual_protocol_overlay_keeps_uvicorn_private_and_allows_http_cookie():
    compose = (ROOT / "docker-compose.https.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${APP_HTTP_PORT:-5131}:7000"' in compose
    assert 'SECURE_COOKIES: "false"' in compose
    assert 'HSTS_ENABLED: "false"' in compose
    assert "network_mode: host" in compose
    assert '${HTTPS_PORT:-5130}' in compose
    assert "haproxy:3.2-alpine" in compose
    assert "haproxy.cfg" in compose


def test_https_proxy_preserves_streaming():
    caddyfile = (ROOT / "deploy/https/Caddyfile").read_text(encoding="utf-8")
    assert "tls /etc/odysseus-tls/cert.pem /etc/odysseus-tls/key.pem" in caddyfile
    assert "reverse_proxy" in caddyfile
    assert "flush_interval -1" in caddyfile
    assert "X-Forwarded-Proto https" in caddyfile
    assert "http_redirect" not in caddyfile
    assert "TLS_BACKEND_PORT:5133" in caddyfile
    assert "protocols h1 h2" in caddyfile


def test_same_public_port_multiplexes_plain_http_and_tls():
    config = (ROOT / "deploy/https/haproxy.cfg").read_text(encoding="utf-8")
    assert "bind :${PUBLIC_PORT}" in config
    assert "req.ssl_hello_type 1" in config
    assert "default_backend odysseus_http" in config
    assert "127.0.0.1:${TLS_BACKEND_PORT}" in config
    assert "127.0.0.1:${APP_HTTP_PORT}" in config


def test_public_route_installer_is_port_scoped_and_persistent():
    script = (ROOT / "scripts/install-public-port-route.sh").read_text(encoding="utf-8")
    assert '--sport "$PORT"' in script
    assert 'fwmark "$PACKET_MARK"' in script
    assert "systemctl enable --now odysseus-public-port-route.service" in script
    assert "iptables -t mangle -F" not in script
