"""Additive engineering records in the existing TeamStore transaction boundary.

Legacy team schema/version and rows are unchanged. This extension only creates
its own tables; old TeamStore can still operate on its own tasks. Project-bound
execution must not be enabled on an older runtime without the policy gate.
"""
import json
import os
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
CREATE TABLE IF NOT EXISTS engineering_project_memory (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES engineering_projects(id),
 owner TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('proposed','verified','stale')),
 revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS engineering_project_memory_owner_project
 ON engineering_project_memory(owner,project_id,id);
CREATE TABLE IF NOT EXISTS engineering_project_skills (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES engineering_projects(id),
 owner TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL, content TEXT NOT NULL,
 digest TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(project_id,name)
);
CREATE INDEX IF NOT EXISTS engineering_project_skills_owner_project
 ON engineering_project_skills(owner,project_id,id);
'''

MEMORY_KINDS = frozenset({
    'architecture', 'verified_command', 'known_problem', 'hypothesis',
    'rejected_hypothesis', 'constraint', 'preference',
})
MEMORY_STATES = frozenset({'proposed', 'verified', 'stale'})


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
                version = db.execute("SELECT version FROM engineering_versions WHERE name='project_memory'").fetchone()
                if version and version[0] != 1:
                    raise Conflict('Unsupported engineering project memory version')
                db.execute("INSERT OR IGNORE INTO engineering_versions VALUES ('project_memory',1)")
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
        if access_mode == 'isolated' and os.environ.get('ODYSSEUS_ISOLATED_RUNNER_ENABLED') != '1':
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
        if effect != 'read' and project['access_mode'] not in {'trusted_host', 'isolated'}:
            raise PermissionError('Choose and approve a supported execution mode first')
        return project

    def events(self, owner, project_id, *, after_seq=0, limit=100):
        _integer(after_seq, 'after_seq'); _integer(limit, 'limit', 1, 200)
        self.initialize()
        with self.team._tx(write=False) as db:
            self._project(db, owner, project_id)
            return [dict(row, payload=json.loads(row['payload'])) for row in db.execute(
                'SELECT * FROM engineering_events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?', (project_id, after_seq, limit))]

    @staticmethod
    def _memory_fields(kind, text, source, state):
        if kind not in MEMORY_KINDS or state not in MEMORY_STATES:
            raise ValueError('Unknown project memory kind or state')
        _text(text, 'project memory text'); _text(source, 'project memory source')
        if len(text.encode('utf-8')) > 16384 or len(source.encode('utf-8')) > 2048:
            raise ValueError('Project memory field is too long')
        return kind, text, source, state

    def list_memory(self, owner, project_id, *, after_id='', limit=100):
        _text(owner, 'owner'); _text(project_id, 'project id'); _integer(limit, 'limit', 1, 200)
        if not isinstance(after_id, str):
            raise ValueError('Invalid cursor')
        self.initialize()
        with self.team._tx(write=False) as db:
            self._project(db, owner, project_id)
            return [dict(row) for row in db.execute(
                'SELECT * FROM engineering_project_memory WHERE owner=? AND project_id=? AND id>? ORDER BY id LIMIT ?',
                (owner, project_id, after_id, limit))]

    def list_skills(self, owner, project_id, *, after_id='', limit=100):
        _text(owner, 'owner'); _text(project_id, 'project id'); _integer(limit, 'limit', 1, 200)
        if not isinstance(after_id, str):
            raise ValueError('Invalid cursor')
        self.initialize()
        with self.team._tx(write=False) as db:
            self._project(db, owner, project_id)
            return [dict(row) for row in db.execute(
                'SELECT id,project_id,owner,name,source,digest,enabled,revision,created_at,updated_at '
                'FROM engineering_project_skills WHERE owner=? AND project_id=? AND id>? ORDER BY id LIMIT ?',
                (owner, project_id, after_id, limit))]

    def enabled_skill_context(self, owner, project_id, *, limit=32):
        _integer(limit, 'limit', 1, 64)
        self.initialize()
        with self.team._tx(write=False) as db:
            self._project(db, owner, project_id)
            return [dict(row) for row in db.execute(
                'SELECT name,source,content,digest,revision FROM engineering_project_skills '
                'WHERE owner=? AND project_id=? AND enabled=1 ORDER BY name LIMIT ?',
                (owner, project_id, limit))]

    def save_skill(self, owner, project_id, *, name, source, content, enabled=True,
                   expected_revision=None):
        import hashlib
        _text(owner, 'owner'); _text(project_id, 'project id'); _text(name, 'skill name')
        _text(source, 'skill source'); _text(content, 'skill content')
        if len(name) > 160 or len(source) > 2048 or len(content.encode('utf-8')) > 262144:
            raise ValueError('Project skill field is too long')
        digest = hashlib.sha256(content.encode()).hexdigest()
        self.initialize()
        with self.team._tx() as db:
            self._project(db, owner, project_id)
            row = db.execute('SELECT * FROM engineering_project_skills WHERE project_id=? AND name=?',
                             (project_id, name)).fetchone()
            now = self.team.clock()
            if row:
                row = dict(row)
                if expected_revision is None or row['revision'] != expected_revision:
                    raise Conflict('Project skill changed; reload before saving')
                db.execute('UPDATE engineering_project_skills SET source=?,content=?,digest=?,enabled=?,revision=revision+1,updated_at=? WHERE id=?',
                           (source, content, digest, 1 if enabled else 0, now, row['id']))
                skill_id = row['id']
            else:
                if expected_revision not in (None, 0):
                    raise Conflict('Project skill changed; reload before saving')
                skill_id = uuid.uuid4().hex
                db.execute('INSERT INTO engineering_project_skills VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (skill_id, project_id, owner, name, source, content, digest, 1 if enabled else 0, 1, now, now))
            self._event(db, project_id, 'project_skill_saved', {'skill_id': skill_id, 'name': name, 'digest': digest})
            return dict(db.execute('SELECT id,project_id,owner,name,source,digest,enabled,revision,created_at,updated_at FROM engineering_project_skills WHERE id=?', (skill_id,)).fetchone())

    def delete_skill(self, owner, project_id, skill_id, *, expected_revision):
        _integer(expected_revision, 'expected_revision', 1)
        self.initialize()
        with self.team._tx() as db:
            self._project(db, owner, project_id)
            row = db.execute('SELECT * FROM engineering_project_skills WHERE id=? AND project_id=? AND owner=?',
                             (skill_id, project_id, owner)).fetchone()
            if row is None:
                raise NotFound('Project skill not found')
            if row['revision'] != expected_revision:
                raise Conflict('Project skill changed; reload before deleting')
            db.execute('DELETE FROM engineering_project_skills WHERE id=?', (skill_id,))
            self._event(db, project_id, 'project_skill_deleted', {'skill_id': skill_id})

    def save_memory(self, owner, project_id, *, memory_id, kind, text, source, state,
                    expected_revision, confirmation):
        if confirmation is not True:
            raise PermissionError('Explicit project-memory confirmation required')
        _text(owner, 'owner'); _text(project_id, 'project id'); _integer(expected_revision, 'expected_revision', 0)
        if not isinstance(memory_id, str):
            raise ValueError('Invalid project memory identity')
        kind, text, source, state = self._memory_fields(kind, text, source, state)
        self.initialize()
        with self.team._tx() as db:
            self._project(db, owner, project_id)
            if not memory_id:
                if expected_revision != 0:
                    raise Conflict('New project memory requires revision zero')
                memory_id, now = uuid.uuid4().hex, self.team.clock()
                db.execute('INSERT INTO engineering_project_memory VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (memory_id, project_id, owner, kind, text, source, state, 1, now, now))
                self._event(db, project_id, 'project_memory_saved', {'id': memory_id, 'revision': 1, 'state': state})
            else:
                row = db.execute('SELECT * FROM engineering_project_memory WHERE id=? AND owner=? AND project_id=?',
                    (memory_id, owner, project_id)).fetchone()
                if row is None:
                    raise NotFound('Project memory not found')
                if row['revision'] != expected_revision:
                    raise Conflict('Project memory changed; reload before saving')
                now = self.team.clock()
                db.execute('UPDATE engineering_project_memory SET kind=?,text=?,source=?,state=?,revision=revision+1,updated_at=? WHERE id=?',
                    (kind, text, source, state, now, memory_id))
                self._event(db, project_id, 'project_memory_saved', {'id': memory_id, 'revision': expected_revision + 1, 'state': state})
            row = db.execute('SELECT * FROM engineering_project_memory WHERE id=?', (memory_id,)).fetchone()
            return dict(row)

    def delete_memory(self, owner, project_id, memory_id, *, expected_revision, confirmation):
        if confirmation is not True:
            raise PermissionError('Explicit project-memory deletion confirmation required')
        _text(owner, 'owner'); _text(project_id, 'project id'); _text(memory_id, 'project memory id')
        _integer(expected_revision, 'expected_revision', 1); self.initialize()
        with self.team._tx() as db:
            self._project(db, owner, project_id)
            row = db.execute('SELECT * FROM engineering_project_memory WHERE id=? AND owner=? AND project_id=?',
                (memory_id, owner, project_id)).fetchone()
            if row is None:
                raise NotFound('Project memory not found')
            if row['revision'] != expected_revision:
                raise Conflict('Project memory changed; reload before deleting')
            db.execute('DELETE FROM engineering_project_memory WHERE id=?', (memory_id,))
            self._event(db, project_id, 'project_memory_deleted', {'id': memory_id, 'revision': expected_revision})
