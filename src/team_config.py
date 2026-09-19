"""Team feature discovery and server-owned model resolution (never exposes keys)."""
import ipaddress
import json
import os
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlsplit, urlunsplit


PRESETS = [
    {'id': 'coding', 'label': 'Кодинг', 'config': {'reviewer': True, 'web': False}},
    {'id': 'bugfix', 'label': 'Исправить баг', 'config': {'reviewer': True, 'web': False}},
    {'id': 'research', 'label': 'Исследование', 'config': {'reviewer': True, 'web': True}},
    {'id': 'review', 'label': 'Ревью', 'config': {'reviewer': True, 'web': False}},
    {'id': 'admin', 'label': 'Администрирование', 'config': {'reviewer': True, 'web': False}},
]


def enabled():
    return os.environ.get('ODYSSEUS_TEAM_ENABLED', '').lower() in {'1', 'true', 'yes'}


def resource_group(url):
    host = (urlsplit(url).hostname or '').lower()
    # Operator-controlled aliases: endpoints to the same physical backend
    # must share a slot, even through host.docker.internal or another port.
    aliases = json.loads(os.environ.get('ODYSSEUS_TEAM_RESOURCE_ALIASES', '{}'))
    if host in aliases:
        return str(aliases[host])
    if host in {'192.168.50.6', 'host.docker.internal', 'localhost', '127.0.0.1', '::1'}:
        return 'jetson'
    return host


def local_endpoint(url, kind='auto'):
    if kind in {'api', 'proxy'}:
        return False
    if kind == 'local':
        return True
    host = (urlsplit(url).hostname or '').lower()
    if host in {'localhost', 'host.docker.internal'}:
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback
    except ValueError:
        # Unknown DNS names are external until operator explicitly marks local.
        return False


def _model_names(endpoint):
    names = []
    for field in ('cached_models', 'pinned_models'):
        try:
            values = json.loads(getattr(endpoint, field, '') or '[]')
            for item in values if isinstance(values, list) else []:
                name = item.get('id') if isinstance(item, dict) else item
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
        except (ValueError, TypeError):
            pass
    try:
        hidden = json.loads(endpoint.hidden_models or '[]')
        if not isinstance(hidden, list):
            hidden = []
    except (TypeError, ValueError):
        hidden = []
    return [name for name in names if name not in hidden]


def models(owner):
    from core.database import ModelEndpoint, SessionLocal
    with SessionLocal() as db:
        records = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()
        result = []
        for endpoint in records:
            if endpoint.owner not in (None, owner) or endpoint.model_type not in (None, 'llm'):
                continue
            for name in _model_names(endpoint):
                result.append({'endpoint_id': endpoint.id, 'model': name,
                               'label': endpoint.name + ' · ' + name,
                               'local': local_endpoint(endpoint.base_url, endpoint.endpoint_kind),
                               'resource_group': resource_group(endpoint.base_url)})
        return result


def refresh_models(owner, *, timeout=15, probe=None, key_resolver=None):
    """Synchronously refresh the owner-visible Team model inventory.

    The ordinary Team listing stays cached and instant.  This explicit path is
    called only from the user's Refresh models action and preserves a previous
    non-empty cache when an endpoint is temporarily offline.
    """
    from core.database import ModelEndpoint, SessionLocal
    if probe is None or key_resolver is None:
        from routes.model_routes import _probe_endpoint, _resolve_probe_key
        probe = probe or _probe_endpoint
        key_resolver = key_resolver or _resolve_probe_key

    refreshed = 0
    failed = []
    with SessionLocal() as db:
        records = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()
        visible = [endpoint for endpoint in records
                   if endpoint.owner in (None, owner)
                   and endpoint.model_type in (None, 'llm')]

        # An owner can have many configured endpoints. Probing them serially
        # makes the explicit refresh exceed the app-wide 45 second request
        # deadline as soon as a few offline endpoints each consume their
        # timeout. Resolve credentials on the request thread, then perform
        # bounded independent probes in parallel. Database objects are only
        # mutated back on this thread.
        jobs = []
        for endpoint in visible:
            try:
                jobs.append((endpoint, endpoint.base_url, key_resolver(endpoint)))
            except Exception:
                failed.append(endpoint.id)

        pool = ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs))))
        futures = {
            pool.submit(probe, base_url, key, timeout=timeout): endpoint
            for endpoint, base_url, key in jobs
        }
        done, pending = wait(futures, timeout=max(1, timeout + 2))
        discovered_by_id = {}
        for future in done:
            endpoint = futures[future]
            try:
                discovered_by_id[endpoint.id] = future.result() or []
            except Exception:
                discovered_by_id[endpoint.id] = []
        for future in pending:
            endpoint = futures[future]
            failed.append(endpoint.id)
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

        for endpoint in visible:
            if endpoint.id in failed:
                continue
            discovered = discovered_by_id.get(endpoint.id, [])
            if discovered:
                endpoint.cached_models = json.dumps(discovered)
                refreshed += 1
            else:
                failed.append(endpoint.id)
        if refreshed:
            db.commit()
    return {'refreshed_endpoints': refreshed, 'failed_endpoint_ids': failed}


def resolve(owner, endpoint_id, model):
    from core.database import ModelEndpoint, SessionLocal
    with SessionLocal() as db:
        endpoint = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id,
                                                  ModelEndpoint.is_enabled == True).first()
        if not endpoint or endpoint.owner not in (None, owner) or model not in _model_names(endpoint):
            raise ValueError('Model is unavailable or not owned by this account')
        parsed = urlsplit(endpoint.base_url)
        if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
            raise ValueError('Only configured HTTP(S) model endpoints are supported')
        path = parsed.path.rstrip('/')
        if not path.endswith('/chat/completions'):
            path += '/chat/completions' if path.endswith('/v1') else '/v1/chat/completions'
        url = urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
        headers = {'Content-Type': 'application/json'}
        if endpoint.api_key:
            headers['Authorization'] = 'Bearer ' + endpoint.api_key
        return {'endpoint_id': endpoint.id, 'model': model, 'url': url, 'headers': headers,
                'local': local_endpoint(endpoint.base_url, endpoint.endpoint_kind),
                'resource_group': resource_group(endpoint.base_url)}
