"""Bounded HEAD-only diagnostics for owner-visible registered model endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from src.url_safety import _classify


_ENDPOINT_ID = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_SAFE_HEADERS = frozenset({
    'content-type', 'content-length', 'server', 'date',
    'cache-control', 'strict-transport-security',
})
_MAX_HEADER_BYTES = 16384
_MAX_ADDRESSES = 8
_TOTAL_SECONDS = 8.0


def _registered_url(owner: str | None, endpoint_id: str) -> str | None:
    from core.database import ModelEndpoint, SessionLocal
    from src.auth_helpers import owner_filter

    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == endpoint_id,
            ModelEndpoint.is_enabled == True,  # noqa: E712
        )
        row = owner_filter(query, ModelEndpoint, owner).first()
        return str(row.base_url) if row else None
    finally:
        db.close()


def _target(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Invalid registered endpoint URL') from exc
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if (scheme not in {'http', 'https'} or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.query
            or (port is not None and port <= 0)
            or any(char in host for char in '\r\n\x00')):
        raise ValueError('Registered endpoint URL cannot be probed')
    return scheme, host, port or (443 if scheme == 'https' else 80)


def _resolve(host: str, port: int) -> list[tuple[int, tuple, str]]:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not addresses or len(addresses) > _MAX_ADDRESSES:
        raise ValueError('Registered endpoint DNS resolution is unavailable or too broad')
    result = []
    for family, _kind, _proto, _canonical, sockaddr in addresses:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError('Unsupported endpoint address family')
        ip = ipaddress.ip_address(sockaddr[0].split('%', 1)[0])
        if _classify(ip, block_private=False):
            raise PermissionError('Registered endpoint resolves to a disallowed address')
        result.append((family, sockaddr, str(ip)))
    return result


def _read_headers(stream: socket.socket) -> tuple[int, dict[str, str]]:
    data = bytearray()
    while b'\r\n\r\n' not in data:
        part = stream.recv(min(4096, _MAX_HEADER_BYTES + 1 - len(data)))
        if not part:
            raise ConnectionError('HTTP response ended before headers')
        data.extend(part)
        if len(data) > _MAX_HEADER_BYTES:
            raise ValueError('HTTP headers exceed probe bound')
    head = bytes(data).split(b'\r\n\r\n', 1)[0]
    rows = head.decode('latin-1').split('\r\n')
    match = re.fullmatch(r'HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s+.*)?', rows[0])
    if not match:
        raise ValueError('Invalid HTTP response status')
    headers: dict[str, str] = {}
    for row in rows[1:]:
        if ':' not in row:
            continue
        name, value = row.split(':', 1)
        name = name.strip().lower()
        if name in _SAFE_HEADERS and name not in headers:
            headers[name] = value.strip()[:200]
    return int(match.group(1)), headers


def probe_url(url: str) -> dict:
    scheme, host, port = _target(url)
    started = time.monotonic()
    addresses = _resolve(host, port)
    dns_ms = round((time.monotonic() - started) * 1000, 1)
    deadline = started + _TOTAL_SECONDS
    last_error: Exception | None = None
    for index, (family, sockaddr, ip) in enumerate(addresses):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        per_ip = min(2.0, max(0.2, remaining / (len(addresses) - index)))
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(per_ip)
            connect_start = time.monotonic()
            sock.connect(sockaddr)
            connect_ms = round((time.monotonic() - connect_start) * 1000, 1)
            tls_version = None
            if scheme == 'https':
                tls_start = time.monotonic()
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                tls_version = sock.version()
                tls_ms = round((time.monotonic() - tls_start) * 1000, 1)
            else:
                tls_ms = None
            host_header = f'[{host}]' if ':' in host else host
            if port != (443 if scheme == 'https' else 80):
                host_header += f':{port}'
            sock.sendall((f'HEAD / HTTP/1.1\r\nHost: {host_header}\r\n'
                          'User-Agent: Odysseus-HttpProbe/1\r\nConnection: close\r\n\r\n').encode('ascii'))
            status, headers = _read_headers(sock)
            return {
                'scheme': scheme, 'host': host, 'port': port,
                'connected_ip': ip, 'resolved_ips': [item[2] for item in addresses],
                'status': status, 'headers': headers, 'dns_ms': dns_ms,
                'connect_ms': connect_ms, 'tls_ms': tls_ms, 'tls_version': tls_version,
                'total_ms': round((time.monotonic() - started) * 1000, 1),
                'redirect_followed': False, 'method': 'HEAD', 'exit_code': 0,
                'output': f'HEAD {scheme}://{host_header}/ → HTTP {status} in '
                          f'{round((time.monotonic() - started) * 1000, 1)} ms',
            }
        except (socket.timeout, TimeoutError, ConnectionError, OSError, ValueError, ssl.SSLError) as exc:
            last_error = exc
        finally:
            sock.close()
    if isinstance(last_error, ssl.SSLError):
        code = 'tls_error'
    elif isinstance(last_error, (socket.timeout, TimeoutError)) or time.monotonic() >= deadline:
        code = 'timeout'
    else:
        code = 'transport_unavailable'
    return {'error': 'Registered endpoint probe failed', 'code': code,
            'dns_ms': dns_ms, 'resolved_ips': [item[2] for item in addresses],
            'exit_code': 124 if code == 'timeout' else 1}


class HttpProbeTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content or '{}')
            if (not isinstance(args, dict) or set(args) != {'endpoint_id'}
                    or not isinstance(args['endpoint_id'], str)
                    or not _ENDPOINT_ID.fullmatch(args['endpoint_id'])):
                raise ValueError
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return {'error': 'Use a registered endpoint_id; arbitrary URLs and methods are not accepted',
                    'code': 'invalid_arguments', 'exit_code': 1}
        url = await asyncio.to_thread(_registered_url, ctx.get('owner'), args['endpoint_id'])
        if not url:
            return {'error': 'Registered endpoint is unavailable to this account',
                    'code': 'not_found', 'exit_code': 1}
        try:
            result = await asyncio.wait_for(asyncio.to_thread(probe_url, url), timeout=10)
        except asyncio.TimeoutError:
            return {'error': 'Registered endpoint probe timed out', 'code': 'timeout', 'exit_code': 124}
        except PermissionError:
            return {'error': 'Registered endpoint address is blocked', 'code': 'permission_denied', 'exit_code': 1}
        except (OSError, ValueError, UnicodeError):
            return {'error': 'Registered endpoint cannot be probed safely', 'code': 'invalid_arguments', 'exit_code': 1}
        result['endpoint_id'] = args['endpoint_id']
        return result
