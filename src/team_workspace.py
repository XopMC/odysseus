"""Workspace orchestration helpers; return patches, never mutate runtime/store.

host_call is async (op, args, owner, scope) -> runner response. The runtime must
serialize direct/non-Git writers when exclusive_required is returned. Git leases
are coordination, not a security sandbox. Callers supply authenticated IDs.
"""
import hashlib
import json


class WorkspaceError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _key(kind, owner, team_id, worker_id=''):
    encoded = json.dumps([kind, owner, team_id, worker_id], separators=(',', ':')).encode()
    return 'team-workspace-v1-' + hashlib.sha256(encoded).hexdigest()


async def _call(host_call, op, args, owner, scope):
    response = await host_call(op, args, owner, scope)
    if not isinstance(response, dict) or response.get('ok') is not True:
        message = response.get('error', 'invalid host response') if isinstance(response, dict) else 'invalid host response'
        raise WorkspaceError(str(message), response.get('code') if isinstance(response, dict) else None)
    result = response.get('result')
    if not isinstance(result, dict):
        raise WorkspaceError('host result must be an object')
    return result


def _record(result):
    if not all(isinstance(result.get(key), str) and result[key] for key in ('id', 'path', 'source')):
        raise WorkspaceError('host returned an incomplete worktree record')
    return result


async def ensure_team(host_call, owner, team_id, metadata):
    """Create/recover the team's isolated integration checkout from dirty source.

    A known non-Git directory uses direct mode. Permission, SSH, lock, unsupported
    submodule and all other failures remain errors, never a direct-write fallback.
    """
    source = metadata.get('project_path')
    if not isinstance(source, str) or not source.startswith('/'):
        raise WorkspaceError('project_path must be an absolute host path')
    try:
        record = _record(await _call(host_call, 'git.worktree.create',
                         {'source': source, 'idempotency_key': _key('integration', owner, team_id)}, owner, team_id))
    except WorkspaceError as exc:
        if exc.code != 'not_git_repository':
            raise
        return {'workspace': {'mode': 'direct', 'source_path': source,
                              'exclusive_required': True, 'checkpoint_supported': False,
                              'file_checkpoints_supported': True},
                'integration_path': source}
    return {'workspace': {'mode': 'git', 'source_path': source, 'integration': record,
                          'exclusive_required': False, 'checkpoint_supported': True},
            'integration_path': record['path']}


async def ensure_worker(host_call, owner, team_id, worker_id, metadata, profile):
    """Create/recover one child checkout. The parent scope is verified team ID."""
    workspace = metadata.get('workspace') or {}
    if workspace.get('mode') == 'direct':
        return {'cwd': workspace['source_path'],
                'workspace': {'mode': 'direct', 'exclusive_required': True,
                              'checkpoint_supported': False, 'file_checkpoints_supported': True}}
    if workspace.get('mode') != 'git':
        raise WorkspaceError('team workspace has not been initialized')
    parent = _record(workspace.get('integration') or {})
    record = _record(await _call(host_call, 'git.worktree.create',
                     {'source': parent['path'], 'parent_scope': team_id,
                      'idempotency_key': _key('worker', owner, team_id, worker_id)}, owner, worker_id))
    return {'cwd': record['path'], 'workspace': {'mode': 'git', 'record': record,
                                               'checkpoint_supported': True}}


async def review_diff(host_call, owner, worker_id, profile):
    """Capture the exact worker tree for independent review; never auto-approve."""
    workspace = profile.get('workspace') or {}
    if workspace.get('mode') == 'direct':
        return {'mode': 'direct', 'approved': False, 'patch': None,
                'requires_manual_review': True, 'checkpoint_supported': False,
                'file_checkpoints_supported': True}
    if workspace.get('mode') != 'git':
        raise WorkspaceError('worker workspace has not been initialized')
    record = _record(workspace.get('record') or {})
    diff = await _call(host_call, 'git.diff', {'id': record['id']}, owner, worker_id)
    if diff.get('truncated') or not isinstance(diff.get('patch'), str):
        raise WorkspaceError('full untruncated diff required for review')
    if not all(isinstance(diff.get(key), str) and diff[key] for key in ('source_tree', 'worktree_tree')):
        raise WorkspaceError('review is missing Git version guards')
    return {**diff, 'mode': 'git', 'worktree_id': record['id'], 'approved': False,
            'patch_sha256': hashlib.sha256(diff['patch'].encode()).hexdigest()}


async def integrate_reviewed(host_call, owner, worker_id, profile, review):
    """Integrate only the reviewed worker version, atop current parent changes.

    A sibling worker may have merged since review; refresh the source version for
    the runner's conflict-checked three-way merge. Never refresh the worker guard.
    """
    if review.get('approved') is not True:
        raise WorkspaceError('explicit successful review required before integration')
    workspace = profile.get('workspace') or {}
    if workspace.get('mode') == 'direct':
        if review.get('mode') != 'direct':
            raise WorkspaceError('review/workspace mode mismatch')
        return {'mode': 'direct', 'status': 'direct_workspace_no_merge', 'checkpoint_id': None,
                'checkpoint_supported': False}
    record = _record(workspace.get('record') or {})
    if review.get('mode') != 'git' or review.get('worktree_id') != record['id']:
        raise WorkspaceError('review belongs to a different worktree')
    latest = await _call(host_call, 'git.diff', {'id': record['id']}, owner, worker_id)
    if latest.get('truncated') or latest.get('worktree_tree') != review.get('worktree_tree'):
        raise WorkspaceError('worker changed after review; review again before integration')
    if not isinstance(latest.get('source_tree'), str) or not latest['source_tree']:
        raise WorkspaceError('current source version guard missing')
    if hashlib.sha256(latest.get('patch', '').encode()).hexdigest() != review.get('patch_sha256'):
        raise WorkspaceError('worker patch changed after review')
    args = {'id': record['id'], 'expected_source_tree': latest['source_tree'],
            'expected_worktree_tree': review['worktree_tree']}
    key_guard = review['worktree_tree']
    if review.get('selected_paths') is not None:
        selected = review['selected_paths']
        if not isinstance(selected, list) or not all(isinstance(path, str) for path in selected):
            raise WorkspaceError('selected_paths must be a list of reviewed files')
        selected = sorted(set(selected))
        if not isinstance(latest.get('files'), list) or any(path not in latest['files'] for path in selected):
            raise WorkspaceError('selected path is not a changed file in the reviewed worktree')
        args['paths'] = selected
        key_guard = json.dumps([review['worktree_tree'], selected], separators=(',', ':'))
    args['idempotency_key'] = _key('integrate', owner, record['id'], key_guard)
    return await _call(host_call, 'git.integrate', args, owner, worker_id)
