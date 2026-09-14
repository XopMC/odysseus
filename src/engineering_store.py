"""Additive engineering records in the existing TeamStore transaction boundary.

Legacy team schema/version and rows are unchanged. This extension only creates
its own tables; old TeamStore can still operate on its own tasks. Project-bound
execution must not be enabled on an older runtime without the policy gate.
"""
import json
import posixpath
import threading
import uuid

from src.team_store import Conflict, NotFound, _integer, _json, _text


SCHEMA = '''
CREATE TABLE IF NOT EXISTS engineering_versions (name TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS engineering_projects (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, root TEXT NOT NULL,
 host_id TEXT NOT NULL, access_mode TEXT CHECK(access_mode IN ('trusted_host','isolated')),
 revision INTEGER NOT NULL DEFAULT 1, event_seq INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS engineering_projects_owner ON engineering_projects(owner,created_at,id);
CREATE TABLE IF NOT EXISTS engineering_events (
 project_id TEXT NOT NULL REFERENCES engineering_projects(id), seq INTEGER NOT NULL,
 type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(project_id,seq)
);
'''


def project_root(value):
    _text(value, 'project root')
    if (not value.startswith('/') or value.startswith('//') or value == '/'
            or posixpath.normpath(value) != value or any(c in value for c in ('\0', '\n', '\r', '\\'))):
        raise ValueError('Project root must be an absolute normalized directory, not filesystem root')
    return value


class EngineeringStore:
    def __init__(self, team_store):
        self.team = team_store
        self._initialized = False
        self._lock = threading.Lock()

    def initialize(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self.team._tx() as db:
                for statement in SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
                version = db.execute("SELECT version FROM engineering_versions WHERE name='projects'").fetchone()
                if version and version[0] != 1:
                    raise Conflict('Unsupported engineering projects version')
                db.execute("INSERT OR IGNORE INTO engineering_versions VALUES ('projects',1)")
            self._initialized = True

    def _project(self, db, owner, project_id):
        _text(owner, 'owner'); _text(project_id, 'project id')
        row = db.execute('SELECT * FROM engineering_projects WHERE id=? AND owner=?', (project_id, owner)).fetchone()
        if row is None:
            raise NotFound('Project not found')
        return dict(row)

    def _event(self, db, project_id, kind, payload):
        encoded = _json(payload)
        now = self.team.clock()
        db.execute('UPDATE engineering_projects SET event_seq=event_seq+1,updated_at=? WHERE id=?', (now, project_id))
        seq = db.execute('SELECT event_seq FROM engineering_projects WHERE id=?', (project_id,)).fetchone()[0]
        db.execute('INSERT INTO engineering_events VALUES (?,?,?,?,?)', (project_id, seq, kind, encoded, now))

    def create_project(self, owner, *, name, root, host_id):
        _text(owner, 'owner'); _text(name, 'name'); _text(host_id, 'host id')
        root = project_root(root)
        if len(name) > 200 or len(host_id) > 200:
            raise ValueError('Project field is too long')
        self.initialize()
        project_id, now = uuid.uuid4().hex, self.team.clock()
        with self.team._tx() as db:
            db.execute('INSERT INTO engineering_projects(id,owner,name,root,host_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                       (project_id, owner, name, root, host_id, now, now))
            self._event(db, project_id, 'project_created', {'access_mode': None})
            return self._project(db, owner, project_id)

    def get_project(self, owner, project_id):
        self.initialize()
        with self.team._tx(write=False) as db:
            return self._project(db, owner, project_id)

    def list_projects(self, owner, *, after_id='', limit=100):
        _text(owner, 'owner'); _integer(limit, 'limit', 1, 200)
        if not isinstance(after_id, str):
            raise ValueError('Invalid cursor')
        self.initialize()
        with self.team._tx(write=False) as db:
            return [dict(row) for row in db.execute('SELECT * FROM engineering_projects WHERE owner=? AND id>? ORDER BY id LIMIT ?',
                                                   (owner, after_id, limit))]

    def set_policy(self, owner, project_id, expected_revision, access_mode, *, confirmation):
        _integer(expected_revision, 'expected_revision', 1)
        if confirmation is not True:
            raise PermissionError('Explicit project execution-mode confirmation required')
        if access_mode not in (None, 'trusted_host', 'isolated'):
            raise ValueError('Unknown access mode')
        if access_mode == 'isolated':
            raise PermissionError('Verified isolated runner is not available; trusted-host fallback is forbidden')
        self.initialize()
        with self.team._tx() as db:
            project = self._project(db, owner, project_id)
            if project['revision'] != expected_revision:
                raise Conflict('Project changed; reload before changing permissions')
            db.execute('UPDATE engineering_projects SET access_mode=?,revision=revision+1 WHERE id=?', (access_mode, project_id))
            self._event(db, project_id, 'policy_changed', {'access_mode': access_mode, 'revision': expected_revision + 1})
            return self._project(db, owner, project_id)

    def assert_access(self, owner, project_id, *, effect, revision=None, host_id=None):
        project = self.get_project(owner, project_id)
        if host_id is not None and project['host_id'] != host_id:
            raise PermissionError('Execution host is not approved for this project')
        if revision is not None and project['revision'] != revision:
            raise Conflict('Project policy changed before dispatch')
        if effect not in {'read', 'write', 'execute'}:
            raise PermissionError('This action needs a separate scoped permission')
        if effect != 'read' and project['access_mode'] != 'trusted_host':
            raise PermissionError('Choose and approve a supported execution mode first')
        return project

    def events(self, owner, project_id, *, after_seq=0, limit=100):
        _integer(after_seq, 'after_seq'); _integer(limit, 'limit', 1, 200)
        self.initialize()
        with self.team._tx(write=False) as db:
            self._project(db, owner, project_id)
            return [dict(row, payload=json.loads(row['payload'])) for row in db.execute(
                'SELECT * FROM engineering_events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?', (project_id, after_seq, limit))]
