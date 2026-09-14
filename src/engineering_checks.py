"""Approved check evidence, not an execution adapter or model-written verdict.

The caller must authenticate human profile/requirement edits and obtain current
workspace digests from the runner (a tree/content digest, never a model claim).
``verify_evidence(expected, evidence)`` is a synchronous server-owned verifier:
it must authenticate persisted runner results, not merely compare JSON fields.
No verifier means no successful or failed result can be recorded. Evidence is
bounded structured metadata only; command output and credentials are not saved.
"""
import hashlib
import json
import re
import threading
import uuid

from src.engineering_store import EngineeringStore
from src.team_store import Conflict, NotFound, _text


SCHEMA = '''
CREATE TABLE IF NOT EXISTS engineering_check_profiles (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES engineering_projects(id),
 name TEXT NOT NULL, command TEXT NOT NULL, command_hash TEXT NOT NULL,
 revision INTEGER NOT NULL, approved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS engineering_check_runs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES engineering_projects(id),
 profile_id TEXT NOT NULL REFERENCES engineering_check_profiles(id),
 profile_revision INTEGER NOT NULL, command_hash TEXT NOT NULL,
 project_revision INTEGER NOT NULL, host_id TEXT NOT NULL,
 workspace_hash TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL, evidence TEXT, started_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS engineering_check_runs_project ON engineering_check_runs(project_id,started_at,id);
CREATE TABLE IF NOT EXISTS engineering_check_requirements (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES engineering_projects(id),
 title TEXT NOT NULL, profile_ids TEXT NOT NULL, mandatory INTEGER NOT NULL,
 revision INTEGER NOT NULL
);
'''


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Expected SHA-256 workspace content digest')
    return value


class EngineeringChecks:
    def __init__(self, team_store, *, verify_evidence=None):
        self.team = team_store
        self.projects = EngineeringStore(team_store)
        self.verify_evidence = verify_evidence
        self._initialized = False
        self._lock = threading.Lock()

    def initialize(self):
        self.projects.initialize()
        with self._lock:
            if self._initialized:
                return
            with self.team._tx() as db:
                for statement in SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
                row = db.execute("SELECT version FROM engineering_versions WHERE name='checks'").fetchone()
                if row and row[0] != 1:
                    raise Conflict('Unsupported engineering checks schema')
                db.execute("INSERT OR IGNORE INTO engineering_versions VALUES ('checks',1)")
            self._initialized = True

    def _profile(self, db, project_id, profile_id):
        row = db.execute('SELECT * FROM engineering_check_profiles WHERE id=? AND project_id=?',
                         (profile_id, project_id)).fetchone()
        if row is None:
            raise NotFound('Check profile not found')
        return dict(row)

    def approve_profile(self, owner, project_id, *, name, command, confirmation,
                        profile_id=None, expected_revision=None):
        if confirmation is not True:
            raise PermissionError('Explicit check-command approval required')
        _text(name, 'name'); _text(command, 'command')
        if len(name) > 200 or len(command.encode()) > 16384 or '\0' in command:
            raise ValueError('Invalid check profile')
        digest = hashlib.sha256(command.encode()).hexdigest()
        self.initialize()
        with self.team._tx() as db:
            self.projects._project(db, owner, project_id)
            if profile_id is None:
                if expected_revision is not None:
                    raise Conflict('New profile has no prior revision')
                profile_id = uuid.uuid4().hex
                db.execute('INSERT INTO engineering_check_profiles VALUES (?,?,?,?,?,?,?)',
                           (profile_id, project_id, name, command, digest, 1, self.team.clock()))
            else:
                old = self._profile(db, project_id, profile_id)
                if type(expected_revision) is not int or old['revision'] != expected_revision:
                    raise Conflict('Check profile changed; approve current command explicitly')
                db.execute('UPDATE engineering_check_profiles SET name=?,command=?,command_hash=?,revision=revision+1,approved_at=? WHERE id=?',
                           (name, command, digest, self.team.clock(), profile_id))
            return self._profile(db, project_id, profile_id)

    def start_run(self, owner, project_id, profile_id, workspace_hash, *, kind='check'):
        _digest(workspace_hash)
        if kind not in {'check', 'baseline'}:
            raise ValueError('Unknown check kind')
        self.initialize()
        with self.team._tx() as db:
            project = self.projects._project(db, owner, project_id)
            if project['access_mode'] not in {'trusted_host', 'isolated'}:
                raise PermissionError('Project execution is not approved')
            profile = self._profile(db, project_id, profile_id)
            run_id = uuid.uuid4().hex
            db.execute('INSERT INTO engineering_check_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (run_id, project_id, profile_id, profile['revision'], profile['command_hash'],
                        project['revision'], project['host_id'], workspace_hash, kind, 'running', None,
                        self.team.clock(), None))
            return self._run(db, project_id, run_id)

    def list_profiles(self, owner, project_id, *, after_id='', limit=50):
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after_id, str):
            raise ValueError('Invalid check profile page')
        self.initialize()
        with self.team._tx(write=False) as db:
            self.projects._project(db, owner, project_id)
            if after_id:
                self._profile(db, project_id, after_id)
            rows = db.execute('SELECT * FROM engineering_check_profiles WHERE project_id=? AND id>? ORDER BY id LIMIT ?',
                              (project_id, after_id, limit + 1)).fetchall()
            records = [dict(row) for row in rows[:limit]]
            return {'profiles': records, 'next_cursor': records[-1]['id'] if len(rows) > limit else None}

    def _run(self, db, project_id, run_id):
        row = db.execute('SELECT * FROM engineering_check_runs WHERE id=? AND project_id=?',
                         (run_id, project_id)).fetchone()
        if row is None:
            raise NotFound('Check run not found')
        result = dict(row)
        result['evidence'] = json.loads(result['evidence']) if result['evidence'] else None
        return result

    def finish_run(self, owner, project_id, run_id, evidence):
        """Authenticate exact terminal result; reject replay to another run.

        Required evidence: run_id, host_id, command_hash, workspace_hash (before),
        workspace_hash_after, exit_code, runner_job_id, toolchain and protocol.
        Toolchain is an authenticated nonempty identity string, not a success flag.
        A changed workspace during a check is conservatively stale even at exit 0.
        """
        fields = {'run_id', 'host_id', 'command_hash', 'workspace_hash', 'workspace_hash_after',
                  'exit_code', 'runner_job_id', 'toolchain', 'protocol'}
        # Older authenticated observations omit terminal_status and mean exited.
        # Keep their exact payload for immutable replay; new adapters include it.
        if not isinstance(evidence, dict) or set(evidence) not in (fields, fields | {'terminal_status'}):
            raise ValueError('Incomplete or unexpected runner evidence')
        evidence = dict(evidence)
        terminal_status = evidence.get('terminal_status', 'exited')
        if terminal_status not in ('exited', 'timed_out', 'cancelled'):
            raise ValueError('Invalid runner terminal status')
        for key in fields - {'exit_code', 'protocol'}:
            if not isinstance(evidence[key], str) or not evidence[key] or len(evidence[key]) > 512:
                raise ValueError('Invalid runner evidence field')
        for key in ('workspace_hash', 'workspace_hash_after', 'command_hash'):
            _digest(evidence[key])
        if type(evidence['exit_code']) is not int or not -255 <= evidence['exit_code'] <= 255:
            raise ValueError('Invalid runner exit code')
        if type(evidence['protocol']) is not int or evidence['protocol'] != 1:
            raise ValueError('Unsupported evidence protocol')
        self.initialize()
        with self.team._tx(write=False) as db:
            self.projects._project(db, owner, project_id)
            expected = self._run(db, project_id, run_id)
        for key in ('host_id', 'command_hash', 'workspace_hash'):
            if evidence[key] != expected[key]:
                raise Conflict('Runner evidence does not match dispatched check')
        if evidence['run_id'] != run_id:
            raise Conflict('Runner evidence belongs to another run')
        if self.verify_evidence is None:
            raise PermissionError('Trusted runner evidence verifier is not configured')
        # Authenticate outside SQLite lock; recheck immutable state below.
        verification = self.verify_evidence({**expected, 'owner': owner}, dict(evidence))
        if verification is not True:
            raise PermissionError('Runner evidence authentication failed')
        with self.team._tx() as db:
            project = self.projects._project(db, owner, project_id)
            run = self._run(db, project_id, run_id)
            if run['status'] != 'running':
                if run['evidence'] == evidence:
                    return run
                raise Conflict('A terminal check result is immutable')
            profile = self._profile(db, project_id, run['profile_id'])
            stale = (profile['revision'] != run['profile_revision'] or
                     project['revision'] != run['project_revision'] or
                     project['access_mode'] not in {'trusted_host', 'isolated'} or
                     evidence['workspace_hash_after'] != run['workspace_hash'])
            status = (terminal_status if terminal_status in ('timed_out', 'cancelled') else
                      'stale' if stale else 'passed' if evidence['exit_code'] == 0 else 'failed')
            db.execute('UPDATE engineering_check_runs SET status=?,evidence=?,finished_at=? WHERE id=?',
                       (status, json.dumps(evidence, sort_keys=True), self.team.clock(), run_id))
            self.projects._event(db, project_id, 'check_finished', {
                'run_id': run_id, 'profile_id': run['profile_id'], 'status': status,
                'workspace_hash': run['workspace_hash']})
            return self._run(db, project_id, run_id)

    def set_requirement(self, owner, project_id, *, title, profile_ids, mandatory=True,
                        requirement_id=None, expected_revision=None):
        _text(title, 'requirement title')
        if (len(title) > 1000 or type(mandatory) is not bool or not isinstance(profile_ids, list)
                or not 1 <= len(profile_ids) <= 32 or any(not isinstance(x, str) for x in profile_ids)
                or len(set(profile_ids)) != len(profile_ids)):
            raise ValueError('Requirement must link distinct approved check profiles')
        self.initialize()
        with self.team._tx() as db:
            self.projects._project(db, owner, project_id)
            for profile_id in profile_ids:
                self._profile(db, project_id, profile_id)
            if requirement_id is None:
                if expected_revision is not None:
                    raise Conflict('New requirement has no prior revision')
                requirement_id, revision = uuid.uuid4().hex, 1
                db.execute('INSERT INTO engineering_check_requirements VALUES (?,?,?,?,?,?)',
                           (requirement_id, project_id, title, json.dumps(profile_ids), int(mandatory), revision))
            else:
                row = db.execute('SELECT revision FROM engineering_check_requirements WHERE id=? AND project_id=?',
                                 (requirement_id, project_id)).fetchone()
                if row is None:
                    raise NotFound('Requirement not found')
                if type(expected_revision) is not int or expected_revision != row[0]:
                    raise Conflict('Requirement changed')
                revision = row[0] + 1
                db.execute('UPDATE engineering_check_requirements SET title=?,profile_ids=?,mandatory=?,revision=? WHERE id=?',
                           (title, json.dumps(profile_ids), int(mandatory), revision, requirement_id))
            self.projects._event(db, project_id, 'requirement_approved', {
                'requirement_id': requirement_id, 'revision': revision,
                'mandatory': mandatory, 'profile_ids': profile_ids})
            return {'id': requirement_id, 'revision': revision}

    def readiness(self, owner, project_id, workspace_hash):
        """Computed evidence gate; this never writes a completed status."""
        _digest(workspace_hash)
        self.initialize()
        with self.team._tx(write=False) as db:
            project = self.projects._project(db, owner, project_id)
            requirements = []
            for row in db.execute('SELECT * FROM engineering_check_requirements WHERE project_id=? ORDER BY id', (project_id,)):
                linked = []
                for profile_id in json.loads(row['profile_ids']):
                    profile = self._profile(db, project_id, profile_id)
                    # Latest attempt for the current immutable state supersedes
                    # prior success: an in-flight retry or failure blocks completion.
                    run = db.execute('SELECT * FROM engineering_check_runs WHERE project_id=? AND profile_id=? AND profile_revision=? AND project_revision=? AND workspace_hash=? AND kind=? ORDER BY started_at DESC,rowid DESC LIMIT 1',
                                     (project_id, profile_id, profile['revision'], project['revision'], workspace_hash, 'check')).fetchone()
                    passed = bool(run and run['status'] == 'passed' and project['access_mode'] in {'trusted_host', 'isolated'})
                    linked.append({'profile_id': profile_id, 'profile_revision': profile['revision'],
                                   'run_id': run['id'] if run else None,
                                   'passed': passed, 'status': run['status'] if run else 'missing_or_stale'})
                requirements.append({'id': row['id'], 'revision': row['revision'], 'title': row['title'],
                                     'mandatory': bool(row['mandatory']), 'passed': all(x['passed'] for x in linked),
                                     'checks': linked})
            mandatory = [r for r in requirements if r['mandatory']]
            snapshot = {'ready': bool(mandatory) and all(r['passed'] for r in mandatory),
                        'workspace_hash': workspace_hash, 'project_revision': project['revision'],
                        'requirements': requirements}
            snapshot['snapshot_id'] = hashlib.sha256(json.dumps(
                snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            snapshot['observed_at'] = self.team.clock()
            return snapshot

    def list_requirements(self, owner, project_id, *, after_id='', limit=50):
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after_id, str):
            raise ValueError('Invalid requirement page')
        self.initialize()
        with self.team._tx(write=False) as db:
            self.projects._project(db, owner, project_id)
            if after_id and not db.execute('SELECT 1 FROM engineering_check_requirements WHERE id=? AND project_id=?',
                                           (after_id, project_id)).fetchone():
                raise NotFound('Requirement cursor not found')
            rows = db.execute('SELECT * FROM engineering_check_requirements WHERE project_id=? AND id>? ORDER BY id LIMIT ?',
                              (project_id, after_id, limit + 1)).fetchall()
            records = [dict(row, profile_ids=json.loads(row['profile_ids']), mandatory=bool(row['mandatory']))
                       for row in rows[:limit]]
            return {'requirements': records, 'next_cursor': records[-1]['id'] if len(rows) > limit else None}

    def assert_complete(self, owner, project_id, workspace_hash):
        result = self.readiness(owner, project_id, workspace_hash)
        if not result['ready']:
            raise Conflict('Mandatory verified checks are missing, failed, running or stale')
        return result

    def compare_baseline(self, owner, project_id, baseline_run_id, check_run_id):
        """Compare authenticated command outcomes, not individual test failures.

        Neither a green comparison nor a failure transition is a project verdict
        or proof that source changes caused the transition. Native compiler and
        dependency identities are only known to the extent recorded by the runner.
        """
        self.initialize()
        with self.team._tx(write=False) as db:
            self.projects._project(db, owner, project_id)
            before = self._run(db, project_id, baseline_run_id)
            after = self._run(db, project_id, check_run_id)
        if before['kind'] != 'baseline' or after['kind'] != 'check':
            raise ValueError('Select an initial baseline and a subsequent check')
        if before['started_at'] > after['started_at']:
            raise Conflict('Baseline must precede the compared check')
        reasons = []
        for field in ('profile_id', 'profile_revision', 'command_hash', 'host_id', 'project_revision'):
            if before[field] != after[field]:
                reasons.append(field + '_changed')
        if any(run['status'] not in {'passed', 'failed'} or not run['evidence'] for run in (before, after)):
            reasons.append('terminal_evidence_unavailable')
        elif any(before['evidence'].get(field) != after['evidence'].get(field) for field in ('toolchain', 'protocol')):
            reasons.append('reported_environment_changed')
        classification = 'not_comparable'
        if not reasons:
            classification = {
                ('passed', 'passed'): 'remained_passing',
                ('passed', 'failed'): 'became_failing',
                ('failed', 'passed'): 'became_passing',
                ('failed', 'failed'): 'failure_persists',
            }[(before['status'], after['status'])]
        def view(run):
            return {**{key: run[key] for key in ('id', 'kind', 'status', 'workspace_hash', 'host_id',
                'profile_id', 'profile_revision', 'command_hash', 'project_revision', 'started_at', 'finished_at')},
                'exit_code': (run['evidence'] or {}).get('exit_code'),
                'reported_toolchain': (run['evidence'] or {}).get('toolchain')}
        return {'classification': classification, 'comparable': not reasons, 'reasons': reasons,
                'scope': 'command_outcome_only', 'individual_failures_compared': False,
                'before': view(before), 'after': view(after)}

    def list_runs(self, owner, project_id, *, limit=100, after_id=''):
        if type(limit) is not int or not 1 <= limit <= 200 or not isinstance(after_id, str):
            raise ValueError('Invalid run limit')
        self.initialize()
        with self.team._tx(write=False) as db:
            self.projects._project(db, owner, project_id)
            where, args = 'project_id=?', [project_id]
            if after_id:
                cursor = self._run(db, project_id, after_id)
                where += ' AND (started_at,id)<(?,?)'
                args.extend([cursor['started_at'], after_id])
            args.append(limit)
            ids = db.execute(f'SELECT id FROM engineering_check_runs WHERE {where} ORDER BY started_at DESC,id DESC LIMIT ?',
                             args).fetchall()
            return [self._run(db, project_id, row[0]) for row in ids]
