"""Real Team HTTP boundary and SQLite ownership; no model or host execution.

Only the runtime executor/host transport are substituted. Authentication,
request parsing, routing and durable owner checks run.
"""
import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from core.database import Base, Session as DbSession
from routes import team_routes
from src import team_config, team_runtime, team_host, host_execution
from src.team_store import TeamStore


class BoundaryRuntime:
    def __init__(self, store):
        self.store, self.active, self.calls = store, {}, []

    def snapshot(self, owner, team_id):
        task = self.store.get_task(owner, team_id)
        return {"team_id": team_id, "status": task["status"], "metadata": task["metadata"],
                "workers": self.store.list_workers(owner, team_id), "last_seq": task["event_seq"],
                "resources": self.store.budget_status(owner, team_id)}

    async def create(self, owner, session_id, body):
        self.calls.append(("create", owner, session_id, body))
        return {"team_id": "created", "status": "pending"}

    def event(self, owner, team_id, kind, payload):
        return self.store.add_event(owner, team_id, kind, payload)

    def approve_cloud(self, owner, team_id, body):
        self.store.get_task(owner, team_id)
        self.calls.append(("approve", owner, team_id, body))

    def add_worker(self, owner, team_id, body):
        self.store.get_task(owner, team_id)
        self.calls.append(("add_worker", owner, team_id, body))

    def start(self):
        self.calls.append(("start",))


@pytest.fixture
def team_client(monkeypatch, tmp_path):
    store = TeamStore(tmp_path / "teams.sqlite")
    runtime = BoundaryRuntime(store)
    metadata = {"session_id": "alice-chat", "config": {"trusted_host": True},
                "leader": {"endpoint_id": "local", "model": "coder"}, "participants": []}
    task = store.create_task("alice", "Private task", metadata=metadata)
    other_task = store.create_task("bob", "Other private task", metadata={**metadata, "session_id": "bob-chat"})
    worker = store.add_worker("alice", task["id"], "Private worker", profile=metadata["leader"])
    other_worker = store.add_worker("bob", other_task["id"], "Other worker", profile=metadata["leader"])
    monkeypatch.setattr(team_config, "enabled", lambda: True)
    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: owner in {"alice", "bob"})
    monkeypatch.setattr(team_runtime, "get_runtime", lambda: runtime)
    host_calls = []

    async def host_call(op, args, **kwargs):
        host_calls.append({"op": op, "args": args, **kwargs})
        return {"ok": True, "result": {"id": "host-job"}}
    monkeypatch.setattr(team_host, "call", host_call)

    # Keep the existing chat ownership gate real, including its database lookup.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DbSession.__table__])
    db_factory = sessionmaker(bind=engine)
    with db_factory() as db:
        for owner in ("alice", "bob"):
            for suffix in ("chat", "empty"):
                db.add(DbSession(id=f"{owner}-{suffix}", owner=owner, name="Team fixture",
                                 endpoint_url="http://127.0.0.1:11434/v1", model="coder"))
        db.commit()
    # The team route imports this module lazily. Other route tests deliberately
    # evict/reimport it, so a collection-time alias can point at an obsolete
    # module and leave the live ownership gate bound to the wrong test database.
    session_routes = importlib.import_module("routes.session_routes")
    monkeypatch.setattr(session_routes, "SessionLocal", db_factory)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(get_username_for_token=lambda token: {"alice-cookie": "alice", "bob-cookie": "bob"}.get(token))

    @app.middleware("http")
    async def test_auth(request, call_next):
        request.state.api_token = request.headers.get("X-Test-API-Token") == "1"
        request.state.current_user = app.state.auth_manager.get_username_for_token(request.cookies.get("odysseus_session"))
        return await call_next(request)

    router = team_routes.setup_team_routes()
    app.include_router(router)
    # Unexpected exceptions should expose their real cause, not turn into an
    # unhelpful JSONDecodeError when this fixture checks an HTTP JSON response.
    with TestClient(app, raise_server_exceptions=True) as client:
        client.cookies.set("odysseus_session", "alice-cookie")
        yield SimpleNamespace(client=client, app=app, router=router, runtime=runtime, store=store,
                              task=task, worker=worker, other_task=other_task,
                              other_worker=other_worker, host_calls=host_calls)
    engine.dispose()


ORIGIN = {"Origin": "http://testserver"}


def test_cancelled_task_and_worker_cannot_reopen_closed_host_scope(team_client):
    env = team_client
    env.store.stop_worker('alice', env.task['id'], env.worker['id'], status='cancelled')
    response = env.client.post(f"/api/team/{env.task['id']}/workers/{env.worker['id']}/resume", json={}, headers=ORIGIN)
    assert response.status_code == 409
    assert env.store.get_worker('alice', env.task['id'], env.worker['id'])['status'] == 'cancelled'
    env.store.set_task_status('alice', env.task['id'], 'cancelled')
    response = env.client.post(f"/api/team/{env.task['id']}/resume", json={}, headers=ORIGIN)
    assert response.status_code == 409
    assert env.store.get_task('alice', env.task['id'])['status'] == 'cancelled'


def test_feature_off_hides_discovery_and_never_constructs_runtime(team_client, monkeypatch):
    env = team_client
    monkeypatch.setattr(team_config, "enabled", lambda: False)
    monkeypatch.setattr(team_runtime, "get_runtime", lambda: pytest.fail("Disabled feature touched runtime"))
    assert env.client.get("/api/team/capabilities").json() == {"enabled": False, "host_enabled": False}
    assert env.client.get("/api/team/models").status_code == 404
    assert env.client.get("/api/team/profiles").status_code == 404
    assert env.client.post("/api/team/session/alice-empty/start", json={}, headers=ORIGIN).status_code == 404


@pytest.mark.parametrize("credential", ["missing", "invalid", "api-token", "internal-token"])
def test_only_interactive_owner_cookie_can_use_team(team_client, credential):
    env = team_client
    headers = {}
    if credential in {"missing", "invalid"}:
        env.client.cookies.clear()
        if credential == "invalid":
            env.client.cookies.set("odysseus_session", "untrusted-cookie")
    elif credential == "api-token":
        headers["X-Test-API-Token"] = "1"
    else:
        headers["X-Odysseus-Internal-Token"] = "delegated-token"
    response = env.client.get(f"/api/team/{env.task['id']}", headers=headers)
    assert response.status_code == (401 if credential in {"missing", "invalid"} else 403)
    assert env.client.get("/api/team/capabilities", headers=headers).json()["enabled"] is False


@pytest.mark.parametrize("origin", [None, "null", "https://another-device.example", "http://testserver:81"])
def test_authenticated_owner_can_mutate_from_any_browser_origin(team_client, origin):
    env = team_client
    headers = {} if origin is None else {"Origin": origin}
    response = env.client.post(f"/api/team/{env.task['id']}/pause", json={}, headers=headers)
    assert response.status_code == 200
    assert env.store.get_task("alice", env.task["id"])["status"] == "paused"


def test_host_allowlist_is_independent_of_cookie_login(team_client, monkeypatch):
    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: False)
    assert team_client.client.get("/api/team/capabilities").json()["enabled"] is False
    assert team_client.client.get(f"/api/team/{team_client.task['id']}").status_code == 403


def test_session_lookup_and_start_preserve_existing_chat_owner_gate(team_client):
    env = team_client
    assert env.client.get("/api/team/session/alice-chat").json()["team_id"] == env.task["id"]
    assert env.client.get("/api/team/session/alice-empty").json()["team_id"] is None
    for action in ("snapshot", "start"):
        for session in ("bob-chat", "nonexistent-chat"):
            response = (env.client.get(f"/api/team/session/{session}") if action == "snapshot" else
                        env.client.post(f"/api/team/session/{session}/start", json={}, headers=ORIGIN))
            assert response.status_code == 404
    assert env.runtime.calls == []


def test_start_is_scoped_to_cookie_owner_and_rejects_duplicate_active_task(team_client):
    env = team_client
    body = {"goal": "Run the test suite", "project_path": "/work", "leader": {"endpoint_id": "local", "model": "coder"}}
    assert env.client.post("/api/team/session/alice-chat/start", json=body, headers=ORIGIN).status_code == 409
    response = env.client.post("/api/team/session/alice-empty/start", json=body, headers=ORIGIN)
    assert response.status_code == 200
    assert env.runtime.calls == [("create", "alice", "alice-empty", body)]


def test_project_profile_save_and_load_are_owner_scoped(team_client):
    env = team_client
    env.store.save_profile("bob", "Foreign profile", {"project_path": "/private/bob"})
    assert env.client.get("/api/team/profiles").json() == {"profiles": []}
    body = {"name": "My project", "profile": {"project_path": "/work", "test_command": "pytest -q",
             "build_command": "", "constraints": "Keep public API"}}
    assert env.client.post("/api/team/profiles", json=body).status_code == 200
    profiles = env.client.get("/api/team/profiles").json()["profiles"]
    assert len(profiles) == 1 and profiles[0]["name"] == body["name"]
    assert profiles[0]["profile"] == body["profile"]
    assert len(env.store.list_profiles("bob")) == 1
    assert env.runtime.calls == [] and env.host_calls == []


@pytest.mark.parametrize("extra", [{"trusted_host": True}, {"external": True}, {"leader": {"endpoint_id": "cloud"}}])
def test_project_profiles_cannot_grant_tool_or_external_authority(team_client, extra):
    env = team_client
    response = env.client.post("/api/team/profiles", json={"name": "Do not grant", "profile": {"project_path": "/work", **extra}}, headers=ORIGIN)
    assert response.status_code == 400
    assert env.store.list_profiles("alice") == []


@pytest.mark.parametrize("target", ["foreign", "missing"])
@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None), ("GET", "/events", None), ("GET", "/resources", None),
    ("GET", "/artifacts", None), ("GET", "/intents", None),
    ("POST", "/pause", {}), ("POST", "/guidance", {"message": "Private guidance"}),
    ("POST", "/host", {"op": "terminal.list", "args": {}}),
])
def test_foreign_and_missing_team_ids_are_uniform_404_not_500(team_client, target, method, suffix, body):
    env = team_client
    team_id = env.other_task["id"] if target == "foreign" else "missing-team"
    response = env.client.request(method, f"/api/team/{team_id}{suffix}", json=body, headers=ORIGIN)
    assert response.status_code == 404
    assert "Other private task" not in response.text
    assert env.host_calls == []


@pytest.mark.parametrize("target", ["foreign", "missing"])
def test_worker_identifier_cannot_escape_owned_team_scope(team_client, target):
    env = team_client
    worker_id = env.other_worker["id"] if target == "foreign" else "missing-worker"
    url = f"/api/team/{env.task['id']}"
    assert env.client.post(f"{url}/workers/{worker_id}/pause", json={}, headers=ORIGIN).status_code == 404
    assert env.client.get(f"{url}/workers/{worker_id}/checkpoint").status_code == 404
    assert env.client.post(f"{url}/host", json={"op": "terminal.list", "args": {}, "worker_id": worker_id}, headers=ORIGIN).status_code == 404
    assert env.host_calls == []


def test_host_rpc_scope_is_server_selected_and_permission_revocation_is_effective(team_client):
    env = team_client
    url = f"/api/team/{env.task['id']}/host"
    body = {"op": "terminal.input", "args": {"id": "job", "data": "hello\n"}, "worker_id": env.worker["id"], "owner": "bob", "scope": "forged"}
    assert env.client.post(url, json=body, headers=ORIGIN).status_code == 200
    assert env.host_calls == [{"op": body["op"], "args": body["args"], "owner": "alice", "scope": env.worker["id"]}]
    env.store.update_task_metadata("alice", env.task["id"], {"config": {"trusted_host": False}})
    assert env.client.post(url, json=body, headers=ORIGIN).status_code == 403
    assert env.client.post(url, json={"op": "terminal.list", "args": {}}, headers=ORIGIN).status_code == 200
    assert env.client.post(url, json={"op": "sudo.execute", "args": {}}, headers=ORIGIN).status_code == 400
    assert len(env.host_calls) == 2


@pytest.mark.parametrize("body", [{"web": "false"}, {"trusted_host": 1}, {"owner": "bob"}, {"root": True}])
def test_permission_updates_require_explicit_known_booleans(team_client, body):
    env = team_client
    response = env.client.post(f"/api/team/{env.task['id']}/config", json=body, headers=ORIGIN)
    assert response.status_code == 400
    assert env.store.get_task("alice", env.task["id"])["metadata"]["config"] == {"trusted_host": True}


@pytest.mark.parametrize("body", [[], None, "not an object"])
def test_mutation_body_must_be_json_object(team_client, body):
    env = team_client
    response = env.client.post(f"/api/team/{env.task['id']}/guidance", content=json.dumps(body), headers={**ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 400


def test_cloud_consent_contract_is_forwarded_without_ambient_authority(team_client):
    env = team_client
    approval = {"endpoint_id": "external", "limit_microusd": 1000,
                "input_rate_per_million": 200, "output_rate_per_million": 400,
                "approved_context": "Only this task source files", "data_scope": "assigned_context", "consent": True}
    assert env.client.post(f"/api/team/{env.task['id']}/approvals", json=approval, headers=ORIGIN).status_code == 200
    assert env.runtime.calls == [("approve", "alice", env.task["id"], approval)]


def test_unknown_effect_reconciliation_requires_inspection_and_never_replays_host_action(team_client):
    env = team_client
    claim = env.store.claim_worker("alice", env.task["id"])
    intent = env.store.record_tool_intent("alice", env.task["id"], env.worker["id"], claim["lease_token"],
                                          "terminal.create", {"cwd": "/work"}, effectful=True)
    env.store.stop_worker("alice", env.task["id"], env.worker["id"], status="paused")
    base = f"/api/team/{env.task['id']}"
    assert env.client.get(f"{base}/intents").json()["intents"][0]["status"] == "unknown"
    body = {"status": "done", "confirmation": True, "result": {"output": "Inspected terminal output", "exit_code": 0}}
    invalid_results = ({"output": "Inspected terminal output"},
                       *({"output": "Inspected terminal output", "exit_code": value} for value in (None, True, "0", .5)))
    for invalid in ({**body, "confirmation": False}, {**body, "status": "retry"}, {**body, "result": {}},
                    *({**body, "result": result} for result in invalid_results)):
        assert env.client.post(f"{base}/intents/{intent['id']}/resolve", json=invalid, headers=ORIGIN).status_code == 400
        assert env.store.list_tool_intents("alice", env.task["id"])[0]["status"] == "unknown"
    assert env.client.post(f"{base}/intents/{intent['id']}/resolve", json=body, headers=ORIGIN).status_code == 200
    stored = env.store.list_tool_intents("alice", env.task["id"])[0]
    assert stored["status"] == "done" and stored["result"] == body["result"]
    assert env.host_calls == [] and env.runtime.calls == []


@pytest.mark.parametrize('exit_code', [None, 0, 1])
def test_reconciliation_not_run_persists_explicit_non_success(team_client, exit_code):
    env = team_client
    claim = env.store.claim_worker("alice", env.task["id"])
    intent = env.store.record_tool_intent("alice", env.task["id"], env.worker["id"], claim["lease_token"],
                                          "bash", {"command": "printf fixture"}, effectful=True)
    env.store.stop_worker("alice", env.task["id"], env.worker["id"], status="paused")
    result = {"output": "Inspected: command did not start"}
    if exit_code is not None:
        result['exit_code'] = exit_code
    body = {"status": "not_run", "confirmation": True, "result": result}
    response = env.client.post(f"/api/team/{env.task['id']}/intents/{intent['id']}/resolve", json=body, headers=ORIGIN)
    assert response.status_code == 200
    stored = env.store.list_tool_intents("alice", env.task["id"])[0]
    assert stored['status'] == 'not_run' and stored['result']['not_executed'] is True
    assert stored['result']['exit_code'] != 0
    assert env.host_calls == [] and env.runtime.calls == []


def test_unknown_effect_id_cannot_escape_owned_team(team_client):
    env = team_client
    body = {"status": "not_run", "confirmation": True, "result": {"output": "Inspected absence of effects"}}
    assert env.client.post(f"/api/team/{env.task['id']}/intents/missing/resolve", json=body, headers=ORIGIN).status_code == 404


def test_sse_replays_only_after_seq_with_real_store_payload_and_no_run_cancel(team_client):
    env = team_client
    first = env.store.add_event("alice", env.task["id"], "guidance", {"text": "earlier"})
    env.store.add_event("alice", env.task["id"], "worker.output", {"text": "continue", "worker_id": env.worker["id"]})
    second = env.store.events("alice", env.task["id"], after_seq=first["seq"])[0]
    endpoint = next(route.endpoint for route in env.router.routes if route.path == "/api/team/{team_id}/events")

    async def consume():
        polls = 0
        async def disconnected():
            nonlocal polls
            polls += 1
            return polls > 1
        request = SimpleNamespace(app=env.app, state=SimpleNamespace(api_token=False),
                                  cookies={"odysseus_session": "alice-cookie"}, headers=Headers(),
                                  is_disconnected=disconnected)
        response = await endpoint(env.task["id"], request, after_seq=first["seq"])
        assert response.headers["cache-control"] == "no-store"
        return "".join([part async for part in response.body_iterator])

    text = asyncio.run(consume())
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert events == [{"team_id": env.task["id"], **second}]
    assert events[0]["payload"]["text"] == "continue"
    assert env.runtime.active == {} and env.runtime.calls == []


def test_timeline_replays_durable_history_in_pages_and_is_owner_scoped(team_client):
    env = team_client
    existing = env.store.events("alice", env.task["id"], limit=500)
    before = existing[-1]['seq'] if existing else 0
    for index in range(3):
        env.store.add_event("alice", env.task["id"], "worker_delta", {
            "worker_id": env.worker["id"], "message_id": "turn-1", "text": str(index)})
    first = env.client.get(f"/api/team/{env.task['id']}/timeline?after_seq={before}&limit=2")
    assert first.status_code == 200
    body = first.json()
    assert [event['payload']['text'] for event in body['events']] == ['0', '1']
    assert body['next_cursor'] == body['events'][-1]['seq']
    second = env.client.get(f"/api/team/{env.task['id']}/timeline?after_seq={body['next_cursor']}&limit=2")
    assert [event['payload']['text'] for event in second.json()['events']] == ['2']
    assert second.json()['next_cursor'] is None
    assert env.client.get(f"/api/team/{env.other_task['id']}/timeline").status_code == 404
    assert env.client.get(f"/api/team/{env.task['id']}/timeline?limit=201").status_code == 400
