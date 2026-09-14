"""Server-only adapter for approved checks on durable authenticated runners.

Callers authenticate owner and feature access; no command, path, digest, status
or evidence is accepted from a browser/model. Commands come from approved
profiles, roots/hosts from projects, and evidence only from the pinned host RPC.
"""
import json
import uuid

from src import engineering_hosts
from src.engineering_checks import EngineeringChecks, _digest
from src.team_store import Conflict, NotFound


class EngineeringCheckRunner:
    def __init__(self, team_store, *, host_call=None):
        self.team = team_store
        self.checks = EngineeringChecks(team_store)
        self.host_call = host_call or engineering_hosts.call
        self.checks.initialize()
        with self.team._tx() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS engineering_check_dispatch (
                run_id TEXT PRIMARY KEY REFERENCES engineering_check_runs(id),
                project_id TEXT NOT NULL, request TEXT NOT NULL, job_id TEXT)''')
            if 'stop_requested' not in {row[1] for row in db.execute('PRAGMA table_info(engineering_check_dispatch)')}:
                db.execute('ALTER TABLE engineering_check_dispatch ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0')
            if 'verification_copy' not in {row[1] for row in db.execute('PRAGMA table_info(engineering_check_dispatch)')}:
                db.execute('ALTER TABLE engineering_check_dispatch ADD COLUMN verification_copy TEXT')

    async def _rpc(self, owner, project, op, args):
        result = await self.host_call(project['host_id'], op, args, owner=owner,
                                      scope='engineering-project-' + project['id'])
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise RuntimeError('Check runner unavailable; retry the same run identity')
        return result['result']

    def _load(self, owner, project_id, run_id):
        with self.team._tx(write=False) as db:
            project = self.checks.projects._project(db, owner, project_id)
            run = self.checks._run(db, project_id, run_id)
            row = db.execute('SELECT * FROM engineering_check_dispatch WHERE run_id=? AND project_id=?',
                             (run_id, project_id)).fetchone()
            if row is None:
                raise NotFound('Runner-backed check not found')
            return project, run, dict(row, request=json.loads(row['request']))

    @staticmethod
    def _active(check_active):
        if check_active is not None and check_active() is not True:
            raise PermissionError('Queued check was cancelled or is no longer active')

    def _expected(self, owner, project_id, profile_id, project_revision, profile_revision):
        for revision in (project_revision, profile_revision):
            if revision is not None and (type(revision) is not int or revision < 1):
                raise ValueError('Expected revisions must be positive integers')
        with self.team._tx(write=False) as db:
            project = self.checks.projects._project(db, owner, project_id)
            profile = self.checks._profile(db, project_id, profile_id)
        if project_revision is not None and project['revision'] != project_revision:
            raise Conflict('Queued project revision changed')
        if profile_revision is not None and profile['revision'] != profile_revision:
            raise Conflict('Queued check profile revision changed')
        return project, profile

    async def start(self, owner, project_id, profile_id, idempotency_key, *, kind='check',
                    check_active=None, expected_project_revision=None, expected_profile_revision=None):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValueError('A stable check request identity is required')
        if kind not in {'check', 'baseline'}:
            raise ValueError('Unknown check kind')
        self._active(check_active)
        self._expected(owner, project_id, profile_id, expected_project_revision, expected_profile_revision)
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([owner, project_id, idempotency_key])).hex
        try:
            _, run, _ = self._load(owner, project_id, run_id)
        except NotFound:
            run = None
        if run is not None:
            if run['profile_id'] != profile_id or run['kind'] != kind:
                raise Conflict('Check request identity reused with different arguments')
            return await self.poll(owner, project_id, run_id, check_active=check_active,
                                   expected_project_revision=expected_project_revision,
                                   expected_profile_revision=expected_profile_revision)
        project = self.checks.projects.assert_access(owner, project_id, effect='execute')
        before = await self._rpc(owner, project, 'workspace.digest', {'cwd': project['root']})
        self._active(check_active)
        self._expected(owner, project_id, profile_id, expected_project_revision, expected_profile_revision)
        workspace_hash = _digest(before.get('sha256'))
        with self.team._tx() as db:
            current = self.checks.projects._project(db, owner, project_id)
            if current['revision'] != project['revision'] or current['access_mode'] != 'trusted_host':
                raise Conflict('Project policy changed before check dispatch')
            profile = self.checks._profile(db, project_id, profile_id)
            existing = db.execute('SELECT * FROM engineering_check_runs WHERE id=?', (run_id,)).fetchone()
            if existing:
                if existing['profile_id'] != profile_id or existing['kind'] != kind:
                    raise Conflict('Check request identity reused with different arguments')
            else:
                # Atomic run + dispatch outbox. Calling start_run then saving an
                # outbox separately would leave orphan attempts after a crash.
                db.execute('INSERT INTO engineering_check_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (run_id, project_id, profile_id, profile['revision'], profile['command_hash'],
                            current['revision'], current['host_id'], workspace_hash, kind, 'running', None,
                            self.team.clock(), None))
                args = {'cwd': current['root'], 'command': profile['command'], 'timeout': 3600,
                        'idempotency_key': 'engineering-check:' + run_id,
                        'expected_workspace_hash': workspace_hash, 'check_run_id': run_id}
                # Persist preparation intent with the outbox. Legacy dispatched
                # checks retain their original cwd; only new checks require a copy.
                copy = {'source': current['root'], 'expected_source_sha256': workspace_hash,
                        'idempotency_key': 'engineering-verification:' + run_id}
                db.execute('INSERT INTO engineering_check_dispatch (run_id,project_id,request,job_id,verification_copy) VALUES (?,?,?,NULL,?)',
                           (run_id, project_id, json.dumps(args, sort_keys=True), json.dumps({'request': copy})))
        return await self.poll(owner, project_id, run_id, check_active=check_active,
                               expected_project_revision=expected_project_revision,
                               expected_profile_revision=expected_profile_revision)

    async def observe(self, owner, project_id, run_id):
        """Reconcile existing runner evidence only; never launches a command."""
        return await self.poll(owner, project_id, run_id, allow_dispatch=False)

    async def output(self, owner, project_id, run_id, *, offset=0, limit=16000):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 60000:
            raise ValueError('Invalid output cursor or size')
        result = await self.observe(owner, project_id, run_id)
        project, _, dispatch = self._load(owner, project_id, run_id)
        if not dispatch['job_id']:
            return {**result, 'output_base64': '', 'offset': offset, 'next_offset': offset,
                    'truncated': False, 'notice': 'Runner job identity is not yet confirmed'}
        job = await self._rpc(owner, project, 'terminal.poll', {
            'id': dispatch['job_id'], 'offset': offset, 'limit': limit})
        if job.get('id') != dispatch['job_id'] or (job.get('check_evidence') or {}).get('run_id') != run_id:
            raise Conflict('Check output belongs to another job')
        return {'run_id': run_id, 'job_id': dispatch['job_id'], 'status': result['status'],
                **{key: job.get(key) for key in ('output_base64', 'offset', 'next_offset', 'truncated', 'notice')}}

    async def stop(self, owner, project_id, run_id, *, confirmation):
        if confirmation is not True:
            raise PermissionError('Explicit stop confirmation required')
        result = await self.observe(owner, project_id, run_id)
        project, run, dispatch = self._load(owner, project_id, run_id)
        if run['status'] != 'running':
            return result
        if not dispatch['job_id']:
            raise Conflict('Runner job is unknown; cannot safely stop a process')
        # Persist intent before sending: an uncertain stop acknowledgement must
        # not later allow a signal-catching command to pass with exit code zero.
        with self.team._tx() as db:
            self.checks.projects._project(db, owner, project_id)
            db.execute('UPDATE engineering_check_dispatch SET stop_requested=1 WHERE run_id=?', (run_id,))
        await self._rpc(owner, project, 'terminal.stop', {'id': dispatch['job_id']})
        return {'run_id': run_id, 'job_id': dispatch['job_id'], 'status': 'stop_requested'}

    async def poll(self, owner, project_id, run_id, *, allow_dispatch=True, check_active=None,
                   expected_project_revision=None, expected_profile_revision=None):
        project, run, dispatch = self._load(owner, project_id, run_id)
        if type(allow_dispatch) is not bool:
            raise ValueError('allow_dispatch must be a server boolean')
        if allow_dispatch:
            self._active(check_active)
            self._expected(owner, project_id, run['profile_id'], expected_project_revision, expected_profile_revision)
        if run['status'] != 'running':
            return {'run_id': run_id, 'job_id': dispatch['job_id'], 'status': run['status'], 'run': run}
        job_id = dispatch['job_id']
        if job_id is None:
            # Read-only recovery also works after policy revocation. A lost SSH
            # acknowledgement is not permission to issue a fresh command.
            jobs = await self._rpc(owner, project, 'terminal.list', {})
            matches = [j for j in jobs.get('jobs', []) if j.get('check_evidence', {}).get('run_id') == run_id]
            if len(matches) > 1:
                raise Conflict('Ambiguous check runner identity')
            if matches:
                job = matches[0]
            else:
                if not allow_dispatch:
                    return {'run_id': run_id, 'job_id': None, 'status': 'dispatch_unknown'}
                self._active(check_active)
                current = self.checks.projects.assert_access(owner, project_id, effect='execute',
                                                            revision=run['project_revision'], host_id=run['host_id'])
                with self.team._tx(write=False) as db:
                    profile = self.checks._profile(db, project_id, run['profile_id'])
                if profile['revision'] != run['profile_revision']:
                    raise Conflict('Approved check command changed before dispatch')
                now = await self._rpc(owner, current, 'workspace.digest', {'cwd': current['root']})
                self._active(check_active)
                self._expected(owner, project_id, run['profile_id'], expected_project_revision, expected_profile_revision)
                if _digest(now.get('sha256')) != run['workspace_hash']:
                    with self.team._tx() as db:
                        db.execute("UPDATE engineering_check_runs SET status='stale',finished_at=? WHERE id=? AND status='running'",
                                   (self.team.clock(), run_id))
                    return {'run_id': run_id, 'job_id': None, 'status': 'stale', 'verified': False}
                if dispatch['verification_copy']:
                    copy = json.loads(dispatch['verification_copy'])
                    if 'result' not in copy:
                        try:
                            prepared = await self._rpc(owner, current, 'workspace.verification-copy', copy['request'])
                        except (RuntimeError, TimeoutError, OSError):
                            # Recover only through the same durable preparation key.
                            # Observation alone never prepares or dispatches a copy.
                            return {'run_id': run_id, 'job_id': None, 'status': 'dispatch_unknown'}
                        self._active(check_active)
                        self.checks.projects.assert_access(owner, project_id, effect='execute',
                                                          revision=run['project_revision'], host_id=run['host_id'])
                        self._expected(owner, project_id, run['profile_id'], expected_project_revision, expected_profile_revision)
                        if (prepared.get('status') != 'ready' or
                                prepared.get('source') != copy['request']['source'] or
                                prepared.get('source_sha256') != run['workspace_hash'] or
                                prepared.get('copy_sha256') != run['workspace_hash'] or
                                not isinstance(prepared.get('id'), str) or not prepared['id'] or
                                not isinstance(prepared.get('path'), str) or not prepared['path'].startswith('/') or
                                prepared['path'] == current['root']):
                            raise Conflict('Verification copy is not a confirmed matching workspace')
                        copy['result'] = prepared
                        request = {**dispatch['request'], 'cwd': prepared['path']}
                        with self.team._tx() as db:
                            previous = db.execute('SELECT verification_copy FROM engineering_check_dispatch WHERE run_id=?', (run_id,)).fetchone()[0]
                            if previous != dispatch['verification_copy']:
                                raise Conflict('Verification preparation changed concurrently; observe existing run')
                            db.execute('UPDATE engineering_check_dispatch SET request=?,verification_copy=? WHERE run_id=?',
                                       (json.dumps(request, sort_keys=True), json.dumps(copy), run_id))
                        _, _, dispatch = self._load(owner, project_id, run_id)
                # Hashing may take time; recheck policy immediately before send.
                self.checks.projects.assert_access(owner, project_id, effect='execute',
                                                  revision=run['project_revision'], host_id=run['host_id'])
                with self.team._tx(write=False) as db:
                    if self.checks._profile(db, project_id, run['profile_id'])['revision'] != run['profile_revision']:
                        raise Conflict('Approved check command changed before dispatch')
                self._active(check_active)
                try:
                    job = await self._rpc(owner, current, 'command.start', dispatch['request'])
                except (RuntimeError, TimeoutError, OSError):
                    return {'run_id': run_id, 'job_id': None, 'status': 'dispatch_unknown'}
            job_id = job['id']
            with self.team._tx() as db:
                db.execute('UPDATE engineering_check_dispatch SET job_id=? WHERE run_id=? AND job_id IS NULL', (job_id, run_id))
                if db.execute('SELECT job_id FROM engineering_check_dispatch WHERE run_id=?', (run_id,)).fetchone()[0] != job_id:
                    raise Conflict('Check runner identity changed')
        job = await self._rpc(owner, project, 'terminal.poll', {'id': job_id, 'limit': 1})
        actual = job.get('check_evidence') or {}
        if (job.get('id') != job_id or job.get('cwd') != dispatch['request']['cwd'] or
                any(actual.get(key) != value for key, value in {
                    'run_id': run_id, 'command_hash': run['command_hash'], 'workspace_hash': run['workspace_hash']}.items())):
            raise Conflict('Check runner evidence does not match dispatch')
        if job.get('status') == 'running':
            return {'run_id': run_id, 'job_id': job_id, 'status': 'running'}
        if job.get('status') not in {'exited', 'timed_out'} or actual.get('workspace_hash_after') is None:
            with self.team._tx() as db:
                self.checks.projects._project(db, owner, project_id)
                db.execute("UPDATE engineering_check_runs SET status='interrupted',finished_at=? WHERE id=? AND status='running'",
                           (self.team.clock(), run_id))
            return {'run_id': run_id, 'job_id': job_id, 'status': 'interrupted', 'verified': False}
        evidence = {key: actual[key] for key in ('run_id', 'command_hash', 'workspace_hash',
                                                'workspace_hash_after', 'toolchain', 'protocol')}
        # A stop can arrive while the poll RPC is in flight.
        _, _, latest_dispatch = self._load(owner, project_id, run_id)
        evidence.update(host_id=run['host_id'], runner_job_id=job_id, exit_code=job['exit_code'],
                        terminal_status='cancelled' if latest_dispatch['stop_requested'] else job['status'])
        # This verifier is scoped to the just-authenticated RPC observation,
        # never exposed as an endpoint accepting client-provided evidence.
        verified = EngineeringChecks(self.team, verify_evidence=lambda expected, candidate:
                                     expected['id'] == run_id and expected['owner'] == owner and candidate == evidence)
        final = verified.finish_run(owner, project_id, run_id, evidence)
        return {'run_id': run_id, 'job_id': job_id, 'status': final['status'], 'run': final}

    async def readiness(self, owner, project_id):
        project = self.checks.projects.get_project(owner, project_id)
        current = await self._rpc(owner, project, 'workspace.digest', {'cwd': project['root']})
        return self.checks.readiness(owner, project_id, _digest(current.get('sha256')))
