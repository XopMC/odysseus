"""Owner-scoped inherited context profiles in the existing Team database.

An absent policy remains absent: callers preserve legacy runtime defaults until
the owner explicitly saves a profile. No migration enables new behavior.
"""
import json
import uuid

from src.context_policy import ContextPolicy
from src.engineering_store import EngineeringStore
from src.team_store import Conflict, NotFound, _json, _text


class ContextPolicyStore:
    def __init__(self, team):
        self.team = team
        with team._tx() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS engineering_context_policies (
                owner TEXT NOT NULL, scope TEXT NOT NULL, revision INTEGER NOT NULL,
                overrides TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner,scope))''')
            db.execute('''CREATE TABLE IF NOT EXISTS engineering_context_policy_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                scope TEXT NOT NULL, revision INTEGER NOT NULL, overrides TEXT NOT NULL,
                created_at REAL NOT NULL)''')
            db.execute('''CREATE INDEX IF NOT EXISTS engineering_context_events_owner_seq
                ON engineering_context_policy_events(owner,seq)''')
            db.execute('''CREATE TABLE IF NOT EXISTS engineering_context_presets (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
                policy TEXT NOT NULL, updated_at REAL NOT NULL)''')
            db.execute('''CREATE INDEX IF NOT EXISTS engineering_context_presets_owner_seq
                ON engineering_context_presets(owner,seq)''')
            if 'kind' not in {row['name'] for row in db.execute('PRAGMA table_info(engineering_context_presets)')}:
                db.execute("ALTER TABLE engineering_context_presets ADD COLUMN kind TEXT NOT NULL DEFAULT 'full'")

    @staticmethod
    def _preset(row):
        return {key: row[key] for key in ('id', 'name', 'revision', 'updated_at')} | {
            'values': json.loads(row['policy']), 'schema_version': 1, 'kind': row['kind']}

    def save_preset(self, owner, *, name, values, preset_id='', expected_revision=0, kind='full'):
        """Save a full effective snapshot; never change an active policy or infer."""
        _text(owner, 'owner')
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError('Preset name must contain 1 to 120 characters')
        if kind not in ('full', 'overrides'):
            raise ValueError('Unknown preset kind')
        if not isinstance(values, dict) or set(values) - set(ContextPolicy.__dataclass_fields__):
            raise ValueError('Unknown context policy fields')
        if kind == 'full' and set(values) != set(ContextPolicy.__dataclass_fields__):
            raise ValueError('Preset requires a complete context policy')
        # Validate individual bounds without imposing default coupled values on
        # a partial profile. The destination's effective policy is checked on save.
        ContextPolicy.from_dict({**({'trigger_percent': 95, 'target_percent': 5} if kind == 'overrides' else {}), **values})
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('Invalid preset revision')
        if not isinstance(preset_id, str):
            raise ValueError('Invalid preset id')
        with self.team._tx() as db:
            row = db.execute('SELECT * FROM engineering_context_presets WHERE owner=? AND id=?',
                             (owner, preset_id)).fetchone() if preset_id else None
            if preset_id and row is None:
                raise NotFound('Preset not found')
            if expected_revision != (row['revision'] if row else 0):
                raise Conflict('Preset changed; reload before saving')
            preset_id = preset_id or str(uuid.uuid4())
            if row:
                db.execute('''UPDATE engineering_context_presets SET name=?,policy=?,revision=?,updated_at=?,kind=?
                    WHERE owner=? AND id=?''', (name.strip(), _json(values), expected_revision + 1,
                                              self.team.clock(), kind, owner, preset_id))
            else:
                db.execute('''INSERT INTO engineering_context_presets
                    (id,owner,name,revision,policy,updated_at,kind) VALUES (?,?,?,1,?,?,?)''',
                    (preset_id, owner, name.strip(), _json(values), self.team.clock(), kind))
            return self._preset(db.execute('SELECT * FROM engineering_context_presets WHERE owner=? AND id=?',
                                           (owner, preset_id)).fetchone())

    def list_presets(self, owner, *, after_seq=0, limit=50, query=''):
        _text(owner, 'owner')
        if not isinstance(query, str) or len(query) > 120:
            raise ValueError('Preset search must contain at most 120 characters')
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid preset cursor or page size')
        with self.team._tx(write=False) as db:
            # SQLite lower()/NOCASE only fold ASCII; profile names are multilingual.
            db.create_function('preset_casefold', 1, str.casefold, deterministic=True)
            rows = db.execute('''SELECT * FROM engineering_context_presets
                WHERE owner=? AND seq>? AND instr(preset_casefold(name),?)>0
                ORDER BY seq LIMIT ?''', (owner, after_seq, query.strip().casefold(), limit + 1)).fetchall()
        return {'items': [self._preset(row) for row in rows[:limit]],
                'next_cursor': rows[limit - 1]['seq'] if len(rows) > limit else None}

    def rename_preset(self, owner, preset_id, *, name, expected_revision):
        _text(owner, 'owner')
        _text(preset_id, 'preset_id')
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError('Preset name must contain 1 to 120 characters')
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('Invalid preset revision')
        with self.team._tx() as db:
            row = db.execute('SELECT * FROM engineering_context_presets WHERE owner=? AND id=?',
                             (owner, preset_id)).fetchone()
            if row is None:
                raise NotFound('Preset not found')
            if row['revision'] != expected_revision:
                raise Conflict('Preset changed; reload before saving')
            db.execute('''UPDATE engineering_context_presets SET name=?,revision=revision+1,updated_at=?
                WHERE owner=? AND id=?''', (name.strip(), self.team.clock(), owner, preset_id))
            return self._preset(db.execute('SELECT * FROM engineering_context_presets WHERE owner=? AND id=?',
                                           (owner, preset_id)).fetchone())

    def delete_preset(self, owner, preset_id, *, expected_revision):
        _text(owner, 'owner')
        _text(preset_id, 'preset_id')
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('Invalid preset revision')
        with self.team._tx() as db:
            row = db.execute('SELECT revision FROM engineering_context_presets WHERE owner=? AND id=?',
                             (owner, preset_id)).fetchone()
            if row is None:
                raise NotFound('Preset not found')
            if row['revision'] != expected_revision:
                raise Conflict('Preset changed; reload before deleting')
            db.execute('DELETE FROM engineering_context_presets WHERE owner=? AND id=?', (owner, preset_id))

    def chain(self, owner, *, project_id='', task_id='', worker_id='', session_id=''):
        _text(owner, 'owner')
        if session_id:
            _text(session_id, 'session_id')
            if project_id or task_id or worker_id:
                raise ValueError('Chat policy cannot be combined with Team scope')
            from core.database import get_db_session, Session
            with get_db_session() as db:
                exists = db.query(Session.id).filter(Session.id == session_id, Session.owner == owner).first()
            if not exists:
                raise NotFound('Chat not found')
            return ['owner', 'session:' + session_id]
        if worker_id and not task_id:
            raise ValueError('Worker policy requires its task')
        scopes = ['owner']
        if task_id:
            task = self.team.get_task(owner, task_id)
            actual_project = task['metadata'].get('engineering_project_id', '')
            if project_id and project_id != actual_project:
                raise ValueError('Project does not match the task')
            project_id = actual_project
        if project_id:
            EngineeringStore(self.team).get_project(owner, project_id)
            scopes.append('project:' + project_id)
        if task_id:
            scopes.append('task:' + task_id)
        if worker_id:
            self.team.get_worker(owner, task_id, worker_id)
            scopes.append('worker:' + _json([task_id, worker_id]))
        return scopes

    def _resolve(self, db, owner, scopes):
        values = ContextPolicy().to_dict()
        sources = {name: 'default' for name in values}
        revisions, configured = {}, False
        layers = []
        for scope in scopes:
            row = db.execute('SELECT * FROM engineering_context_policies WHERE owner=? AND scope=?',
                             (owner, scope)).fetchone()
            overrides = json.loads(row['overrides']) if row else {}
            revisions[scope] = row['revision'] if row else 0
            if overrides:
                configured = True
                values.update(overrides)
                sources.update({name: scope for name in overrides})
            layers.append({'scope': scope, 'revision': revisions[scope], 'overrides': overrides})
        # A changed parent may invalidate a child's coupled fields. Fail closed,
        # never silently drop overrides or manufacture a different valid policy.
        validation_error = None
        try:
            ContextPolicy.from_dict(values)
        except ValueError as exc:
            validation_error = str(exc)
        return {'configured': configured, 'effective': values, 'sources': sources,
                'revisions': revisions, 'layers': layers, 'valid': validation_error is None,
                'validation_error': validation_error}

    def get(self, owner, **scope):
        chain = self.chain(owner, **scope)
        with self.team._tx() as db:
            return self._resolve(db, owner, chain)

    def last_completed_request(self, owner, *, project_id='', task_id='', worker_id='', session_id=''):
        # Resolve ownership and project/worker binding before querying the journal.
        self.chain(owner, project_id=project_id, task_id=task_id, worker_id=worker_id, session_id=session_id)
        if not worker_id:
            return None
        with self.team._tx(write=False) as db:
            row = db.execute("""SELECT seq, created_at, payload FROM team_events
                WHERE task_id=? AND type='worker_metrics'
                AND json_extract(payload, '$.worker_id')=?
                ORDER BY seq DESC LIMIT 1""", (task_id, worker_id)).fetchone()
        if not row:
            return None
        payload = json.loads(row['payload'])
        return {'seq': row['seq'], 'created_at': row['created_at'],
                'context_policy': payload.get('context_policy')}

    def save(self, owner, *, overrides, expected_revisions, **scope):
        chain = self.chain(owner, **scope)
        if not isinstance(overrides, dict) or set(overrides) - set(ContextPolicy.__dataclass_fields__):
            raise ValueError('Unknown context policy fields')
        if (not isinstance(expected_revisions, dict) or set(expected_revisions) != set(chain)
                or any(type(v) is not int or v < 0 for v in expected_revisions.values())):
            raise ValueError('Exact inherited revision vector is required')
        with self.team._tx() as db:
            # Read revisions independently of effective-policy validation, so an
            # invalid child can still be corrected after a parent update.
            revisions = {}
            effective = ContextPolicy().to_dict()
            for key in chain:
                row = db.execute('SELECT * FROM engineering_context_policies WHERE owner=? AND scope=?',
                                 (owner, key)).fetchone()
                revisions[key] = row['revision'] if row else 0
                effective.update(overrides if key == chain[-1] else json.loads(row['overrides']) if row else {})
            if revisions != expected_revisions:
                raise Conflict('Context policy changed; reload before saving')
            ContextPolicy.from_dict(effective)
            revision, now = revisions[chain[-1]] + 1, self.team.clock()
            db.execute('''INSERT INTO engineering_context_policies VALUES (?,?,?,?,?)
                ON CONFLICT(owner,scope) DO UPDATE SET revision=excluded.revision,
                overrides=excluded.overrides,updated_at=excluded.updated_at''',
                (owner, chain[-1], revision, _json(overrides), now))
            db.execute('''INSERT INTO engineering_context_policy_events
                (owner,scope,revision,overrides,created_at) VALUES (?,?,?,?,?)''',
                (owner, chain[-1], revision, _json(overrides), now))
            return self._resolve(db, owner, chain)

    def events(self, owner, *, after_seq=0, limit=100):
        _text(owner, 'owner')
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid event cursor or page size')
        with self.team._tx() as db:
            rows = db.execute('''SELECT seq,scope,revision,overrides,created_at
                FROM engineering_context_policy_events WHERE owner=? AND seq>? ORDER BY seq LIMIT ?''',
                (owner, after_seq, limit)).fetchall()
        return [{**dict(row), 'overrides': json.loads(row['overrides'])} for row in rows]
