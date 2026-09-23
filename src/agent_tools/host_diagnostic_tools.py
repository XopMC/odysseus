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


class HostFileCheckpointTool:
    """Rollback only a durable, exact-hash-verified host file checkpoint."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src import host_execution

        try:
            args = json.loads(content or '{}')
            if (not isinstance(args, dict) or set(args) != {'checkpoint_id', 'expected_sha256'}
                    or not isinstance(args.get('checkpoint_id'), str)
                    or not isinstance(args.get('expected_sha256'), dict)):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            return {'error': 'Invalid checkpoint id or exact after-hash map',
                    'code': 'invalid_arguments', 'exit_code': 1}
        owner = ctx.get('owner')
        session_id = ctx.get('session_id')
        if not owner or not session_id:
            return {'error': 'Checkpoint rollback requires an owner and chat scope',
                    'code': 'file_checkpoint_scope_required', 'exit_code': 1}
        if args['checkpoint_id'].startswith('local_'):
            from src.local_file_checkpoints import rollback_scoped
            try:
                return await rollback_scoped(owner, session_id, args['checkpoint_id'], args['expected_sha256'])
            except (OSError, ValueError, TypeError):
                return {'error': 'Local checkpoint could not be verified; files were not intentionally overwritten',
                        'code': 'rollback_refused', 'exit_code': 1}
        if not host_execution.enabled_for(owner):
            return {'error': 'No registered host is enabled for this account',
                    'code': 'not_supported_by_route', 'exit_code': 1}
        return await host_execution.execute(
            'rollback_file_checkpoint', content,
            owner=owner, session_id=session_id)
