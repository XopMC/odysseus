"""Friendly owner-scoped project facade over the existing EngineeringStore."""
import posixpath

from fastapi import APIRouter, HTTPException, Request

from core.database import Session as DbSession, SessionLocal
from routes.engineering_routes import EngineeringRoute, context
from routes.team_routes import body_object
from src.engineering_store import project_root


def setup_project_routes():
    router = APIRouter(prefix='/api/projects', route_class=EngineeringRoute)

    @router.get('')
    async def projects(request: Request, after_id: str = '', limit: int = 100):
        owner, store = context(request)
        rows = store.list_projects(owner, after_id=after_id, limit=limit)
        return {'projects': rows, 'next_cursor': rows[-1]['id'] if len(rows) == limit else None}

    @router.get('/hosts')
    async def hosts(request: Request):
        owner, _ = context(request)
        from src.engineering_hosts import public_hosts
        return {'hosts': public_hosts(owner)}

    @router.post('/browse')
    async def browse(request: Request):
        owner, _ = context(request, mutation=True)
        body = await body_object(request, 4096)
        if set(body) != {'host_id', 'path'}:
            raise HTTPException(400, 'Host and absolute directory are required')
        path = project_root(body['path'])
        from src.engineering_hosts import call, public_hosts
        if body['host_id'] not in {item['id'] for item in public_hosts(owner)}:
            raise HTTPException(404, 'Execution host not found')
        response = await call(body['host_id'], 'file.call', {
            'tool': 'ls', 'cwd': path, 'content': {'path': path},
        }, owner=owner, scope='project-folder-picker')
        if not response.get('ok'):
            raise HTTPException(409, 'Directory is unavailable or unreadable')
        entries = []
        for item in response.get('result', {}).get('entries', []):
            child = item.get('path')
            if item.get('is_dir') and isinstance(child, str) and posixpath.dirname(child) == path:
                entries.append({'name': item.get('name') or posixpath.basename(child), 'path': child})
        return {'path': path, 'parent': posixpath.dirname(path) if path != '/' else None, 'directories': entries}

    @router.post('')
    async def create(request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 16384)
        if set(body) != {'name', 'root', 'host_id', 'access_mode'}:
            raise HTTPException(400, 'Name, folder, host and access mode are required')
        from src.engineering_hosts import call, public_hosts
        if body['host_id'] not in {item['id'] for item in public_hosts(owner)}:
            raise HTTPException(400, 'Execution host is not configured')
        root = project_root(body['root'])
        probe = await call(body['host_id'], 'file.call', {
            'tool': 'ls', 'cwd': root, 'content': {'path': root},
        }, owner=owner, scope='project-create-check')
        if not probe.get('ok') or probe.get('result', {}).get('exit_code') != 0:
            raise HTTPException(409, 'Project folder is unavailable or unreadable')
        body['root'] = root
        access_mode = body.pop('access_mode')
        row = store.create_project(owner, **body)
        if access_mode in {'trusted_host', 'isolated'}:
            row = store.set_policy(owner, row['id'], row['revision'], access_mode, confirmation=True)
        elif access_mode not in {None, 'read_only'}:
            raise HTTPException(400, 'Unknown project access mode')
        return row

    @router.get('/{project_id}')
    async def project(project_id: str, request: Request):
        owner, store = context(request)
        return store.get_project(owner, project_id)

    @router.get('/{project_id}/chats')
    async def chats(project_id: str, request: Request):
        owner, store = context(request)
        store.get_project(owner, project_id)
        with SessionLocal() as db:
            rows = db.query(DbSession).filter_by(owner=owner, project_id=project_id, archived=False).order_by(DbSession.updated_at.desc()).all()
            return {'chats': [{'id': row.id, 'name': row.name, 'model': row.model, 'project_id': project_id} for row in rows]}

    @router.get('/{project_id}/memory')
    async def memory(project_id: str, request: Request, after_id: str = '', limit: int = 100):
        owner, store = context(request)
        rows = store.list_memory(owner, project_id, after_id=after_id, limit=limit)
        return {'items': rows, 'next_cursor': rows[-1]['id'] if len(rows) == limit else None}

    @router.get('/{project_id}/skills')
    async def skills(project_id: str, request: Request, after_id: str = '', limit: int = 100):
        owner, store = context(request)
        rows = store.list_skills(owner, project_id, after_id=after_id, limit=limit)
        return {'skills': rows, 'next_cursor': rows[-1]['id'] if len(rows) == limit else None}

    @router.post('/{project_id}/skills')
    async def save_skill(project_id: str, request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 300000)
        if set(body) != {'name', 'source', 'content', 'enabled', 'expected_revision'}:
            raise HTTPException(400, 'Exact project skill fields and revision required')
        # Skill text is untrusted context. Saving it never changes access policy.
        return store.save_skill(owner, project_id, **body)

    @router.post('/{project_id}/skills/import')
    async def import_skills(project_id: str, request: Request):
        """Import bounded SKILL.md files from this project's own workspace."""
        owner, store = context(request, mutation=True)
        if await body_object(request, 1024) != {}:
            raise HTTPException(400, 'Empty import body required')
        project = store.get_project(owner, project_id)
        from src.engineering_hosts import call
        policy = {'cwd': project['root'], 'write_scope': []}
        listing = await call(project['host_id'], 'file.call', {
            'tool': 'ls', 'cwd': project['root'],
            'content': {'path': '.odysseus/skills'}, 'model_policy': policy,
        }, owner=owner, scope='project-skill-import-' + project_id)
        if not listing.get('ok'):
            raise HTTPException(409, 'Project skill directory is unavailable')
        existing = {row['name']: row for row in store.list_skills(owner, project_id, limit=200)}
        imported = []
        for item in listing.get('result', {}).get('entries', [])[:64]:
            name = item.get('name')
            if not isinstance(name, str) or not name or name.startswith('.'):
                continue
            relative = posixpath.join('.odysseus/skills', name, 'SKILL.md') if item.get('is_dir') else posixpath.join('.odysseus/skills', name)
            if not item.get('is_dir') and name.lower() != 'skill.md':
                continue
            result = await call(project['host_id'], 'file.call', {
                'tool': 'read_file', 'cwd': project['root'],
                'content': {'path': relative}, 'model_policy': policy,
            }, owner=owner, scope='project-skill-import-' + project_id)
            content = result.get('result', {}).get('output') if result.get('ok') else None
            if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 262144:
                continue
            skill_name = name if item.get('is_dir') else posixpath.basename(project['root']) + '-project'
            old = existing.get(skill_name)
            imported.append(store.save_skill(
                owner, project_id, name=skill_name,
                source='project:' + relative, content=content, enabled=True,
                expected_revision=old['revision'] if old else 0,
            ))
        return {'skills': imported, 'count': len(imported)}

    @router.delete('/{project_id}/skills/{skill_id}')
    async def delete_skill(project_id: str, skill_id: str, request: Request, expected_revision: int):
        owner, store = context(request, mutation=True)
        store.delete_skill(owner, project_id, skill_id, expected_revision=expected_revision)
        return {'deleted': True}

    return router
