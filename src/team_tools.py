"""Explicit team tool surface. Retrieved text cannot change these permissions."""
import json
import re

from src.tool_registry import (
    READ_TOOLS, WRITE_TOOLS, WEB_TOOLS, ToolAccess, ToolRegistry, canonical_name,
)

_REGISTRY = ToolRegistry()


def _builtin_schemas():
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS
    return FUNCTION_TOOL_SCHEMAS


def schemas(owner, role, web=False, *, config=None, store=None):
    """Shared catalogue filtered by current task grants, not an earlier prompt.

    The legacy ``web`` argument can narrow the current config, never enable it.
    Missing configuration deliberately advertises no host or web tools.
    """
    from src.host_execution import adapt_schemas
    if isinstance(config, dict):
        config = {**config, 'web': config.get('web') is True and web is True}
    if store is not None:
        from src.team_mcp import surface
        registry, access = surface(store, owner, role, config, _builtin_schemas())
    else:
        registry, access = ToolRegistry.from_schemas(_builtin_schemas()), ToolAccess.team(role, config)
    selected = registry.schemas(access)
    selected = adapt_schemas(selected, owner)
    for item in selected:
        if item['function']['name'] in {'bash', 'python'}:
            item['function']['description'] = (
                'Execute on the assigned Jetson host workspace using a durable tracked command. '
                'Use explicit paths. Output and command ID are saved. Never provide credentials, '
                'sudo, destructive mass deletion, push, publication or external deployment. '
                'Such actions need a separate human authorization.')
    return selected


def validate_action(name, args, role, config, *, owner=None, store=None):
    # The runtime reloads config before every call, and again before transport.
    name = canonical_name(name)
    if name.startswith('mcp__'):
        if owner is None or store is None:
            raise PermissionError('Owner-scoped reviewed MCP policy is unavailable')
        from src.team_mcp import surface, validate_arguments
        registry, access = surface(store, owner, role, config)
        registry.require(name, access)
        validate_arguments(args)
        return False  # Only reviewed public reads have a Team MCP adapter.
    name = _REGISTRY.require(name, ToolAccess.team(role, config)).id
    if not isinstance(args, dict):
        raise ValueError('Tool arguments must be an object')
    if name in {'bash', 'python'}:
        command = args.get('command' if name == 'bash' else 'code', '')
        if not isinstance(command, str) or not command.strip():
            raise ValueError('Command is empty')
        # This is an accidental-action guard, NOT a sandbox for arbitrary
        # trusted-host code. The account already has docker/root-equivalent
        # capability; hostile code needs a separately isolated host.
        if re.search(r'\b(sudo|su|doas|mkfs|shutdown|reboot)\b|\bgit\s+push\b|\b(?:docker|kubectl)\s+(?:run|exec|apply|delete)\b|\brm\s+[^\n]*(?:-r|-f)', command):
            raise PermissionError('Root, destructive or external action requires separate human authorization')
    return name not in READ_TOOLS | WEB_TOOLS


def tool_arguments(call):
    try:
        args = json.loads(call['function'].get('arguments') or '{}')
    except ValueError as exc:
        raise ValueError('Invalid tool arguments') from exc
    if not isinstance(args, dict):
        raise ValueError('Tool arguments must be an object')
    return args
