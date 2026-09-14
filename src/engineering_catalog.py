"""Project-facing view of the shared tool catalogue; never a permission grant."""
from src.tool_registry import ToolAccess, ToolRegistry


def project_catalog(owner, project):
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS
    registry = ToolRegistry.from_schemas(FUNCTION_TOOL_SCHEMAS)
    access = ToolAccess.team('executor', {'trusted_host': project['access_mode'] == 'trusted_host', 'web': False})
    records = registry.public(access)
    return [dict(record, name=record['display_name'], effect=', '.join(record['effects'])) for record in records]
