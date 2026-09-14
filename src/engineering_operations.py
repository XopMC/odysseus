"""Durable local model diagnostics. Expired in-flight requests are never replayed."""
import asyncio
import json
import uuid

from src.team_store import Conflict, NotFound, _json, _text


class Operations:
    def __init__(self, team):
        self.team = team
        with team._tx() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS engineering_operations (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                request TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
                lease TEXT, expires REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS engineering_operations_owner ON engineering_operations(owner,id)')
            db.execute('CREATE INDEX IF NOT EXISTS engineering_operations_history ON engineering_operations(owner,kind,created_at,id)')

    @staticmethod
    def public(row):
        row = dict(row)
        request = json.loads(row.pop('request'))
        if row['kind'] == 'check_run':
            row['scope'] = {key: request[key] for key in ('project_id', 'profile_id', 'kind',
                'idempotency_key', 'expected_project_revision', 'expected_profile_revision')}
            row['scope']['cancellation_stops_host_command'] = False
            row['scope']['run_id'] = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
                [row['owner'], request['project_id'], request['idempotency_key']])).hex
        else:
            row['scope'] = {'endpoint_id': request['endpoint_id'], 'model': request['model'],
                            'config_digest': request['expected_config_digest']}
        row.pop('lease'); row.pop('expires'); row.pop('owner')
        row['result'] = json.loads(row['result']) if row['result'] else None
        return row

    def create(self, owner, request):
        _text(owner, 'owner')
        if (set(request) != {'endpoint_id', 'model', 'confirmation', 'expected_config_digest'}
                or request['confirmation'] is not True):
            raise ValueError('Explicit confirmation of the selected configuration required')
        for name in ('endpoint_id', 'model', 'expected_config_digest'):
            _text(request[name], name)
        operation_id, now = uuid.uuid4().hex, self.team.clock()
        with self.team._tx() as db:
            db.execute('''INSERT INTO engineering_operations
                (id,owner,kind,request,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)''',
                (operation_id, owner, 'model_probe', _json(request), 'queued', now, now))
        return self.get(owner, operation_id)

    def create_check(self, owner, request):
        """Queue an explicitly approved revision, without contacting any runner."""
        fields = {'project_id', 'profile_id', 'kind', 'idempotency_key',
                  'expected_project_revision', 'expected_profile_revision', 'confirmation'}
        if not isinstance(request, dict) or set(request) != fields or request['confirmation'] is not True:
            raise ValueError('Exact check identity, revisions and confirmation required')
        _text(owner, 'owner')
        for key in ('project_id', 'profile_id', 'idempotency_key'):
            _text(request[key], key)
            if len(request[key]) > 128:
                raise ValueError('Check identity too long')
        if request['kind'] not in ('baseline', 'check') or any(
                type(request[key]) is not int or request[key] < 1
                for key in ('expected_project_revision', 'expected_profile_revision')):
            raise ValueError('Invalid check kind or revisions')
        from src.engineering_checks import EngineeringChecks
        checks = EngineeringChecks(self.team)
        checks.initialize()
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, _json(
            ['check-operation', owner, request['project_id'], request['idempotency_key']])).hex
        with self.team._tx() as db:
            project = checks.projects._project(db, owner, request['project_id'])
            existing = db.execute('SELECT * FROM engineering_operations WHERE id=? AND owner=?',
                                  (operation_id, owner)).fetchone()
            if existing:
                if json.loads(existing['request']) != request:
                    raise Conflict('Check request identity reused with different arguments')
                return self.public(existing)
            if project['access_mode'] != 'trusted_host':
                raise PermissionError('Project execution is not approved')
            profile = checks._profile(db, project['id'], request['profile_id'])
            if (project['revision'] != request['expected_project_revision'] or
                    profile['revision'] != request['expected_profile_revision']):
                raise Conflict('Check approval changed; review the current revisions')
            now = self.team.clock()
            db.execute('''INSERT INTO engineering_operations
                (id,owner,kind,request,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)''',
                (operation_id, owner, 'check_run', _json(request), 'queued', now, now))
            checks.projects._event(db, project['id'], 'check_queued', {
                'operation_id': operation_id, 'profile_id': profile['id'], 'kind': request['kind']})
        return self.get(owner, operation_id)

    def get(self, owner, operation_id):
        with self.team._tx() as db:
            row = db.execute('SELECT * FROM engineering_operations WHERE id=? AND owner=?',
                             (operation_id, owner)).fetchone()
            if row is None:
                raise NotFound('Operation not found')
            return self.public(row)

    def list(self, owner, *, kind='model_probe', after_id='', limit=50, active_only=False, project_id=''):
        if (kind not in {'model_probe', 'check_run'} or type(limit) is not int or not 1 <= limit <= 100
                or type(active_only) is not bool or not isinstance(after_id, str)
                or not isinstance(project_id, str) or (project_id and kind != 'check_run')):
            raise ValueError('Unsupported operation kind or page size')
        _text(owner, 'owner')
        if project_id:
            from src.engineering_store import EngineeringStore
            projects = EngineeringStore(self.team)
            projects.initialize()
        # Activity recovery is independent of the most recent history page.
        # Oldest active work is what this manager claims first. Stable time/id
        # keysets avoid skipping rows when newer operations are inserted.
        direction, comparison = ('ASC', '>') if active_only else ('DESC', '<')
        with self.team._tx() as db:
            where, args = 'owner=? AND kind=?', [owner, kind]
            if project_id:
                projects._project(db, owner, project_id)
                where += " AND json_extract(request, '$.project_id')=?"
                args.append(project_id)
            if active_only:
                where += " AND status IN ('queued','running','cancel_requested')"
            if after_id:
                cursor = db.execute('SELECT created_at,request FROM engineering_operations WHERE id=? AND owner=? AND kind=?',
                                    (after_id, owner, kind)).fetchone()
                if cursor is None or (project_id and json.loads(cursor['request']).get('project_id') != project_id):
                    raise NotFound('Operation cursor not found')
                where += f' AND (created_at,id) {comparison} (?,?)'
                args.extend([cursor['created_at'], after_id])
            args.append(limit + 1)
            rows = db.execute(f'''SELECT * FROM engineering_operations
                WHERE {where} ORDER BY created_at {direction},id {direction} LIMIT ?''', args).fetchall()
        return {'operations': [self.public(row) for row in rows[:limit]],
                'next_cursor': rows[limit-1]['id'] if len(rows) > limit else None}

    def cancel(self, owner, operation_id):
        self.get(owner, operation_id)
        with self.team._tx() as db:
            db.execute('''UPDATE engineering_operations SET status=CASE
                WHEN status='queued' THEN 'cancelled' ELSE 'cancel_requested' END, updated_at=?
                WHERE id=? AND owner=? AND status IN ('queued','running')''',
                (self.team.clock(), operation_id, owner))
        return self.get(owner, operation_id)

    def claim(self):
        now, lease = self.team.clock(), uuid.uuid4().hex
        with self.team._tx() as db:
            db.execute('''UPDATE engineering_operations SET status=CASE
                WHEN status='cancel_requested' THEN 'cancelled' ELSE 'interrupted' END,
                error='Runtime lease expired; request was not repeated', updated_at=?
                WHERE status IN ('running','cancel_requested') AND expires<=?''', (now, now))
            row = db.execute("SELECT * FROM engineering_operations WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE engineering_operations SET status='running',lease=?,expires=?,updated_at=? WHERE id=?",
                       (lease, now + 15, now, row['id']))
            return dict(row), lease

    def renew(self, operation_id, lease):
        now = self.team.clock()
        with self.team._tx() as db:
            return db.execute('''UPDATE engineering_operations SET expires=?
                WHERE id=? AND lease=? AND status='running' AND expires>?''',
                (now + 15, operation_id, lease, now)).rowcount == 1

    def finish(self, operation_id, lease, status, *, result=None, error=None):
        if status not in {'completed', 'failed', 'interrupted', 'cancelled'}:
            raise ValueError('Invalid terminal status')
        now = self.team.clock()
        with self.team._tx() as db:
            db.execute('''UPDATE engineering_operations SET status=CASE
                WHEN status='cancel_requested' THEN 'cancelled' ELSE ? END,
                result=?,error=?,updated_at=? WHERE id=? AND lease=? AND expires>?
                AND status IN ('running','cancel_requested')''',
                (status, _json(result) if result is not None else None, error, now, operation_id, lease, now))


class OperationManager:
    """One diagnostic per app worker; backend limits still use the shared slot."""
    def __init__(self, team, handler=None):
        self.store = Operations(team)
        self.handler = handler
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
        return self.task

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run_check(self, owner, *, confirmation, check_active, **request):
        from src.engineering_check_runner import EngineeringCheckRunner
        if confirmation is not True or not check_active():
            raise PermissionError('Check launch no longer authorized')
        runner = EngineeringCheckRunner(self.store.team)
        result = await runner.start(owner, check_active=check_active, **request)
        # Observation after first dispatch cannot issue a new command. A lost
        # acknowledgement stays uncertain and can be inspected by stable run ID.
        while result['status'] == 'running':
            await asyncio.sleep(.5)
            if not check_active():
                raise asyncio.CancelledError()
            result = await runner.observe(owner, request['project_id'], result['run_id'])
        return result

    async def run(self):
        while True:
            claimed = self.store.claim()
            if claimed is None:
                await asyncio.sleep(.5)
                continue
            row, lease = claimed
            def active():
                return self.store.renew(row['id'], lease)
            handler = self.handler
            if handler is None:
                if row['kind'] == 'check_run':
                    handler = self.run_check
                else:
                    from src.engineering_probe import probe
                    handler = probe
            job = asyncio.create_task(handler(row['owner'],
                **json.loads(row['request']), check_active=active))
            try:
                while not job.done():
                    await asyncio.wait({job}, timeout=1)
                    if not active():
                        job.cancel()
                        await asyncio.gather(job, return_exceptions=True)
                        self.store.finish(row['id'], lease, 'cancelled')
                        break
                else:
                    if job.cancelled():
                        self.store.finish(row['id'], lease, 'cancelled')
                    else:
                        self.store.finish(row['id'], lease, 'completed', result=job.result())
            except asyncio.CancelledError:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                self.store.finish(row['id'], lease, 'interrupted', error='Runtime stopped; request was not repeated')
                raise
            except Exception:
                # Provider exceptions can contain URLs, credentials or response text.
                self.store.finish(row['id'], lease, 'failed', error='Operation failed; inspect configuration and saved run before retrying')


_manager = None


def get_manager(team):
    global _manager
    if _manager is None or _manager.store.team is not team:
        _manager = OperationManager(team)
    return _manager
