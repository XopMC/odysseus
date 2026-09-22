"""Typed read-only diagnostics for the single configured trusted host route."""

from __future__ import annotations

import json


class HostDiagnosticTool:
    def __init__(self, name: str):
        self.name = name

    async def execute(self, content: str, ctx: dict) -> dict:
        from src import host_execution

        try:
            args = json.loads(content or '{}')
            if not isinstance(args, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            return {'error': 'Invalid diagnostic arguments', 'code': 'invalid_arguments', 'exit_code': 1}
        if not host_execution.enabled_for(ctx.get('owner')):
            return {'error': 'No registered host is enabled for this account',
                    'code': 'not_supported_by_route', 'exit_code': 1}
        return await host_execution.execute(self.name, content,
                                            owner=ctx.get('owner'), session_id=ctx.get('session_id'))
