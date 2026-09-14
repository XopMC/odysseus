"""Reviewed, owner-scoped read-only MCP adapter for Team.

This is not a sandbox or a classifier of remote implementation behavior. A
human reviews an exact discovered schema and server configuration identity.
Only public/brokered reads are supported; private reads and all mutations are
unsupported even if a caller tries to approve them. MCP annotations, task
metadata and model instructions never grant authority. Revocation prevents
new dispatches; it cannot undo a remote request already sent.

The authenticated interactive-owner API is the only permitted caller of
``review``/``revoke``. These are not model tools or generic settings APIs.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading

from src.team_store import Conflict, NotFound, _integer, _json, _text
from src.tool_capabilities import ToolCapabilities, ToolEffect, ResultIntegrity, capabilities_for_tool
from src.tool_registry import ToolAccess, ToolRegistry, canonical_name, mcp_tool_id


READ_EFFECTS = frozenset({'read_public', 'brokered_network_read'})
ROLES = frozenset({'lead', 'executor', 'reviewer', 'researcher'})
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS engineering_versions (name TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS engineering_mcp_policies (
 owner TEXT NOT NULL, tool_id TEXT NOT NULL, schema_digest TEXT NOT NULL,
 effects TEXT NOT NULL, roles TEXT NOT NULL, enabled INTEGER NOT NULL,
 revision INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(owner,tool_id)
);
'''


def enabled():
    """Whether the reviewed read adapter is installed; both flags default off."""
    return (os.environ.get('ODYSSEUS_ENGINEERING_ENABLED') == '1'
            and os.environ.get('ODYSSEUS_TEAM_MCP_ENABLED') == '1')


def _id(value):
    value = canonical_name(value)
    if not value.startswith('mcp__'):
        raise ValueError('An exact namespaced MCP tool ID is required')
    return value


def _decode(row):
    result = dict(row)
    result['effects'] = json.loads(result['effects'])
    result['roles'] = json.loads(result['roles'])
    result['enabled'] = bool(result['enabled'])
    return result


class TeamMCPStore:
    """Additive policy rows in TeamStore's existing SQLite transaction boundary."""
    def __init__(self, team_store):
        self.team = team_store
        self._initialized = False
        self._lock = threading.Lock()

    def initialize(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self.team._tx() as db:
                for statement in _SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
                row = db.execute("SELECT version FROM engineering_versions WHERE name='mcp_tools'").fetchone()
                if row and row[0] != 1:
                    raise Conflict('Unsupported MCP policy version')
                db.execute("INSERT OR IGNORE INTO engineering_versions VALUES ('mcp_tools',1)")
            self._initialized = True

    def get(self, owner, tool_id):
        """Return this owner's policy, including revoked rows; foreign is NotFound."""
        _text(owner, 'owner'); tool_id = _id(tool_id)
        self.initialize()
        with self.team._tx(write=False) as db:
            row = db.execute('SELECT * FROM engineering_mcp_policies WHERE owner=? AND tool_id=?',
                             (owner, tool_id)).fetchone()
            if row is None:
                raise NotFound('MCP policy not found')
            return _decode(row)

    def list(self, owner):
        """List only exact owner's reviews (no remote catalogue or credentials)."""
        _text(owner, 'owner'); self.initialize()
        with self.team._tx(write=False) as db:
            return [_decode(row) for row in db.execute(
                'SELECT * FROM engineering_mcp_policies WHERE owner=? ORDER BY tool_id', (owner,))]

    def review(self, owner, tool_id, schema_digest, effects, roles, *, expected_revision=0, confirmation=False):
        """Human API only: CAS review of one exact digest; returns the new revision.

        API must validate the submitted digest against ``review_catalogue`` and
        authenticate an interactive owner. No writes/command execution support.
        """
        if confirmation is not True:
            raise PermissionError('Explicit human review confirmation is required')
        _text(owner, 'owner'); tool_id = _id(tool_id)
        _integer(expected_revision, 'expected revision')
        if not isinstance(schema_digest, str) or not re.fullmatch('[0-9a-f]{64}', schema_digest):
            raise ValueError('Exact current schema digest is required')
        if (not isinstance(effects, (list, tuple)) or not effects
                or not all(isinstance(item, str) and item in READ_EFFECTS for item in effects)):
            raise ValueError('Only public/brokered read effects can be reviewed for Team MCP')
        if (not isinstance(roles, (list, tuple)) or not roles
                or not all(isinstance(item, str) and item in ROLES for item in roles)):
            raise ValueError('Explicit supported worker roles are required')
        known = capabilities_for_tool(tool_id)
        if known.known and not {effect.value for effect in known.effects} <= READ_EFFECTS:
            raise PermissionError('Known private or effectful MCP tool cannot be downgraded to a public read')
        self.initialize()
        with self.team._tx() as db:
            row = db.execute('SELECT revision FROM engineering_mcp_policies WHERE owner=? AND tool_id=?',
                             (owner, tool_id)).fetchone()
            if (row[0] if row else 0) != expected_revision:
                raise Conflict('MCP policy changed; refresh before reviewing')
            now = self.team.clock()
            db.execute('''INSERT INTO engineering_mcp_policies VALUES (?,?,?,?,?,1,?,?,?)
                ON CONFLICT(owner,tool_id) DO UPDATE SET schema_digest=excluded.schema_digest,
                effects=excluded.effects,roles=excluded.roles,enabled=1,revision=excluded.revision,
                updated_at=excluded.updated_at''', (owner, tool_id, schema_digest,
                    _json(sorted(set(effects))), _json(sorted(set(roles))), expected_revision + 1, now, now))
            return _decode(db.execute('SELECT * FROM engineering_mcp_policies WHERE owner=? AND tool_id=?',
                                      (owner, tool_id)).fetchone())

    def revoke(self, owner, tool_id, expected_revision):
        """Human API only: atomically disable a review using its current revision."""
        _text(owner, 'owner'); tool_id = _id(tool_id)
        _integer(expected_revision, 'expected revision'); self.initialize()
        with self.team._tx() as db:
            row = db.execute('SELECT * FROM engineering_mcp_policies WHERE owner=? AND tool_id=?',
                             (owner, tool_id)).fetchone()
            if row is None:
                raise NotFound('MCP policy not found')
            if row['revision'] != expected_revision:
                raise Conflict('MCP policy changed; refresh before revoking')
            db.execute('UPDATE engineering_mcp_policies SET enabled=0,revision=revision+1,updated_at=? WHERE owner=? AND tool_id=?',
                       (self.team.clock(), owner, tool_id))
            return _decode(db.execute('SELECT * FROM engineering_mcp_policies WHERE owner=? AND tool_id=?',
                                      (owner, tool_id)).fetchone())


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode()) > 128 * 1024:
        raise ValueError('MCP schema/configuration exceeds review size limit')
    return hashlib.sha256(encoded.encode()).hexdigest()


def _server_configs():
    """Fresh admin configuration identity; never return credentials or endpoints."""
    from core.database import McpServer, SessionLocal
    db = SessionLocal()
    try:
        result = {}
        for row in db.query(McpServer).all():
            blocked = json.loads(row.disabled_tools) if row.disabled_tools else []
            if not isinstance(blocked, list) or not all(isinstance(item, str) for item in blocked):
                raise PermissionError('Current MCP server tool policy is invalid')
            # The opaque digest binds configuration changes, not just the display
            # name. Secret-bearing fields are hashed in memory, never returned.
            identity = {key: getattr(row, key, None) for key in
                        ('transport', 'command', 'args', 'env', 'url', 'oauth_config')}
            identity['updated_at'] = str(getattr(row, 'updated_at', None))
            result[row.id] = {'enabled': row.is_enabled is True, 'disabled_tools': blocked,
                              'config_digest': _digest(identity)}
        return result
    finally:
        db.close()


def _owner_policy(owner):
    # Retain existing Agent MCP account restrictions, in addition to new
    # per-owner reviews. A review cannot resurrect a revoked/deleted account.
    from src.tool_execution import _current_agent_privileges, _owner_is_admin
    from src.settings import get_setting
    privileges = _current_agent_privileges(owner)
    disabled = get_setting('disabled_tools', [])
    if not isinstance(disabled, list) or not all(isinstance(item, str) for item in disabled):
        raise PermissionError('Current global tool policy is invalid')
    return {'allowed': bool(isinstance(privileges, dict) and privileges.get('can_use_agent') is True
                            and _owner_is_admin(owner)), 'disabled_tools': disabled,
            'browser_allowed': isinstance(privileges, dict) and privileges.get('can_use_browser', True) is True}


def assert_review_access(owner):
    """Interactive API gate: raise PermissionError unless feature/admin access is current.

    Caller must separately authenticate the exact interactive human owner (not
    a delegated/model credential). This does not itself grant any tool access.
    """
    _text(owner, 'owner')
    if not enabled() or _owner_policy(owner).get('allowed') is not True:
        raise PermissionError('Reviewed Team MCP administration is unavailable for this owner')


def review_catalogue(manager=None):
    """Snapshot discovered tools with opaque server identity and review digest.

    This performs no MCP I/O/reconnect and trusts no annotations as permission.
    The schema/description are untrusted review material, not instructions.
    """
    if manager is None:
        from src.tool_utils import get_mcp_manager
        manager = get_mcp_manager()
    if manager is None:
        return []
    configs = _server_configs()
    result, seen = [], set()
    for tool in manager.get_all_tools():
        tool_id = mcp_tool_id(tool['server_id'], tool['name'])
        if tool_id in seen:
            raise ValueError('Duplicate canonical MCP tool identity')
        seen.add(tool_id)
        status = manager.get_server_status(tool['server_id'])
        configured = configs.get(tool['server_id'])
        active = (status.get('status') == 'connected' and not tool.get('is_disabled')
                  and (configured is not None and configured.get('enabled') is True
                       or configured is None and manager.is_builtin(tool['server_id'])))
        if configured and (tool['name'] in configured.get('disabled_tools', [])
                           or tool_id in configured.get('disabled_tools', [])):
            active = False
        server_identity = _digest({'server_id': tool['server_id'], 'config': configured,
            'transport': status.get('transport'), 'identity': status.get('identity'),
            'version': status.get('version')})
        schema = {'type': 'function', 'function': {'name': tool_id,
            'description': tool.get('description') or '', 'parameters': tool.get('input_schema') or {}}}
        if not isinstance(schema['function']['description'], str) or not isinstance(schema['function']['parameters'], dict):
            raise ValueError('Invalid discovered MCP schema')
        digest = _digest({'schema': schema, 'server_identity': server_identity})
        result.append({'tool_id': tool_id, 'server_id': tool['server_id'],
                       'server_identity': server_identity, 'schema_digest': digest,
                       'schema': copy.deepcopy(schema), 'available': bool(active)})
        if len(result) > 512:
            raise ValueError('MCP catalogue exceeds review size limit')
    return result


def surface(store, owner, role, config, builtin_schemas=(), manager=None):
    """Return (registry, access) using current owner reviews and actual inventory.

    The same factory is used for discovery, ledger classification and dispatch.
    Missing feature/config/account/review/server authority yields no MCP tools.
    """
    base = ToolRegistry.from_schemas(builtin_schemas)
    if not enabled() or not isinstance(config, dict) or config.get('mcp') is not True:
        return base, ToolAccess.team(role, config)
    account = _owner_policy(owner)
    catalogue = review_catalogue(manager)
    grants = {item['tool_id']: item for item in TeamMCPStore(store).list(owner)}
    caps, allowed, servers = {}, set(base.names()), set()
    for item in catalogue:
        grant = grants.get(item['tool_id'])
        if (not account.get('allowed') or not item['available'] or not grant or not grant['enabled']
                or role not in grant['roles'] or grant['schema_digest'] != item['schema_digest']):
            continue
        if item['server_id'] == 'builtin_browser' and account.get('browser_allowed', True) is not True:
            continue
        effects = frozenset(grant['effects'])
        if not effects or not effects <= READ_EFFECTS:
            continue
        caps[item['tool_id']] = ToolCapabilities(frozenset(ToolEffect(effect) for effect in effects),
                                                ResultIntegrity.EXTERNAL_UNTRUSTED)
        allowed.add(item['tool_id']); servers.add(item['server_id'])
    registry = ToolRegistry.from_schemas(builtin_schemas, mcp_schemas=[item['schema'] for item in catalogue],
                                        trusted_mcp_capabilities=caps, team_mcp_tools=frozenset(caps))
    access = ToolAccess(mode='team', role=role, config=config,
        adapters=frozenset({'team_host', 'team_web', 'mcp'}), allowed_tools=frozenset(allowed),
        enabled_mcp_servers=frozenset(servers), disabled_tools=frozenset(account.get('disabled_tools', ())))
    return registry, access


def validate_arguments(args):
    """Bound JSON and reject server-owned context injection before any dispatch."""
    if not isinstance(args, dict):
        raise ValueError('MCP arguments must be an object')
    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.startswith('_odysseus_'):
                    raise ValueError('Reserved MCP context argument')
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(args)
    if len(_json(args).encode()) > 64 * 1024:
        raise ValueError('MCP arguments exceed size limit')


async def dispatch(store, owner, role, config_provider, name, args, manager=None, artifact_sink=None):
    """Freshly authorized read-only manager call; no cached grant can authorize it."""
    validate_arguments(args)
    if manager is None:
        from src.tool_utils import get_mcp_manager
        manager = get_mcp_manager()
    registry, access = surface(store, owner, role, config_provider(), (), manager)
    spec = registry.require(name, access)
    if spec.server_id is None or spec.adapters.get('team') != 'mcp':
        raise PermissionError('Reviewed MCP dispatcher is unavailable')
    grant = TeamMCPStore(store).get(owner, spec.id)
    current = next((item for item in review_catalogue(manager) if item['tool_id'] == spec.id), None)
    if (not grant['enabled'] or role not in grant['roles'] or not current or not current['available']
            or grant['schema_digest'] != current['schema_digest']
            or current['schema'] != spec.schema()):
        raise PermissionError('MCP review or server changed before dispatch')
    source = {'tool_id': spec.id, 'schema_digest': grant['schema_digest'], 'policy_revision': grant['revision']}
    def uncertain():
        return {'exit_code': 1, 'stderr': 'MCP outcome is unknown. Inspect the remote result before retrying; no verified evidence was returned.',
                'outcome_unknown': True, 'retryable': False, 'untrusted_content': True, 'source': source}
    try:
        result = await asyncio.wait_for(manager.call_tool(spec.id, copy.deepcopy(args)), timeout=30)
    except asyncio.CancelledError:
        raise
    except Exception:
        return uncertain()
    if isinstance(result, dict) and result.get('outcome_unknown') is True:
        return uncertain()
    if not isinstance(result, dict) or type(result.get('exit_code')) is not int:
        return uncertain()
    screenshots = result.get('images')
    if screenshots:
        # Team's present model transport consumes text tool results only. Do
        # not silently drop the screenshot and claim verified visual evidence.
        if artifact_sink is None:
            return {'exit_code': 1, 'stderr': 'MCP image results are not supported by Team; visual evidence was not delivered.',
                    'untrusted_content': True, 'source': source}
        try:
            artifacts = artifact_sink(screenshots)
        except (ValueError, OSError):
            return {'exit_code': 1, 'stderr': 'Browser screenshot evidence was rejected; no visual evidence was delivered.',
                    'untrusted_content': True, 'source': source}
        if not isinstance(artifacts, list) or not artifacts:
            return {'exit_code': 1, 'stderr': 'Browser screenshot evidence was unavailable; no visual evidence was delivered.',
                    'untrusted_content': True, 'source': source}
    normalized = {'exit_code': 0 if result['exit_code'] == 0 else 1,
                  'untrusted_content': True, 'source': source}
    for key in ('stdout', 'stderr'):
        if key in result:
            normalized[key] = str(result[key])[:60000]
    if screenshots:
        normalized['artifacts'] = artifacts
        normalized['stdout'] = (normalized.get('stdout', '') + '\n[Browser screenshots saved as owner-scoped artifacts; inspect them in the Team evidence panel.]').strip()
    if normalized['exit_code'] and not normalized.get('stderr'):
        normalized['stderr'] = 'MCP read failed; no verified evidence returned.'
    return normalized
