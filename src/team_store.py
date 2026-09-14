"""Separate, lazy SQLite storage for durable team execution.

Every public data operation takes an exact owner; missing and foreign records
both raise NotFound. ``configure_resource_group`` and ``list_owners_internal``
are trusted scheduler/configuration interfaces, NEVER HTTP/agent tools.

Lease tokens fence writes, not operating-system processes. The executor must
stop dispatching on lease loss. Record an effectful intent BEFORE dispatch;
uncertain intents block recovery until a human verifies their outcome. Store
only redacted task context: credential-shaped JSON keys are rejected, but the
caller must also remove secrets embedded in arbitrary prose or command output.

Money is integer micro-USD; rates are micro-USD per million tokens. Reserve an
upper input bound and the enforced max output BEFORE HTTP; mark_sent BEFORE
sending. Never release a sent reservation. Unknown usage consumes its ceiling.
The provider call must enforce these token bounds; over-ceiling settlements
fail closed and retain the reservation rather than authorizing more spending.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class LeaseLost(Conflict):
    pass


class BudgetError(Conflict):
    pass


_MAX_INT = 2**63 - 1
_JSON_FIELDS = {"metadata", "profile", "result", "payload", "data"}
_SECRET_KEYS = {"password", "passwd", "api_key", "apikey", "access_token",
                "refresh_token", "authorization", "cookie", "cookies", "private_key",
                "client_secret", "credentials", "headers", "api_token", "token", "secret"}
_UNSET = object()
_TASK_STATES = {"pending", "running", "done", "accepted", "failed", "waiting_approval",
                "blocked", "cancelled", "paused"}
_WORKER_IDLE = {"pending", "failed", "waiting_approval", "blocked", "paused"}


def _integer(value, name, minimum=0, maximum=_MAX_INT):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\0" in value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _json(value):
    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON keys must be strings")
                if key.lower().replace("-", "_") in _SECRET_KEYS:
                    raise ValueError("Credentials must not be persisted in team context")
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
    check(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 8 * 1024 * 1024:
        raise ValueError("Team JSON payload exceeds 8 MiB")
    return encoded


def _row(row, *, lease=False):
    if row is None:
        return None
    result = dict(row)
    for key in _JSON_FIELDS & result.keys():
        if result[key] is not None:
            result[key] = json.loads(result[key])
    if not lease:
        result.pop("lease_token", None)
    if 'max_workers' in result:
        result['max_workers'] = result['metadata'].get('concurrency_limit')
    return result


_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_tasks (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
 metadata TEXT NOT NULL, max_workers INTEGER NOT NULL CHECK(max_workers BETWEEN 1 AND 4),
 budget_microusd INTEGER NOT NULL CHECK(budget_microusd >= 0),
 spent_microusd INTEGER NOT NULL DEFAULT 0 CHECK(spent_microusd >= 0),
 reserved_microusd INTEGER NOT NULL DEFAULT 0 CHECK(reserved_microusd >= 0),
 event_seq INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS team_tasks_owner ON team_tasks(owner, created_at);
CREATE TABLE IF NOT EXISTS team_coordinator_leases (
 task_id TEXT PRIMARY KEY REFERENCES team_tasks(id), lease_token TEXT NOT NULL, lease_expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_resource_groups (name TEXT PRIMARY KEY, capacity INTEGER NOT NULL CHECK(capacity > 0));
CREATE TABLE IF NOT EXISTS team_workers (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id), name TEXT NOT NULL,
 status TEXT NOT NULL, profile TEXT NOT NULL, resource_group TEXT REFERENCES team_resource_groups(name),
 result TEXT, attempt_id TEXT, lease_token TEXT, lease_expires REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS team_workers_task ON team_workers(task_id, status);
CREATE TABLE IF NOT EXISTS team_worker_stop_requests (
 worker_id TEXT PRIMARY KEY REFERENCES team_workers(id), status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_worker_resources (
 worker_id TEXT NOT NULL REFERENCES team_workers(id), resource_group TEXT NOT NULL REFERENCES team_resource_groups(name),
 PRIMARY KEY(worker_id, resource_group)
);
CREATE TABLE IF NOT EXISTS team_dependencies (
 worker_id TEXT NOT NULL REFERENCES team_workers(id), dependency_id TEXT NOT NULL REFERENCES team_workers(id),
 PRIMARY KEY(worker_id, dependency_id)
);
CREATE TABLE IF NOT EXISTS team_attempts (
 id TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES team_workers(id), attempt_no INTEGER NOT NULL,
 status TEXT NOT NULL, lease_token TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
 UNIQUE(worker_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS team_events (
 task_id TEXT NOT NULL REFERENCES team_tasks(id), seq INTEGER NOT NULL, type TEXT NOT NULL,
 payload TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(task_id, seq)
);
CREATE TABLE IF NOT EXISTS team_checkpoints (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id), worker_id TEXT NOT NULL REFERENCES team_workers(id),
 attempt_id TEXT NOT NULL REFERENCES team_attempts(id), seq INTEGER NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(worker_id, seq)
);
CREATE TABLE IF NOT EXISTS team_tool_intents (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id), worker_id TEXT NOT NULL REFERENCES team_workers(id),
 attempt_id TEXT NOT NULL REFERENCES team_attempts(id), name TEXT NOT NULL, payload TEXT NOT NULL,
 effectful INTEGER NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(worker_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS team_artifacts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id), worker_id TEXT REFERENCES team_workers(id),
 name TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_profiles (
 owner TEXT NOT NULL, name TEXT NOT NULL, profile TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(owner, name)
);
CREATE TABLE IF NOT EXISTS team_approvals (
 task_id TEXT NOT NULL REFERENCES team_tasks(id), endpoint_id TEXT NOT NULL,
 limit_microusd INTEGER NOT NULL, input_rate_per_million INTEGER NOT NULL, output_rate_per_million INTEGER NOT NULL,
 spent_microusd INTEGER NOT NULL DEFAULT 0, reserved_microusd INTEGER NOT NULL DEFAULT 0,
 revoked INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(task_id, endpoint_id)
);
CREATE TABLE IF NOT EXISTS team_reservations (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id), worker_id TEXT NOT NULL REFERENCES team_workers(id),
 attempt_id TEXT NOT NULL REFERENCES team_attempts(id), endpoint_id TEXT NOT NULL, approval_revision INTEGER NOT NULL,
 input_tokens INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, input_rate_per_million INTEGER NOT NULL,
 output_rate_per_million INTEGER NOT NULL, reserved_microusd INTEGER NOT NULL, charged_microusd INTEGER,
 actual_input_tokens INTEGER, actual_output_tokens INTEGER, status TEXT NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY(task_id, endpoint_id) REFERENCES team_approvals(task_id, endpoint_id)
);
"""


class TeamStore:
    def __init__(self, path, *, resource_capacities=None, clock=time.time):
        """Create a lazy handle to a separate file DB; no I/O until first use."""
        if str(path) == ":memory:":
            raise ValueError("TeamStore requires a durable file path")
        self.path = Path(path)
        self.clock = clock
        self._resource_capacities = dict(resource_capacities or {})
        for group, capacity in self._resource_capacities.items():
            _text(group, "resource group")
            _integer(capacity, "capacity", 1, 1024)
        self._initialized = False
        self._init_lock = threading.Lock()

    def _connect(self):
        self._initialize()
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            try:
                connection.execute("PRAGMA busy_timeout=30000")
                # journal_mode's initial lock upgrade can return SQLITE_BUSY
                # immediately despite busy_timeout when two fresh handles race.
                # Retry only that explicit transient code, under a real deadline.
                deadline = time.monotonic() + 30
                while True:
                    try:
                        connection.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        code = getattr(exc, "sqlite_errorcode", 0) & 255
                        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                            raise
                        time.sleep(.01)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise Conflict(f"Unsupported team schema version {version}")
                # No executescript: it implicitly commits an existing transaction.
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                for name, capacity in self._resource_capacities.items():
                    existing = connection.execute("SELECT capacity FROM team_resource_groups WHERE name=?", (name,)).fetchone()
                    if existing and existing[0] != capacity:
                        raise Conflict("Resource capacity changed; configure it explicitly")
                    connection.execute("INSERT OR IGNORE INTO team_resource_groups VALUES (?,?)", (name, capacity))
                connection.execute("PRAGMA user_version=1")
                connection.commit()
                self._initialized = True
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def _tx(self, *, write=True):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _task(self, db, owner, task_id):
        _text(owner, "owner")
        row = db.execute("SELECT * FROM team_tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone()
        if row is None:
            raise NotFound("Task not found")
        return row

    def _worker(self, db, owner, task_id, worker_id):
        self._task(db, owner, task_id)
        row = db.execute("SELECT * FROM team_workers WHERE id=? AND task_id=?", (worker_id, task_id)).fetchone()
        if row is None:
            raise NotFound("Worker not found")
        return row

    def _lease(self, db, owner, task_id, worker_id, token):
        worker = self._worker(db, owner, task_id, worker_id)
        if (worker["status"] != "running" or not token or worker["lease_token"] != token
                or worker["lease_expires"] <= self.clock()):
            raise LeaseLost("Lease expired or superseded")
        return worker

    def _coordinator(self, db, owner, task_id, token):
        self._task(db, owner, task_id)
        row = db.execute("SELECT * FROM team_coordinator_leases WHERE task_id=?", (task_id,)).fetchone()
        if not row or not token or row["lease_token"] != token or row["lease_expires"] <= self.clock():
            raise LeaseLost("Coordinator lease expired or superseded")
        return row

    def claim_coordinator(self, owner, task_id, *, lease_seconds=60, manual_review=False):
        """Atomically acquire one coordinator per task, else None. Token is private.

        Pass coordinator_token to every subsequent coordinator mutation; renew
        while awaiting I/O and stop dispatching immediately if renewal fails.
        This fences database writes, not external host operations.
        Only the explicit manual-acceptance path sets manual_review=True; it
        may review done workers in waiting_approval/blocked tasks. The scheduler
        keeps the default pending/running gate. No paused or terminal override.
        """
        _integer(lease_seconds, "lease_seconds", 1, 3600)
        if type(manual_review) is not bool:
            raise ValueError("manual_review must be an explicit boolean")
        with self._tx() as db:
            task = self._task(db, owner, task_id)
            allowed = {"pending", "running"}
            if manual_review:
                allowed.update({"waiting_approval", "blocked"})
            if task["status"] not in allowed:
                return None
            existing = db.execute("SELECT * FROM team_coordinator_leases WHERE task_id=?", (task_id,)).fetchone()
            if existing and existing["lease_expires"] > self.clock():
                return None
            token, expires = uuid.uuid4().hex, self.clock() + lease_seconds
            db.execute("INSERT INTO team_coordinator_leases VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET lease_token=excluded.lease_token,lease_expires=excluded.lease_expires", (task_id, token, expires))
            return {"task_id": task_id, "lease_token": token, "lease_expires": expires}

    def renew_coordinator(self, owner, task_id, lease_token, *, lease_seconds=60):
        """Extend only a still-live coordinator lease; stale holders cannot renew."""
        _integer(lease_seconds, "lease_seconds", 1, 3600)
        with self._tx() as db:
            self._coordinator(db, owner, task_id, lease_token)
            expires = self.clock() + lease_seconds
            db.execute("UPDATE team_coordinator_leases SET lease_expires=? WHERE task_id=?", (expires, task_id))
            return {"task_id": task_id, "lease_expires": expires}

    def release_coordinator(self, owner, task_id, lease_token):
        """Release a live owned coordinator lease, never a replacement's lock."""
        with self._tx() as db:
            self._coordinator(db, owner, task_id, lease_token)
            db.execute("DELETE FROM team_coordinator_leases WHERE task_id=?", (task_id,))

    def assert_coordinator(self, owner, task_id, lease_token):
        """Check before external dispatch; database mutations also need the token."""
        with self._tx(write=False) as db:
            self._coordinator(db, owner, task_id, lease_token)

    def _event(self, db, task_id, event_type, payload):
        _text(event_type, "event type")
        encoded = _json(payload)
        now = self.clock()
        db.execute("UPDATE team_tasks SET event_seq=event_seq+1,updated_at=? WHERE id=?", (now, task_id))
        seq = db.execute("SELECT event_seq FROM team_tasks WHERE id=?", (task_id,)).fetchone()[0]
        db.execute("INSERT INTO team_events VALUES (?,?,?,?,?)", (task_id, seq, event_type, encoded, now))
        return {"task_id": task_id, "seq": seq, "type": event_type, "payload": payload, "created_at": now}

    def schema_version(self):
        """Trusted diagnostic: return the separate database's schema version."""
        with self._tx(write=False) as db:
            return db.execute("PRAGMA user_version").fetchone()[0]

    def configure_resource_group(self, group, capacity):
        """TRUSTED SERVER ONLY. Set physical capacity; never expose as a tool/API."""
        _text(group, "resource group")
        _integer(capacity, "capacity", 1, 1024)
        with self._tx() as db:
            if self._group_occupied(db, group) > capacity:
                raise Conflict("Cannot reduce capacity below current occupancy")
            db.execute("INSERT INTO team_resource_groups VALUES (?,?) ON CONFLICT(name) DO UPDATE SET capacity=excluded.capacity", (group, capacity))

    def list_owners_internal(self):
        """TRUSTED SCHEDULER ONLY: discover owners without leaking task contents."""
        with self._tx(write=False) as db:
            return [row[0] for row in db.execute("SELECT DISTINCT owner FROM team_tasks ORDER BY owner")]

    def create_task(self, owner, title, *, task_id=None, metadata=None, max_workers=None, budget_microusd=0):
        """Create a pending task; budget_microusd is an explicitly approved ceiling."""
        _text(owner, "owner"); _text(title, "title")
        if max_workers is not None:
            _integer(max_workers, "max_workers", 1)
        _integer(budget_microusd, "budget")
        task_id = _text(task_id or uuid.uuid4().hex, "task id")
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise ValueError("Task metadata must be an object")
        # Keep the legacy SQL column intact for rollback; resource groups now
        # govern concurrency unless the caller explicitly requests a ceiling.
        metadata = dict(metadata)
        if max_workers is not None:
            metadata['concurrency_limit'] = max_workers
        encoded, now = _json(metadata), self.clock()
        with self._tx() as db:
            try:
                db.execute("INSERT INTO team_tasks(id,owner,title,status,metadata,max_workers,budget_microusd,created_at,updated_at) VALUES (?,?,?,'pending',?,?,?,?,?)",
                           (task_id, owner, title, encoded, 4, budget_microusd, now, now))
            except sqlite3.IntegrityError:
                raise Conflict("Task id already exists") from None
            self._event(db, task_id, "task_created", {})
        return self.get_task(owner, task_id)

    def get_task(self, owner, task_id):
        """Return task, metadata and workers (never expose lease tokens)."""
        with self._tx(write=False) as db:
            result = _row(self._task(db, owner, task_id))
            result["workers"] = self._workers(db, task_id)
            return result

    def list_tasks(self, owner, *, limit=100):
        _text(owner, "owner"); _integer(limit, "limit", 1, 500)
        with self._tx(write=False) as db:
            return [_row(row) for row in db.execute("SELECT * FROM team_tasks WHERE owner=? ORDER BY created_at DESC,id LIMIT ?", (owner, limit))]

    def update_task_metadata(self, owner, task_id, patch, *, coordinator_token=None):
        """Shallow-merge owner-provided metadata without replacing other fields."""
        if not isinstance(patch, dict):
            raise ValueError("Metadata patch must be an object")
        with self._tx() as db:
            task = self._task(db, owner, task_id)
            if coordinator_token is not None:
                self._coordinator(db, owner, task_id, coordinator_token)
            metadata = json.loads(task["metadata"])
            metadata.update(patch)
            db.execute("UPDATE team_tasks SET metadata=? WHERE id=?", (_json(metadata), task_id))
            self._event(db, task_id, "task_metadata_updated", {"keys": list(patch)})
        return self.get_task(owner, task_id)

    def set_task_budget(self, owner, task_id, budget_microusd):
        """Replace the approved task ceiling, never below spent+reserved funds."""
        _integer(budget_microusd, "budget")
        with self._tx() as db:
            task = self._task(db, owner, task_id)
            if budget_microusd < task["spent_microusd"] + task["reserved_microusd"]:
                raise BudgetError("Budget below committed spending")
            db.execute("UPDATE team_tasks SET budget_microusd=? WHERE id=?", (budget_microusd, task_id))
            self._event(db, task_id, "budget_approved", {"budget_microusd": budget_microusd})

    def set_task_status(self, owner, task_id, status, *, coordinator_token=None):
        """Pause/resume/cancel explicitly; done/accepted require accepted workers."""
        if status not in _TASK_STATES:
            raise ValueError("Invalid task status")
        with self._tx() as db:
            task = self._task(db, owner, task_id)
            if coordinator_token is not None:
                self._coordinator(db, owner, task_id, coordinator_token)
            if task["status"] in {"cancelled", "accepted"} and status != task["status"]:
                raise Conflict("Terminal task cannot be resumed")
            if status in {"done", "accepted"}:
                workers = db.execute("SELECT status FROM team_workers WHERE task_id=?", (task_id,)).fetchall()
                if not workers or any(row[0] != "accepted" for row in workers):
                    raise Conflict("Workers are not all accepted")
            if status == "cancelled":
                for worker in db.execute("SELECT * FROM team_workers WHERE task_id=? AND status='running'", (task_id,)).fetchall():
                    self._lose_lease(db, worker, cancelled=True)
                db.execute("UPDATE team_workers SET status='cancelled',updated_at=? WHERE task_id=? AND status IN ('pending','paused','waiting_approval','failed','done')", (self.clock(), task_id))
            if status in {"paused", "cancelled"}:
                db.execute("DELETE FROM team_coordinator_leases WHERE task_id=?", (task_id,))
            db.execute("UPDATE team_tasks SET status=? WHERE id=?", (status, task_id))
            self._event(db, task_id, "task_status", {"status": status})
        return self.get_task(owner, task_id)

    def _workers(self, db, task_id):
        result = []
        for row in db.execute("SELECT * FROM team_workers WHERE task_id=? ORDER BY created_at,id", (task_id,)):
            worker = _row(row)
            worker["depends_on"] = [r[0] for r in db.execute("SELECT dependency_id FROM team_dependencies WHERE worker_id=? ORDER BY dependency_id", (row["id"],))]
            result.append(worker)
        return result

    def list_workers(self, owner, task_id):
        with self._tx(write=False) as db:
            self._task(db, owner, task_id)
            return self._workers(db, task_id)

    def task_for_scope(self, owner, scope):
        """Resolve either a task or its worker without accepting another owner."""
        with self._tx(write=False) as db:
            row = db.execute('SELECT t.* FROM team_tasks t WHERE t.owner=? AND (t.id=? OR EXISTS (SELECT 1 FROM team_workers w WHERE w.task_id=t.id AND w.id=?))',
                             (owner, scope, scope)).fetchone()
            if row is None:
                raise NotFound('Task scope not found')
            return _row(row)

    def get_worker(self, owner, task_id, worker_id):
        with self._tx(write=False) as db:
            self._worker(db, owner, task_id, worker_id)
            return next(worker for worker in self._workers(db, task_id) if worker["id"] == worker_id)

    def worker_status_page(self, owner, task_id, *, after_id='', limit=64, worker_id=None):
        """Bounded model-facing status without silently losing workers/dependencies.

        With worker_id, page that worker's dependencies; otherwise page workers.
        Cursors are exact IDs in lexical order, never offsets into mutable rows.
        """
        _integer(limit, 'limit', 1, 200)
        if not isinstance(after_id, str) or len(after_id) > 1024:
            raise ValueError('Invalid status cursor')
        with self._tx(write=False) as db:
            self._task(db, owner, task_id)
            if worker_id is not None:
                self._worker(db, owner, task_id, worker_id)
                rows = db.execute('SELECT dependency_id FROM team_dependencies WHERE worker_id=? AND dependency_id>? ORDER BY dependency_id LIMIT ?',
                                  (worker_id, after_id, limit + 1)).fetchall()
                dependencies = [row[0] for row in rows[:limit]]
                return {'worker_id': worker_id, 'depends_on': dependencies,
                        'next_cursor': dependencies[-1] if len(rows) > limit else None}
            rows = db.execute('SELECT id,name,status,profile FROM team_workers WHERE task_id=? AND id>? ORDER BY id LIMIT ?',
                              (task_id, after_id, limit + 1)).fetchall()
            workers = []
            for row in rows[:limit]:
                profile = json.loads(row['profile'])
                dependencies = [r[0] for r in db.execute('SELECT dependency_id FROM team_dependencies WHERE worker_id=? ORDER BY dependency_id LIMIT 65', (row['id'],))]
                workers.append({'id': row['id'], 'name': row['name'][:120], 'status': row['status'],
                                'role': str(profile.get('role', 'executor'))[:40], 'depends_on': dependencies[:64],
                                'dependencies_next_cursor': dependencies[63] if len(dependencies) > 64 else None})
            remaining = db.execute('SELECT count(*) FROM team_workers WHERE task_id=? AND id>?',
                                   (task_id, workers[-1]['id'] if workers else after_id)).fetchone()[0]
            return {'workers': workers, 'next_cursor': workers[-1]['id'] if len(rows) > limit else None,
                    'omitted_workers': remaining}

    def _dependencies(self, db, owner, task_id, worker_id, dependencies):
        if not isinstance(dependencies, (list, tuple)):
            raise ValueError("Dependencies must be a list of worker ids")
        dependencies = list(dict.fromkeys(dependencies))
        for dependency in dependencies:
            self._worker(db, owner, task_id, dependency)
        graph = {row["id"]: [] for row in db.execute("SELECT id FROM team_workers WHERE task_id=?", (task_id,))}
        for row in db.execute("SELECT d.worker_id,d.dependency_id FROM team_dependencies d JOIN team_workers w ON w.id=d.worker_id WHERE w.task_id=?", (task_id,)):
            graph[row[0]].append(row[1])
        graph[worker_id] = dependencies
        visited, active = set(), set()
        def visit(node):
            if node in active:
                raise Conflict("Dependency cycle")
            if node in visited:
                return
            active.add(node)
            for dependency in graph[node]:
                visit(dependency)
            active.remove(node); visited.add(node)
        for node in graph:
            visit(node)
        db.execute("DELETE FROM team_dependencies WHERE worker_id=?", (worker_id,))
        db.executemany("INSERT INTO team_dependencies VALUES (?,?)", [(worker_id, dependency) for dependency in dependencies])

    def _resource_groups(self, db, primary, profile):
        if not isinstance(profile, dict):
            raise ValueError("Worker profile must be an object")
        extra = profile.get("resource_groups", [])
        if not isinstance(extra, list):
            raise ValueError("profile.resource_groups must be a list")
        groups = set()
        for group in ([primary] if primary is not None else []) + extra:
            groups.add(_text(group, "resource group"))
            if not db.execute("SELECT 1 FROM team_resource_groups WHERE name=?", (group,)).fetchone():
                raise Conflict("Resource group is not configured")
        return groups

    def add_worker(self, owner, task_id, name, *, worker_id=None, depends_on=(), resource_group=None, profile=None, coordinator_token=None):
        """Add a queued DAG subtask. At most four workers may hold task leases."""
        _text(name, "name")
        worker_id = _text(worker_id or uuid.uuid4().hex, "worker id")
        with self._tx() as db:
            task = self._task(db, owner, task_id)
            if coordinator_token is not None:
                self._coordinator(db, owner, task_id, coordinator_token)
            if task["status"] in {"cancelled", "accepted", "done"}:
                raise Conflict("Task no longer accepts workers")
            profile = {} if profile is None else profile
            groups = self._resource_groups(db, resource_group, profile)
            now = self.clock()
            db.execute("INSERT INTO team_workers(id,task_id,name,status,profile,resource_group,created_at,updated_at) VALUES (?,?,?,'pending',?,?,?,?)",
                       (worker_id, task_id, name, _json(profile), resource_group, now, now))
            db.executemany("INSERT INTO team_worker_resources VALUES (?,?)", [(worker_id, group) for group in groups])
            self._dependencies(db, owner, task_id, worker_id, depends_on)
            self._event(db, task_id, "worker_added", {"worker_id": worker_id})
        return self.get_worker(owner, task_id, worker_id)

    def set_dependencies(self, owner, task_id, worker_id, depends_on):
        with self._tx() as db:
            worker = self._worker(db, owner, task_id, worker_id)
            if worker["status"] not in _WORKER_IDLE:
                raise Conflict("Only idle workers can change dependencies")
            self._dependencies(db, owner, task_id, worker_id, depends_on)
            self._event(db, task_id, "dependencies_updated", {"worker_id": worker_id})

    def update_worker(self, owner, task_id, worker_id, *, name=None, profile=None, status=None, resource_group=_UNSET):
        """Reassign only idle workers; unknown effectful outcomes cannot be reset."""
        with self._tx() as db:
            worker = self._worker(db, owner, task_id, worker_id)
            if worker["status"] not in _WORKER_IDLE:
                raise Conflict("Worker is not idle")
            if self._unknown(db, worker_id):
                raise Conflict("Reconcile unknown tool effects first")
            next_status = status or worker["status"]
            if next_status not in _WORKER_IDLE:
                raise ValueError("Invalid idle status")
            group = worker["resource_group"] if resource_group is _UNSET else resource_group
            next_profile = profile if profile is not None else json.loads(worker["profile"])
            groups = self._resource_groups(db, group, next_profile)
            db.execute("UPDATE team_workers SET name=?,profile=?,status=?,resource_group=?,updated_at=? WHERE id=?",
                       (_text(name, "name") if name is not None else worker["name"], _json(next_profile), next_status, group, self.clock(), worker_id))
            db.execute("DELETE FROM team_worker_resources WHERE worker_id=?", (worker_id,))
            db.executemany("INSERT INTO team_worker_resources VALUES (?,?)", [(worker_id, item) for item in groups])
            if next_status == "pending":
                db.execute("DELETE FROM team_worker_stop_requests WHERE worker_id=?", (worker_id,))
            self._event(db, task_id, "worker_guidance", {"worker_id": worker_id, "status": next_status})
        return self.get_worker(owner, task_id, worker_id)

    def _unknown(self, db, worker_id):
        return db.execute("SELECT 1 FROM team_tool_intents WHERE worker_id=? AND status='unknown' LIMIT 1", (worker_id,)).fetchone() is not None

    def stop_worker(self, owner, task_id, worker_id, *, status="paused"):
        """Fence a worker before cancelling its executor. Unknown effects stay blocked.

        Does not stop OS processes: caller must stop/reconcile managed host jobs.
        Accepted results remain immutable. Pause can later resume via update_worker.
        """
        if status not in {"paused", "cancelled"}:
            raise ValueError("Stop status must be paused or cancelled")
        with self._tx() as db:
            worker = self._worker(db, owner, task_id, worker_id)
            if worker["status"] == "accepted":
                raise Conflict("Accepted worker cannot be stopped")
            if worker["status"] == "cancelled" and status != "cancelled":
                raise Conflict("Cancelled worker cannot be resumed")
            if worker["status"] == "running":
                self._lose_lease(db, worker, cancelled=(status == "cancelled"))
            db.execute("INSERT INTO team_worker_stop_requests VALUES (?,?) ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status", (worker_id, status))
            actual = "blocked" if self._unknown(db, worker_id) else status
            db.execute("UPDATE team_workers SET status=?,updated_at=? WHERE id=?", (actual, self.clock(), worker_id))
            self._event(db, task_id, "worker_stopped", {"worker_id": worker_id, "status": actual, "requested_status": status})
        return self.get_worker(owner, task_id, worker_id)

    def _group_occupied(self, db, group):
        # Include the primary column for additive compatibility with records
        # written before the multi-group join was introduced. Pending effects
        # hold the slot even before their expired owner runs recover().
        return db.execute("SELECT count(*) FROM team_workers w WHERE (resource_group=? OR EXISTS (SELECT 1 FROM team_worker_resources r WHERE r.worker_id=w.id AND r.resource_group=?)) AND ((status='running' AND lease_expires>?) OR EXISTS (SELECT 1 FROM team_tool_intents i WHERE i.worker_id=w.id AND (i.status='unknown' OR (i.effectful=1 AND i.status='intent'))))", (group, group, self.clock())).fetchone()[0]

    def _lose_lease(self, db, worker, *, cancelled=False):
        now = self.clock()
        # Dispatch is required to persist mark_sent before HTTP. Reservations
        # that never crossed that boundary can safely be returned on recovery.
        for reservation in db.execute("SELECT * FROM team_reservations WHERE attempt_id=? AND status='reserved'", (worker["attempt_id"],)).fetchall():
            self._account(db, reservation, 0)
            db.execute("UPDATE team_reservations SET status='released',charged_microusd=0 WHERE id=?", (reservation["id"],))
            self._event(db, worker["task_id"], "cost_released", {"reservation_id": reservation["id"], "reason": "lease_lost_before_send"})
        db.execute("UPDATE team_tool_intents SET status=CASE WHEN effectful=1 THEN 'unknown' ELSE 'abandoned' END,updated_at=? WHERE worker_id=? AND attempt_id=? AND status='intent'", (now, worker["id"], worker["attempt_id"]))
        state = "blocked" if self._unknown(db, worker["id"]) else ("cancelled" if cancelled else "pending")
        db.execute("UPDATE team_attempts SET status=?,finished_at=? WHERE id=?", ("cancelled" if cancelled else "lease_expired", now, worker["attempt_id"]))
        db.execute("UPDATE team_workers SET status=?,lease_token=NULL,lease_expires=NULL,updated_at=? WHERE id=?", (state, now, worker["id"]))
        self._event(db, worker["task_id"], "lease_lost", {"worker_id": worker["id"], "status": state})
        return state

    def _recover(self, db, owner, task_id=None):
        _text(owner, "owner")
        if task_id is not None:
            self._task(db, owner, task_id)
        sql = "SELECT w.* FROM team_workers w JOIN team_tasks t ON t.id=w.task_id WHERE t.owner=? AND w.status='running' AND w.lease_expires<=?"
        args = [owner, self.clock()]
        if task_id is not None:
            sql += " AND t.id=?"; args.append(task_id)
        counts = {"requeued": 0, "blocked": 0}
        for worker in db.execute(sql, args).fetchall():
            state = self._lose_lease(db, worker)
            counts["blocked" if state == "blocked" else "requeued"] += 1
        return counts

    def recover(self, owner, task_id=None):
        """Expire only this owner's leases; never silently replay uncertain effects."""
        with self._tx() as db:
            return self._recover(db, owner, task_id)

    def claim_worker(self, owner, task_id, *, lease_seconds=60, worker_id=None):
        """Atomically claim an accepted-dependency-ready worker, else None.

        Return includes attempt_id and private lease_token; do not expose the
        claim response to a browser/model. Resource limits span all tasks/owners.
        """
        _integer(lease_seconds, "lease_seconds", 1, 3600)
        with self._tx() as db:
            task = self._task(db, owner, task_id)
            self._recover(db, owner, task_id)
            if task["status"] not in {"pending", "running"}:
                return None
            count = db.execute("SELECT count(*) FROM team_workers w WHERE task_id=? AND (status='running' OR EXISTS (SELECT 1 FROM team_tool_intents i WHERE i.worker_id=w.id AND i.status='unknown'))", (task_id,)).fetchone()[0]
            limit = json.loads(task["metadata"]).get("concurrency_limit")
            if limit is not None and count >= limit:
                return None
            if worker_id is not None:
                self._worker(db, owner, task_id, worker_id)
            candidates = db.execute("SELECT w.* FROM team_workers w WHERE task_id=? AND status='pending' AND NOT EXISTS (SELECT 1 FROM team_dependencies d JOIN team_workers p ON p.id=d.dependency_id WHERE d.worker_id=w.id AND p.status!='accepted') ORDER BY created_at,id", (task_id,)).fetchall()
            for worker in candidates:
                if worker_id is not None and worker["id"] != worker_id:
                    continue
                groups = self._resource_groups(db, worker["resource_group"], json.loads(worker["profile"]))
                if any(self._group_occupied(db, group) >= db.execute("SELECT capacity FROM team_resource_groups WHERE name=?", (group,)).fetchone()[0] for group in groups):
                    continue
                attempt_id, token, now = uuid.uuid4().hex, uuid.uuid4().hex, self.clock()
                number = db.execute("SELECT coalesce(max(attempt_no),0)+1 FROM team_attempts WHERE worker_id=?", (worker["id"],)).fetchone()[0]
                db.execute("INSERT INTO team_attempts VALUES (?,?,?,'running',?,?,NULL)", (attempt_id, worker["id"], number, token, now))
                db.execute("UPDATE team_workers SET status='running',attempt_id=?,lease_token=?,lease_expires=?,updated_at=? WHERE id=?", (attempt_id, token, now + lease_seconds, now, worker["id"]))
                db.execute("UPDATE team_tasks SET status='running' WHERE id=?", (task_id,))
                self._event(db, task_id, "worker_claimed", {"worker_id": worker["id"], "attempt_id": attempt_id})
                return _row(db.execute("SELECT * FROM team_workers WHERE id=?", (worker["id"],)).fetchone(), lease=True)
            return None

    def renew_lease(self, owner, task_id, worker_id, lease_token, *, lease_seconds=60):
        _integer(lease_seconds, "lease_seconds", 1, 3600)
        with self._tx() as db:
            self._lease(db, owner, task_id, worker_id, lease_token)
            db.execute("UPDATE team_workers SET lease_expires=?,updated_at=? WHERE id=?", (self.clock() + lease_seconds, self.clock(), worker_id))
            return _row(db.execute("SELECT * FROM team_workers WHERE id=?", (worker_id,)).fetchone())

    def finish_worker(self, owner, task_id, worker_id, lease_token, result, *, status="done"):
        """Fenced execution result. 'done' is NOT verifier acceptance."""
        if status not in {"done", "failed", "waiting_approval", "blocked", "paused"}:
            raise ValueError("Invalid finish status")
        with self._tx() as db:
            worker = self._lease(db, owner, task_id, worker_id, lease_token)
            if db.execute("SELECT 1 FROM team_tool_intents WHERE worker_id=? AND status IN ('intent','unknown') LIMIT 1", (worker_id,)).fetchone():
                raise Conflict("Unfinished tool intents must be resolved before finishing")
            now = self.clock()
            db.execute("UPDATE team_workers SET status=?,result=?,lease_token=NULL,lease_expires=NULL,updated_at=? WHERE id=?", (status, _json(result), now, worker_id))
            db.execute("UPDATE team_attempts SET status=?,finished_at=? WHERE id=?", (status, now, worker["attempt_id"]))
            self._event(db, task_id, "worker_finished", {"worker_id": worker_id, "status": status})
        return self.get_worker(owner, task_id, worker_id)

    def accept_worker(self, owner, task_id, worker_id, *, evidence=None, coordinator_token=None):
        """Record independent verification; only accepted dependencies unblock work."""
        with self._tx() as db:
            worker = self._worker(db, owner, task_id, worker_id)
            if coordinator_token is not None:
                self._coordinator(db, owner, task_id, coordinator_token)
            if worker["status"] != "done":
                raise Conflict("Only completed results can be accepted")
            db.execute("UPDATE team_workers SET status='accepted',updated_at=? WHERE id=?", (self.clock(), worker_id))
            self._event(db, task_id, "worker_accepted", {"worker_id": worker_id, "evidence": evidence})
        return self.get_worker(owner, task_id, worker_id)

    def reject_worker(self, owner, task_id, worker_id, reason, *, retry=True, coordinator_token=None):
        with self._tx() as db:
            worker = self._worker(db, owner, task_id, worker_id)
            if coordinator_token is not None:
                self._coordinator(db, owner, task_id, coordinator_token)
            if worker["status"] != "done":
                raise Conflict("Only completed results can be rejected")
            db.execute("UPDATE team_workers SET status=?,updated_at=? WHERE id=?", ("pending" if retry else "failed", self.clock(), worker_id))
            self._event(db, task_id, "worker_rejected", {"worker_id": worker_id, "reason": reason})

    def list_attempts(self, owner, task_id, worker_id):
        with self._tx(write=False) as db:
            self._worker(db, owner, task_id, worker_id)
            return [_row(row) for row in db.execute("SELECT * FROM team_attempts WHERE worker_id=? ORDER BY attempt_no", (worker_id,))]

    def save_checkpoint(self, owner, task_id, worker_id, lease_token, payload):
        """Persist redacted JSON under a live lease; returns id/seq/payload."""
        with self._tx() as db:
            worker = self._lease(db, owner, task_id, worker_id, lease_token)
            seq = db.execute("SELECT coalesce(max(seq),0)+1 FROM team_checkpoints WHERE worker_id=?", (worker_id,)).fetchone()[0]
            checkpoint_id = uuid.uuid4().hex
            db.execute("INSERT INTO team_checkpoints VALUES (?,?,?,?,?,?,?)", (checkpoint_id, task_id, worker_id, worker["attempt_id"], seq, _json(payload), self.clock()))
            self._event(db, task_id, "checkpoint_saved", {"worker_id": worker_id, "seq": seq})
            return _row(db.execute("SELECT * FROM team_checkpoints WHERE id=?", (checkpoint_id,)).fetchone())

    def load_checkpoint(self, owner, task_id, worker_id):
        """Return latest checkpoint dict or None, including previous attempts."""
        with self._tx(write=False) as db:
            self._worker(db, owner, task_id, worker_id)
            return _row(db.execute("SELECT * FROM team_checkpoints WHERE worker_id=? ORDER BY seq DESC LIMIT 1", (worker_id,)).fetchone())

    def record_tool_intent(self, owner, task_id, worker_id, lease_token, name, payload, *, effectful=False, idempotency_key=None):
        """Write before dispatch. Existing idempotency keys return their record;
        dispatch only a newly created intent, never a returned done/unknown one.
        """
        _text(name, "tool name")
        if type(effectful) is not bool:
            raise ValueError("effectful must be boolean")
        key, encoded = _text(idempotency_key or uuid.uuid4().hex, "idempotency key"), _json(payload)
        with self._tx() as db:
            worker = self._lease(db, owner, task_id, worker_id, lease_token)
            if self._unknown(db, worker_id):
                raise Conflict("Reconcile unknown tool outcomes before dispatching another action")
            existing = db.execute("SELECT * FROM team_tool_intents WHERE worker_id=? AND idempotency_key=?", (worker_id, key)).fetchone()
            if existing:
                if existing["name"] != name or existing["payload"] != encoded or bool(existing["effectful"]) != effectful:
                    raise Conflict("Idempotency key reused for different tool intent")
                return {**_row(existing), "created": False}
            intent_id, now = uuid.uuid4().hex, self.clock()
            db.execute("INSERT INTO team_tool_intents VALUES (?,?,?,?,?,?,?,?, 'intent',NULL,?,?)", (intent_id, task_id, worker_id, worker["attempt_id"], name, encoded, int(effectful), key, now, now))
            self._event(db, task_id, "tool_intent", {"worker_id": worker_id, "intent_id": intent_id, "effectful": effectful})
            return {**_row(db.execute("SELECT * FROM team_tool_intents WHERE id=?", (intent_id,)).fetchone()), "created": True}

    def _intent(self, db, owner, task_id, intent_id):
        self._task(db, owner, task_id)
        intent = db.execute("SELECT * FROM team_tool_intents WHERE id=? AND task_id=?", (intent_id, task_id)).fetchone()
        if not intent:
            raise NotFound("Tool intent not found")
        return intent

    def record_tool_result(self, owner, task_id, intent_id, lease_token, result):
        with self._tx() as db:
            intent = self._intent(db, owner, task_id, intent_id)
            worker = self._lease(db, owner, task_id, intent["worker_id"], lease_token)
            if intent["attempt_id"] != worker["attempt_id"] or intent["status"] != "intent":
                raise LeaseLost("Tool result belongs to an old or closed intent")
            status = 'unknown' if isinstance(result, dict) and result.get('outcome_unknown') is True else 'done'
            db.execute("UPDATE team_tool_intents SET status=?,result=?,updated_at=? WHERE id=?", (status, _json(result), self.clock(), intent_id))
            self._event(db, task_id, "tool_result", {"intent_id": intent_id, "worker_id": worker["id"]})

    def resolve_tool_intent(self, owner, task_id, intent_id, result, *, status="done"):
        """Explicit reconciliation of unknown effects, NOT an automatic retry."""
        if status not in {"done", "not_run"}:
            raise ValueError("Reconciliation must be done or not_run")
        with self._tx() as db:
            intent = self._intent(db, owner, task_id, intent_id)
            if intent["status"] != "unknown":
                raise Conflict("Only unknown effects need reconciliation")
            db.execute("UPDATE team_tool_intents SET status=?,result=?,updated_at=? WHERE id=?", (status, _json(result), self.clock(), intent_id))
            if not self._unknown(db, intent["worker_id"]):
                task = self._task(db, owner, task_id)
                stopped = db.execute("SELECT status FROM team_worker_stop_requests WHERE worker_id=?", (intent["worker_id"],)).fetchone()
                state = "cancelled" if task["status"] == "cancelled" else (stopped[0] if stopped else "pending")
                db.execute("UPDATE team_workers SET status=?,updated_at=? WHERE id=? AND status='blocked'", (state, self.clock(), intent["worker_id"]))
            self._event(db, task_id, "tool_reconciled", {"intent_id": intent_id, "status": status})

    def block_unknown_worker(self, owner, task_id, worker_id, lease_token):
        """Release this executor's lease without closing uncertain tool evidence."""
        with self._tx() as db:
            worker = self._lease(db, owner, task_id, worker_id, lease_token)
            if not self._unknown(db, worker_id):
                raise Conflict('No unknown tool outcome to reconcile')
            self._lose_lease(db, worker)
            self._event(db, task_id, 'worker_blocked', {'worker_id': worker_id,
                        'reason': 'Unknown tool outcome requires reconciliation'})

    def list_tool_intents(self, owner, task_id, worker_id=None):
        with self._tx(write=False) as db:
            self._task(db, owner, task_id)
            if worker_id is not None:
                self._worker(db, owner, task_id, worker_id)
                return [_row(row) for row in db.execute("SELECT * FROM team_tool_intents WHERE task_id=? AND worker_id=? ORDER BY created_at,id", (task_id, worker_id))]
            return [_row(row) for row in db.execute("SELECT * FROM team_tool_intents WHERE task_id=? ORDER BY created_at,id", (task_id,))]

    def get_tool_intent(self, owner, task_id, intent_id):
        with self._tx(write=False) as db:
            return _row(self._intent(db, owner, task_id, intent_id))

    def add_event(self, owner, task_id, event_type, payload, *, coordinator_token=None):
        with self._tx() as db:
            self._task(db, owner, task_id)
            if coordinator_token is not None:
                self._coordinator(db, owner, task_id, coordinator_token)
            return self._event(db, task_id, event_type, payload)

    def events(self, owner, task_id, *, after_seq=0, limit=100):
        """Return ordered events strictly after the durable task-local cursor."""
        _integer(after_seq, "after_seq"); _integer(limit, "limit", 1, 500)
        with self._tx(write=False) as db:
            self._task(db, owner, task_id)
            return [_row(row) for row in db.execute("SELECT * FROM team_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, after_seq, limit))]

    def save_profile(self, owner, name, profile):
        _text(owner, "owner"); _text(name, "profile name")
        with self._tx() as db:
            db.execute("INSERT INTO team_profiles VALUES (?,?,?,?) ON CONFLICT(owner,name) DO UPDATE SET profile=excluded.profile,updated_at=excluded.updated_at", (owner, name, _json(profile), self.clock()))

    def list_profiles(self, owner):
        _text(owner, "owner")
        with self._tx(write=False) as db:
            return [_row(row) for row in db.execute("SELECT * FROM team_profiles WHERE owner=? ORDER BY name", (owner,))]

    def get_profile(self, owner, name):
        _text(owner, "owner")
        with self._tx(write=False) as db:
            row = db.execute("SELECT * FROM team_profiles WHERE owner=? AND name=?", (owner, name)).fetchone()
            if row is None:
                raise NotFound("Profile not found")
            return _row(row)

    def add_artifact(self, owner, task_id, name, data, *, worker_id=None, lease_token=None, coordinator_token=None):
        """Store an artifact reference/metadata, never write an artifact file."""
        _text(name, "artifact name")
        with self._tx() as db:
            self._task(db, owner, task_id)
            if coordinator_token is not None:
                self._coordinator(db, owner, task_id, coordinator_token)
            if worker_id is not None:
                if coordinator_token is not None:
                    self._worker(db, owner, task_id, worker_id)
                else:
                    self._lease(db, owner, task_id, worker_id, lease_token)
            artifact_id = uuid.uuid4().hex
            db.execute("INSERT INTO team_artifacts VALUES (?,?,?,?,?,?)", (artifact_id, task_id, worker_id, name, _json(data), self.clock()))
            self._event(db, task_id, "artifact_added", {"artifact_id": artifact_id, "worker_id": worker_id})
            return _row(db.execute("SELECT * FROM team_artifacts WHERE id=?", (artifact_id,)).fetchone())

    def list_artifacts(self, owner, task_id):
        with self._tx(write=False) as db:
            self._task(db, owner, task_id)
            return [_row(row) for row in db.execute("SELECT * FROM team_artifacts WHERE task_id=? ORDER BY created_at,id", (task_id,))]

    def approve_endpoint(self, owner, task_id, endpoint_id, limit_microusd, input_rate_per_million, output_rate_per_million):
        """Explicit endpoint approval including BOTH integer rates (zero means free)."""
        _text(endpoint_id, "endpoint id")
        for value in (limit_microusd, input_rate_per_million, output_rate_per_million):
            _integer(value, "approval amount/rate")
        with self._tx() as db:
            self._task(db, owner, task_id)
            old = db.execute("SELECT * FROM team_approvals WHERE task_id=? AND endpoint_id=?", (task_id, endpoint_id)).fetchone()
            if old and limit_microusd < old["spent_microusd"] + old["reserved_microusd"]:
                raise BudgetError("Endpoint limit below committed spending")
            db.execute("INSERT INTO team_approvals(task_id,endpoint_id,limit_microusd,input_rate_per_million,output_rate_per_million) VALUES (?,?,?,?,?) ON CONFLICT(task_id,endpoint_id) DO UPDATE SET limit_microusd=excluded.limit_microusd,input_rate_per_million=excluded.input_rate_per_million,output_rate_per_million=excluded.output_rate_per_million,revoked=0,revision=revision+1", (task_id, endpoint_id, limit_microusd, input_rate_per_million, output_rate_per_million))
            self._event(db, task_id, "endpoint_approved", {"endpoint_id": endpoint_id, "limit_microusd": limit_microusd})

    def revoke_endpoint(self, owner, task_id, endpoint_id):
        with self._tx() as db:
            self._task(db, owner, task_id)
            if not db.execute("SELECT 1 FROM team_approvals WHERE task_id=? AND endpoint_id=?", (task_id, endpoint_id)).fetchone():
                raise NotFound("Approval not found")
            db.execute("UPDATE team_approvals SET revoked=1,revision=revision+1 WHERE task_id=? AND endpoint_id=?", (task_id, endpoint_id))
            self._event(db, task_id, "endpoint_revoked", {"endpoint_id": endpoint_id})

    def list_approvals(self, owner, task_id):
        with self._tx(write=False) as db:
            self._task(db, owner, task_id)
            return [_row(row) for row in db.execute("SELECT * FROM team_approvals WHERE task_id=? ORDER BY endpoint_id", (task_id,))]

    def budget_status(self, owner, task_id):
        """Return one consistent owner-scoped budget/approval/reservation snapshot."""
        with self._tx(write=False) as db:
            task = self._task(db, owner, task_id)
            result = {key: task[key] for key in ("budget_microusd", "spent_microusd", "reserved_microusd")}
            result["remaining_microusd"] = task["budget_microusd"] - task["spent_microusd"] - task["reserved_microusd"]
            result["approvals"] = [_row(row) for row in db.execute("SELECT * FROM team_approvals WHERE task_id=? ORDER BY endpoint_id", (task_id,))]
            result["reservations"] = [_row(row) for row in db.execute("SELECT * FROM team_reservations WHERE task_id=? ORDER BY created_at,id", (task_id,))]
            return result

    @staticmethod
    def _cost(input_tokens, output_tokens, input_rate, output_rate):
        _integer(input_tokens, "input tokens"); _integer(output_tokens, "output tokens")
        cost = (input_tokens * input_rate + output_tokens * output_rate + 999999) // 1000000
        return _integer(cost, "cost")

    def reserve(self, owner, task_id, worker_id, lease_token, endpoint_id, input_tokens, max_output_tokens):
        """Atomically reserve approved worst-case usage before dispatching HTTP."""
        with self._tx() as db:
            task = self._task(db, owner, task_id)
            worker = self._lease(db, owner, task_id, worker_id, lease_token)
            if task["status"] != "running":
                raise Conflict("Task is not running")
            approval = db.execute("SELECT * FROM team_approvals WHERE task_id=? AND endpoint_id=? AND revoked=0", (task_id, endpoint_id)).fetchone()
            if not approval:
                raise BudgetError("Endpoint rates and explicit approval required")
            cost = self._cost(input_tokens, max_output_tokens, approval["input_rate_per_million"], approval["output_rate_per_million"])
            if (task["spent_microusd"] + task["reserved_microusd"] + cost > task["budget_microusd"]
                    or approval["spent_microusd"] + approval["reserved_microusd"] + cost > approval["limit_microusd"]):
                raise BudgetError("Approved budget exhausted")
            reservation_id = uuid.uuid4().hex
            db.execute("INSERT INTO team_reservations(id,task_id,worker_id,attempt_id,endpoint_id,approval_revision,input_tokens,max_output_tokens,input_rate_per_million,output_rate_per_million,reserved_microusd,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'reserved',?)", (reservation_id, task_id, worker_id, worker["attempt_id"], endpoint_id, approval["revision"], input_tokens, max_output_tokens, approval["input_rate_per_million"], approval["output_rate_per_million"], cost, self.clock()))
            db.execute("UPDATE team_tasks SET reserved_microusd=reserved_microusd+? WHERE id=?", (cost, task_id))
            db.execute("UPDATE team_approvals SET reserved_microusd=reserved_microusd+? WHERE task_id=? AND endpoint_id=?", (cost, task_id, endpoint_id))
            self._event(db, task_id, "cost_reserved", {"reservation_id": reservation_id, "microusd": cost})
            return _row(db.execute("SELECT * FROM team_reservations WHERE id=?", (reservation_id,)).fetchone())

    def _reservation(self, db, owner, task_id, reservation_id):
        self._task(db, owner, task_id)
        row = db.execute("SELECT * FROM team_reservations WHERE id=? AND task_id=?", (reservation_id, task_id)).fetchone()
        if row is None:
            raise NotFound("Reservation not found")
        return row

    def mark_sent(self, owner, task_id, reservation_id, lease_token):
        """Fence and consume dispatch authority BEFORE starting the HTTP call."""
        with self._tx() as db:
            reservation = self._reservation(db, owner, task_id, reservation_id)
            worker = self._lease(db, owner, task_id, reservation["worker_id"], lease_token)
            if worker["attempt_id"] != reservation["attempt_id"]:
                raise LeaseLost("Reservation belongs to an old attempt")
            if reservation["status"] != "reserved":
                raise Conflict("Reservation already sent or closed")
            task = self._task(db, owner, task_id)
            approval = db.execute("SELECT * FROM team_approvals WHERE task_id=? AND endpoint_id=?", (task_id, reservation["endpoint_id"])).fetchone()
            if task["status"] != "running" or approval["revoked"] or approval["revision"] != reservation["approval_revision"]:
                raise BudgetError("Dispatch approval was revoked or changed")
            db.execute("UPDATE team_reservations SET status='sent' WHERE id=?", (reservation_id,))
            self._event(db, task_id, "cost_sent", {"reservation_id": reservation_id})

    def _account(self, db, reservation, cost):
        reserved = reservation["reserved_microusd"]
        db.execute("UPDATE team_tasks SET reserved_microusd=reserved_microusd-?,spent_microusd=spent_microusd+? WHERE id=?", (reserved, cost, reservation["task_id"]))
        db.execute("UPDATE team_approvals SET reserved_microusd=reserved_microusd-?,spent_microusd=spent_microusd+? WHERE task_id=? AND endpoint_id=?", (reserved, cost, reservation["task_id"], reservation["endpoint_id"]))

    def release(self, owner, task_id, reservation_id):
        """Release only a proven-unsent reservation; idempotent after release."""
        with self._tx() as db:
            reservation = self._reservation(db, owner, task_id, reservation_id)
            if reservation["status"] == "released":
                return
            if reservation["status"] != "reserved":
                raise Conflict("A sent reservation cannot be released")
            self._account(db, reservation, 0)
            db.execute("UPDATE team_reservations SET status='released',charged_microusd=0 WHERE id=?", (reservation_id,))
            self._event(db, task_id, "cost_released", {"reservation_id": reservation_id})

    def settle(self, owner, task_id, reservation_id, input_tokens, output_tokens):
        """Settle known actual usage once, even after lease loss; repeats must match."""
        return self._settle(owner, task_id, reservation_id, input_tokens, output_tokens, False)

    def settle_unknown(self, owner, task_id, reservation_id):
        """Conservatively consume the entire sent reservation; idempotent."""
        return self._settle(owner, task_id, reservation_id, None, None, True)

    def _settle(self, owner, task_id, reservation_id, input_tokens, output_tokens, unknown):
        with self._tx() as db:
            reservation = self._reservation(db, owner, task_id, reservation_id)
            state = "settled_unknown" if unknown else "settled"
            cost = reservation["reserved_microusd"] if unknown else self._cost(input_tokens, output_tokens, reservation["input_rate_per_million"], reservation["output_rate_per_million"])
            if reservation["status"] in {"settled", "settled_unknown"}:
                if (reservation["status"] == state and reservation["charged_microusd"] == cost
                        and reservation["actual_input_tokens"] == input_tokens and reservation["actual_output_tokens"] == output_tokens):
                    return _row(reservation)
                raise Conflict("Reservation already settled with different usage")
            if reservation["status"] != "sent":
                raise Conflict("Only a sent reservation can be settled")
            if cost > reservation["reserved_microusd"] or (not unknown and (
                    input_tokens > reservation["input_tokens"] or output_tokens > reservation["max_output_tokens"])):
                raise BudgetError("Usage exceeded enforced reservation; retain ceiling for reconciliation")
            self._account(db, reservation, cost)
            db.execute("UPDATE team_reservations SET status=?,charged_microusd=?,actual_input_tokens=?,actual_output_tokens=? WHERE id=?", (state, cost, input_tokens, output_tokens, reservation_id))
            self._event(db, task_id, "cost_settled", {"reservation_id": reservation_id, "microusd": cost, "unknown": unknown})
            return _row(db.execute("SELECT * FROM team_reservations WHERE id=?", (reservation_id,)).fetchone())

    def list_reservations(self, owner, task_id):
        with self._tx(write=False) as db:
            self._task(db, owner, task_id)
            return [_row(row) for row in db.execute("SELECT * FROM team_reservations WHERE task_id=? ORDER BY created_at,id", (task_id,))]
