"""Shared tool identities, effects, catalogue and pre-dispatch policy.

This module does not dispatch tools, grant authority, or replace the existing
owner, path, taint, exact-approval and lease gates. Agent adapters must supply an
effective allowlist derived from those server-owned policies. Team uses its
existing explicit host/web grants. Discovery is a snapshot, never a grant:
adapters call ``require_current`` again immediately before an action.

MCP descriptions/annotations are untrusted data. Only namespaced, discovered
schemas plus *server-owned* capability metadata can become available here.
Team MCP is registered only by the reviewed read-only policy adapter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from src.tool_capabilities import TOOL_CAPABILITIES, ToolCapabilities, ToolEffect, capabilities_for_tool
from src.tool_security import email_tool_policy_names


READ_TOOLS = frozenset({'read_file', 'ls', 'glob', 'grep', 'search_files', 'list_tree', 'file_outline', 'git_status', 'git_diff', 'git_log', 'compare_files', 'verify_hashes', 'inspect_toolchain', 'get_workspace'})
WRITE_TOOLS = frozenset({'write_file', 'edit_file', 'apply_patch', 'rollback_file_checkpoint'})
WEB_TOOLS = frozenset({'web_search', 'web_fetch'})
EXECUTE_TOOLS = frozenset({'bash', 'python'})
_TEAM_HOST_TOOLS = READ_TOOLS | WRITE_TOOLS | EXECUTE_TOOLS
_SHELL_NAMES = frozenset({'bash', 'Shell', 'shell'})
_ROLES = {'agent': frozenset({'agent', 'reviewer', 'researcher', 'planner'}),
          'team': frozenset({'lead', 'executor', 'reviewer', 'researcher', 'planner'})}
_READONLY_ROLES = frozenset({'reviewer', 'researcher', 'planner'})
_READ_EFFECTS = frozenset({ToolEffect.READ_PUBLIC, ToolEffect.READ_WORKSPACE,
                         ToolEffect.READ_PRIVATE, ToolEffect.BROKERED_NETWORK_READ})
_PART = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z')
_BUILTIN_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,127}\Z')


def tool_inventory_revision(
    schemas: Iterable[Mapping[str, Any]], *,
    selected_names: Iterable[str],
    disabled_names: Iterable[str] = (),
    relevant_names: Iterable[str] | None = None,
    policy_mode: str = 'normal',
    block_all: bool = False,
    disable_mcp: bool = False,
    plan_mode: bool = False,
    access_mode: str = '',
) -> str:
    """Hash the exact presented schemas plus their effective policy envelope.

    The digest is safe to persist in run events: it contains no schema text,
    user prompts, credentials, or endpoint data. Object-key order is ignored;
    schema/list order is normalized by function name and canonical JSON.
    """
    canonical_schemas = []
    for schema in schemas:
        if not isinstance(schema, Mapping):
            raise ValueError('Tool schemas must be mappings')
        encoded = json.dumps(
            dict(schema), sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), default=str,
        )
        function = schema.get('function')
        name = function.get('name') if isinstance(function, Mapping) else schema.get('name')
        canonical_schemas.append((str(name or ''), encoded))
    canonical_schemas.sort()

    def names(values):
        return sorted({str(value) for value in values if isinstance(value, str) and value})

    envelope = {
        'version': 1,
        'schemas': canonical_schemas,
        'selected': names(selected_names),
        'disabled': names(disabled_names),
        'relevant': None if relevant_names is None else names(relevant_names),
        'policy_mode': str(policy_mode or ''),
        'block_all': bool(block_all),
        'disable_mcp': bool(disable_mcp),
        'plan_mode': bool(plan_mode),
        'access_mode': str(access_mode or ''),
    }
    canonical = json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def mcp_tool_id(server_id: str, tool_name: str) -> str:
    """Build an unambiguous exact MCP ID; server IDs cannot contain ``__``."""
    if (not isinstance(server_id, str) or not isinstance(tool_name, str)
            or not _PART.fullmatch(server_id) or not _PART.fullmatch(tool_name)
            or '__' in server_id):
        raise ValueError('Invalid MCP server or tool name')
    return f'mcp__{server_id}__{tool_name}'


def canonical_name(name: Any) -> str:
    """Resolve only declared aliases, never strip a remote tool's namespace."""
    if not isinstance(name, str) or not name:
        raise ValueError('Tool name must be a non-empty string')
    if name.startswith('mcp__'):
        parts = name.split('__', 2)
        if len(parts) != 3:
            raise ValueError('Invalid namespaced MCP tool name')
        return mcp_tool_id(parts[1], parts[2])
    if not _BUILTIN_NAME.fullmatch(name):
        raise ValueError('Invalid tool name')
    return 'bash' if name in _SHELL_NAMES else name


def policy_names(name: str) -> frozenset[str]:
    """Equivalent spellings for every disable/approval-adjacent policy gate."""
    name = canonical_name(name)
    return _SHELL_NAMES if name == 'bash' else email_tool_policy_names(name)


def _name_set(value: Any) -> frozenset[str]:
    if not isinstance(value, (set, frozenset, list, tuple)):
        raise ValueError('Tool allow/deny lists must be collections of names')
    return frozenset(canonical_name(item) for item in value)


@dataclass(frozen=True)
class ToolAccess:
    """Server-owned policy snapshot; never construct this from model arguments.

    ``adapters`` means dispatchers actually wired by the caller, not requested
    by a user/model. Agent ``allowed_tools`` is mandatory and must already have
    owner, turn, plan-mode and settings restrictions applied. Remaining runtime
    authorization (including exact actions and lease fencing) still applies.
    """
    mode: str = ''
    role: str = ''
    config: Mapping[str, Any] | None = None
    adapters: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] | None = None
    disabled_tools: frozenset[str] = frozenset()
    enabled_mcp_servers: frozenset[str] = frozenset()
    _valid: bool = field(default=True, init=False, repr=False)

    def __post_init__(self):
        try:
            if not isinstance(self.config, Mapping):
                raise ValueError('Tool configuration unavailable')
            config = copy.deepcopy(dict(self.config))
            disabled = _name_set(self.disabled_tools) | _name_set(config.get('disabled_tools', ()))
            allowed = None if self.allowed_tools is None else _name_set(self.allowed_tools)
            if not isinstance(self.adapters, (frozenset, set, list, tuple)) or not all(
                    isinstance(item, str) for item in self.adapters):
                raise ValueError('Adapter inventory unavailable')
            if not isinstance(self.enabled_mcp_servers, (frozenset, set, list, tuple)):
                raise ValueError('MCP inventory unavailable')
            for server in self.enabled_mcp_servers:
                mcp_tool_id(server, 'validate')
            object.__setattr__(self, 'config', MappingProxyType(config))
            object.__setattr__(self, 'disabled_tools', disabled)
            object.__setattr__(self, 'allowed_tools', allowed)
            object.__setattr__(self, 'adapters', frozenset(self.adapters))
            object.__setattr__(self, 'enabled_mcp_servers', frozenset(self.enabled_mcp_servers))
        except (ValueError, TypeError):
            object.__setattr__(self, '_valid', False)

    @classmethod
    def team(cls, role: str, config: Mapping[str, Any] | None) -> ToolAccess:
        """Existing Team host/web adapters only; no implicit MCP dispatcher."""
        return cls(mode='team', role=role, config=config,
                   adapters=frozenset({'team_host', 'team_web'}))

    def narrowed(self, names: Iterable[str]) -> ToolAccess:
        """Intersect a presented catalogue with existing authority, never grant it."""
        selected = _name_set(names)
        if self.allowed_tools is not None:
            selected = frozenset(name for name in selected
                                 if not policy_names(name).isdisjoint(self.allowed_tools))
        return replace(self, allowed_tools=selected)


@dataclass(frozen=True)
class ToolSpec:
    id: str
    display_name: str
    aliases: frozenset[str]
    capabilities: ToolCapabilities
    adapters: Mapping[str, str]
    server_id: str | None = None
    _schema: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def effects(self) -> frozenset[ToolEffect]:
        return self.capabilities.effects

    @property
    def modes(self) -> frozenset[str]:
        return frozenset(self.adapters)

    def schema(self) -> dict | None:
        """A defensive schema copy; canonical wire names are never UI labels."""
        return copy.deepcopy(self._schema) if self._schema is not None else None


def _spec(name: str, schema: Mapping | None = None,
          capabilities: ToolCapabilities | None = None, *, team_mcp=False) -> ToolSpec:
    server_id = name.split('__', 2)[1] if name.startswith('mcp__') else None
    adapters = {'agent': 'mcp' if server_id else 'agent'}
    if name in _TEAM_HOST_TOOLS:
        adapters['team'] = 'team_host'
    elif name in WEB_TOOLS:
        adapters['team'] = 'team_web'
    elif server_id and team_mcp:
        adapters['team'] = 'mcp'
    return ToolSpec(name, 'Shell' if name == 'bash' else name,
                    policy_names(name) - {name}, capabilities or capabilities_for_tool(name),
                    MappingProxyType(adapters), server_id, copy.deepcopy(schema))


class ToolRegistry:
    """Immutable catalogue assembled from actual schemas or builtin metadata.

    The no-argument registry contains builtin metadata for legacy dispatch
    validation. ``from_schemas`` contains only the supplied native inventory;
    use it for discovery so absent tools are never advertised as implemented.
    """
    def __init__(self, specs: Iterable[ToolSpec] | None = None):
        specs = specs if specs is not None else (_spec(name) for name in TOOL_CAPABILITIES)
        values = {}
        for spec in specs:
            if spec.id in values:
                raise ValueError('Duplicate canonical tool identity')
            values[spec.id] = spec
        self._specs = MappingProxyType(values)

    @classmethod
    def from_schemas(cls, builtin_schemas: Iterable[Mapping], *,
                     mcp_schemas: Iterable[Mapping] = (),
                     trusted_mcp_capabilities: Mapping[str, ToolCapabilities] | None = None,
                     team_mcp_tools: Iterable[str] = (),
                     include_legacy: bool = False) -> ToolRegistry:
        """Build a catalogue without trusting MCP annotations as permissions.

        ``trusted_mcp_capabilities`` must come from reviewed server policy,
        never from discovered tool descriptions, ``readOnlyHint`` or a model.
        Unknown tools remain visible as unavailable, conservatively classified.
        ``team_mcp_tools`` is an exact reviewed read-only adapter inventory;
        this parameter never enables unknown or effectful tools for Team.
        """
        specs = []
        team_mcp_tools = _name_set(team_mcp_tools)
        readonly = frozenset({ToolEffect.READ_PUBLIC, ToolEffect.BROKERED_NETWORK_READ})
        for remote, schemas in ((False, builtin_schemas), (True, mcp_schemas)):
            for raw in schemas:
                if not isinstance(raw, Mapping) or raw.get('type') != 'function':
                    raise ValueError('Expected an OpenAI function schema')
                function = raw.get('function')
                if not isinstance(function, Mapping):
                    raise ValueError('Expected a function schema object')
                name = canonical_name(function.get('name'))
                if name.startswith('mcp__') != remote:
                    raise ValueError('MCP and builtin schema namespaces must be separate')
                schema = copy.deepcopy(dict(raw))
                schema['function']['name'] = name
                capabilities = (trusted_mcp_capabilities or {}).get(name) if remote else None
                if capabilities is not None and (not isinstance(capabilities, ToolCapabilities)
                        or not capabilities.effects or not all(isinstance(effect, ToolEffect) for effect in capabilities.effects)):
                    raise ValueError('Invalid trusted MCP capabilities')
                team_mcp = remote and name in team_mcp_tools
                if team_mcp and (capabilities is None or not capabilities.known
                                or not capabilities.effects <= readonly):
                    raise ValueError('Team MCP supports reviewed public read effects only')
                specs.append(_spec(name, schema, capabilities, team_mcp=team_mcp))
        native = cls(specs)  # Validate duplicates before merging legacy metadata.
        if not include_legacy:
            return native
        merged = {name: _spec(name) for name in TOOL_CAPABILITIES}
        merged.update(native._specs)
        return cls(merged.values())

    def names(self) -> frozenset[str]:
        """Canonical registered IDs, including legacy-only metadata if requested."""
        return frozenset(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(canonical_name(name))

    def _reason(self, spec: ToolSpec, access: ToolAccess) -> str | None:
        if not isinstance(access, ToolAccess) or not access._valid:
            return 'Tool configuration unavailable or invalid'
        if not isinstance(access.mode, str) or access.mode not in _ROLES:
            return 'Unknown execution mode'
        if not isinstance(access.role, str) or access.role not in _ROLES[access.mode]:
            return 'Unknown worker role'
        if not policy_names(spec.id).isdisjoint(access.disabled_tools):
            return 'Tool is disabled by current policy'
        adapter = spec.adapters.get(access.mode)
        if not adapter or adapter not in access.adapters:
            return 'Tool adapter is unavailable in this execution mode'
        if access.mode == 'agent' and access.allowed_tools is None:
            return 'Effective Agent tool allowlist is unavailable'
        if access.allowed_tools is not None and not any(
                name in access.allowed_tools for name in policy_names(spec.id)):
            return 'Tool is outside the effective allowlist'
        if not spec.capabilities.known:
            return 'Tool effects have not been classified by trusted policy'
        if spec.server_id is not None:
            if access.config.get('mcp') is not True or spec.server_id not in access.enabled_mcp_servers:
                return 'MCP server is disabled or unavailable'
        if access.mode == 'team':
            if spec.id in _TEAM_HOST_TOOLS and access.config.get('trusted_host') is not True:
                return 'Host access is disabled for this task'
            if spec.id in WEB_TOOLS and access.config.get('web') is not True:
                return 'Web access is disabled for this task'
        if access.role in _READONLY_ROLES and spec.id not in WEB_TOOLS and not spec.effects <= _READ_EFFECTS:
            return 'Tool has effects forbidden for a read-only role'
        return None

    def require(self, name: str, access: ToolAccess) -> ToolSpec:
        """Validate one policy snapshot; this return value is not a lease/grant."""
        try:
            spec = self.get(name)
        except ValueError as exc:
            raise PermissionError('Invalid tool identity') from exc
        if spec is None:
            raise PermissionError('Tool is not in the registered inventory')
        reason = self._reason(spec, access)
        if reason:
            raise PermissionError(reason)
        return spec

    def require_current(self, name: str, access_provider: Callable[[], ToolAccess]) -> ToolSpec:
        """Reload permissions before dispatch; no cached grants or remote I/O.

        A running operation still needs the adapter's own cancellation/lease
        fence. This check cannot roll back an action that already crossed I/O.
        """
        try:
            access = access_provider()
        except Exception as exc:
            raise PermissionError('Current tool policy is unavailable') from exc
        return self.require(name, access)

    def schemas(self, access: ToolAccess) -> list[dict]:
        """Native schemas only for tools that this exact policy can dispatch."""
        return [spec.schema() for spec in self._specs.values()
                if spec._schema is not None and self._reason(spec, access) is None]

    def public(self, access: ToolAccess, *, include_unavailable: bool = True) -> list[dict]:
        """Safe catalogue records (no config, credentials or tool descriptions).

        Fields: id, display_name, aliases, effects, known_effects, modes,
        source, server_id, adapter, native_schema, available, reason.
        An unavailable record is metadata, never a promise of working dispatch.
        """
        result = []
        for spec in self._specs.values():
            reason = self._reason(spec, access)
            if reason is not None and not include_unavailable:
                continue
            mode = access.mode if isinstance(access, ToolAccess) and isinstance(access.mode, str) else ''
            result.append({'id': spec.id, 'display_name': spec.display_name,
                'aliases': sorted(spec.aliases), 'effects': sorted(effect.value for effect in spec.effects),
                'known_effects': spec.capabilities.known, 'modes': sorted(spec.modes),
                'source': 'mcp' if spec.server_id else 'builtin', 'server_id': spec.server_id,
                'adapter': spec.adapters.get(mode), 'native_schema': spec._schema is not None,
                'available': reason is None, 'reason': reason})
        return result
