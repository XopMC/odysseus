import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.agent_tools import model_interaction_tools as tools
from src.tool_capabilities import ToolRunSecurityContext
from src import tool_execution
from core.database import Base
from src.database import ChatSubagentEvent, ChatSubagentRun, Session, SessionLocal
from src.subagent_runtime import CHILD_CORE_TOOLS, SubagentRuntime, runtime


def test_subagent_disabled_fails_before_model_dispatch(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "off" if key == "agent_subagents_mode" else default)
    result = asyncio.run(tools.delegate_subagent(json.dumps({"objective": "Review this"}), {
        "current_endpoint_url": "http://local/v1/chat/completions", "current_model": "model-a",
    }))
    assert result["policy"] == "disabled_by_policy"
    assert result["exit_code"] == 1


def test_child_runtime_has_stable_file_and_verification_tool_core():
    assert CHILD_CORE_TOOLS == {
        "get_workspace", "ls", "glob", "grep", "read_file", "write_file",
        "edit_file", "apply_patch", "bash", "python", "read_tool_artifact",
        "publish_subagent_evidence", "manage_auto_research_lab", "todowrite",
    }


def test_restart_fences_children_owned_by_the_previous_worker(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = sessionmaker(bind=engine)
    monkeypatch.setattr("src.subagent_runtime.SessionLocal", store)
    db = store()
    db.add(Session(id="s", name="n", endpoint_url="u", model="m", owner="alice"))
    db.commit()
    db.add(ChatSubagentRun(
        id="c" * 32, parent_session_id="s", owner="alice", ordinal=1,
        name="Subagent 1", objective="check", assigned_context="", model="m",
        endpoint_id="ep", status="running", slot=1, worker_id="previous-worker",
        heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.commit(); db.close()
    restarted = SubagentRuntime()
    assert restarted.recover_stale() == 1
    children = restarted.list("alice", "s")
    assert children[0]["status"] == "interrupted"
    db = store(); row = db.query(ChatSubagentRun).one()
    assert row.slot is None
    assert "restarted" in row.error.lower()
    db.close()


def test_app_startup_fences_subagents_before_resuming_goals():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    recovery = source.split("async def _recover_detached_chat_work", 1)[1].split(
        "async def _recover_auto_research_work", 1,
    )[0]
    assert "subagent_runtime.recover_stale" in recovery
    assert recovery.index("subagent_runtime.recover_stale") < recovery.index("agent_runs.recover_durable_runs")


def test_same_model_subagent_is_bounded_and_has_stable_child_identity(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "same_model" if key == "agent_subagents_mode" else default)
    captured = {}

    async def spawn(**kwargs):
        captured.update(kwargs)
        return {"child_id": "a" * 32, "model": kwargs["model"], "status": "queued", "exit_code": 0}

    monkeypatch.setattr("src.subagent_runtime.runtime.spawn", spawn)
    result = asyncio.run(tools.delegate_subagent(json.dumps({
        "objective": "Check the parser", "context": "Only parser.py lines 1-20", "model": "same",
    }), {
        "current_endpoint_url": "http://local/v1/chat/completions", "current_model": "model-a",
        "current_headers": {"X-Test": "yes"}, "owner": "alice", "session_id": "s1",
        "access_mode": "full_access",
    }))
    assert result["exit_code"] == 0
    assert result["model"] == "model-a"
    assert len(result["child_id"]) == 32
    assert captured["endpoint_url"] == "http://local/v1/chat/completions"
    assert captured["headers"] == {"X-Test": "yes"}
    assert captured["assigned_context"] == "Only parser.py lines 1-20"
    assert captured["timeout_seconds"] == 21600
    assert captured["access_mode"] == "full_access"


def test_same_model_setting_overrides_a_hallucinated_child_model(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "same_model" if key == "agent_subagents_mode" else default)
    captured = {}

    async def spawn(**kwargs):
        captured.update(kwargs)
        return {"child_id": "b" * 32, "model": kwargs["model"], "status": "queued", "exit_code": 0}

    monkeypatch.setattr("src.subagent_runtime.runtime.spawn", spawn)
    result = asyncio.run(tools.delegate_subagent(json.dumps({
        "objective": "Check one part", "model": "sonnet",
    }), {
        "current_endpoint_url": "http://192.168.50.4:1234/v1/chat/completions",
        "current_model": "qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved",
    }))
    assert result["exit_code"] == 0
    assert captured["model"] == "qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved"


def test_selected_subagent_model_is_an_exact_allowlist(monkeypatch):
    values = {
        "agent_subagents_mode": "selected_models",
        "agent_subagent_models": "worker-a@endpoint, worker-b@endpoint",
    }
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    denied = asyncio.run(tools.delegate_subagent(json.dumps({
        "objective": "Check", "model": "other@endpoint",
    }), {"owner": "alice"}))
    assert denied["policy"] == "disabled_by_policy"


def test_selected_models_are_allocated_breadth_first_before_reuse(monkeypatch):
    allowed = [f"worker-{idx}@endpoint-{idx}" for idx in range(1, 6)]
    values = {
        "agent_subagents_mode": "selected_models",
        "agent_subagent_models": ",".join(allowed),
    }
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))

    def resolve(spec, owner=None):
        model, endpoint = spec.rsplit("@", 1)
        return f"http://{endpoint}/v1/chat/completions", model, {}

    counts = {}
    spawned = []

    def active_count(**kwargs):
        return counts.get((kwargs["model"], kwargs["endpoint_id"]), 0)

    async def spawn(**kwargs):
        key = (kwargs["model"], kwargs["endpoint_id"])
        counts[key] = counts.get(key, 0) + 1
        spawned.append(kwargs)
        return {"child_id": str(len(spawned)), "model": kwargs["model"],
                "status": "queued", "exit_code": 0}

    monkeypatch.setattr("src.ai_interaction._resolve_model", resolve)
    monkeypatch.setattr("src.subagent_runtime.runtime.active_count", active_count)
    monkeypatch.setattr("src.subagent_runtime.runtime.spawn", spawn)
    ctx = {"owner": "alice", "session_id": "s1", "subagent_state": {},
           "current_endpoint_url": "http://parent/v1/chat/completions",
           "current_model": "parent"}

    async def scenario():
        for idx in range(6):
            # Even if a model repeats the first allowed value, it is merely a
            # preference and cannot bypass breadth-first allocation.
            result = await tools.delegate_subagent(json.dumps({
                "objective": f"Task {idx}", "model": allowed[0],
            }), ctx)
            assert result["exit_code"] == 0

    asyncio.run(scenario())
    assert [row["model"] for row in spawned] == [
        "worker-1", "worker-2", "worker-3", "worker-4", "worker-5", "worker-1",
    ]
    assert all(row["max_active_for_model"] == 4 for row in spawned)


def test_selected_models_honor_individual_capacities(monkeypatch):
    allowed = ["worker-a@endpoint-a", "worker-b@endpoint-b", "worker-c@endpoint-c"]
    values = {
        "agent_subagents_mode": "selected_models",
        "agent_subagent_models": ",".join(allowed),
        "agent_subagent_model_limits": {
            allowed[0]: 1,
            allowed[1]: 2,
            allowed[2]: 4,
        },
    }
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))

    def resolve(spec, owner=None):
        model, endpoint = spec.rsplit("@", 1)
        return f"http://{endpoint}/v1/chat/completions", model, {}

    counts, spawned = {}, []

    def active_count(**kwargs):
        return counts.get((kwargs["model"], kwargs["endpoint_id"]), 0)

    async def spawn(**kwargs):
        key = (kwargs["model"], kwargs["endpoint_id"])
        counts[key] = counts.get(key, 0) + 1
        spawned.append(kwargs)
        return {"child_id": str(len(spawned)), "model": kwargs["model"],
                "status": "queued", "exit_code": 0}

    monkeypatch.setattr("src.ai_interaction._resolve_model", resolve)
    monkeypatch.setattr("src.subagent_runtime.runtime.active_count", active_count)
    monkeypatch.setattr("src.subagent_runtime.runtime.spawn", spawn)
    ctx = {"owner": "alice", "session_id": "s1", "subagent_state": {},
           "current_endpoint_url": "http://parent/v1/chat/completions",
           "current_model": "parent"}

    async def scenario():
        for idx in range(7):
            result = await tools.delegate_subagent(json.dumps({
                "objective": f"Task {idx}", "model": "auto",
            }), ctx)
            assert result["exit_code"] == 0

    asyncio.run(scenario())
    assert [row["model"] for row in spawned] == [
        "worker-a", "worker-b", "worker-c", "worker-b",
        "worker-c", "worker-c", "worker-c",
    ]
    assert [row["max_active_for_model"] for row in spawned] == [1, 2, 4, 2, 4, 4, 4]


def test_selected_parent_capacity_is_clamped_and_cache_tracks_limit_changes(monkeypatch):
    spec = "parent@endpoint-parent"
    values = {
        "agent_subagents_mode": "selected_models",
        "agent_subagent_models": spec,
        "agent_subagent_model_limits": {spec: 4},
    }
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr("src.ai_interaction._resolve_model", lambda _spec, owner=None: (
        "http://endpoint-parent/v1/chat/completions", "parent", {},
    ))
    captured = []
    monkeypatch.setattr("src.subagent_runtime.runtime.active_count", lambda **kwargs: 0)

    async def spawn(**kwargs):
        captured.append(kwargs)
        return {"child_id": str(len(captured)), "model": kwargs["model"],
                "status": "queued", "exit_code": 0}

    monkeypatch.setattr("src.subagent_runtime.runtime.spawn", spawn)
    state = {}
    ctx = {"owner": "alice", "session_id": "s1", "subagent_state": state,
           "current_endpoint_url": "http://endpoint-parent/v1/chat/completions",
           "current_model": "parent"}
    asyncio.run(tools.delegate_subagent(json.dumps({"objective": "one", "model": "auto"}), ctx))
    values["agent_subagent_model_limits"] = {spec: 1}
    asyncio.run(tools.delegate_subagent(json.dumps({"objective": "two", "model": "auto"}), ctx))
    assert [row["max_active_for_model"] for row in captured] == [3, 1]


def test_model_claimed_pin_cannot_bypass_breadth_first_allocation(monkeypatch):
    allowed = [f"worker-{idx}@endpoint-{idx}" for idx in range(1, 4)]
    values = {
        "agent_subagents_mode": "selected_models",
        "agent_subagent_models": ",".join(allowed),
    }
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))

    def resolve(spec, owner=None):
        model, endpoint = spec.rsplit("@", 1)
        return f"http://{endpoint}/v1/chat/completions", model, {}

    counts = {}
    spawned = []

    def active_count(**kwargs):
        return counts.get((kwargs["model"], kwargs["endpoint_id"]), 0)

    async def spawn(**kwargs):
        key = (kwargs["model"], kwargs["endpoint_id"])
        counts[key] = counts.get(key, 0) + 1
        spawned.append(kwargs)
        return {"child_id": str(len(spawned)), "model": kwargs["model"],
                "status": "queued", "exit_code": 0}

    monkeypatch.setattr("src.ai_interaction._resolve_model", resolve)
    monkeypatch.setattr("src.subagent_runtime.runtime.active_count", active_count)
    monkeypatch.setattr("src.subagent_runtime.runtime.spawn", spawn)
    ctx = {"owner": "alice", "session_id": "s1", "subagent_state": {},
           "current_endpoint_url": "http://parent/v1/chat/completions",
           "current_model": "parent"}

    async def scenario():
        for idx in range(4):
            result = await tools.delegate_subagent(json.dumps({
                "objective": f"Task {idx}", "model": allowed[0],
                "pin_model": True,
            }), ctx)
            assert result["exit_code"] == 0

    asyncio.run(scenario())
    assert [row["model"] for row in spawned] == [
        "worker-1", "worker-2", "worker-3", "worker-1",
    ]


def test_parent_model_gets_three_child_slots_then_allocator_uses_other_models(monkeypatch):
    values = {
        "agent_subagents_mode": "selected_models",
        "agent_subagent_models": "parent@endpoint-parent,worker@endpoint-worker",
    }
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))

    def resolve(spec, owner=None):
        model, endpoint = spec.rsplit("@", 1)
        return f"http://{endpoint}/v1/chat/completions", model, {}

    counts = {}
    spawned = []

    def active_count(**kwargs):
        return counts.get((kwargs["model"], kwargs["endpoint_id"]), 0)

    async def spawn(**kwargs):
        key = (kwargs["model"], kwargs["endpoint_id"])
        counts[key] = counts.get(key, 0) + 1
        spawned.append(kwargs)
        return {"child_id": str(len(spawned)), "model": kwargs["model"],
                "status": "queued", "exit_code": 0}

    monkeypatch.setattr("src.ai_interaction._resolve_model", resolve)
    monkeypatch.setattr("src.subagent_runtime.runtime.active_count", active_count)
    monkeypatch.setattr("src.subagent_runtime.runtime.spawn", spawn)
    ctx = {"owner": "alice", "session_id": "s1", "subagent_state": {},
           "current_endpoint_url": "http://endpoint-parent/v1/chat/completions",
           "current_model": "parent"}

    async def scenario():
        for idx in range(7):
            result = await tools.delegate_subagent(json.dumps({
                "objective": f"Task {idx}", "model": "auto",
            }), ctx)
            assert result["exit_code"] == 0

    asyncio.run(scenario())
    assert [row["model"] for row in spawned] == [
        "parent", "worker", "parent", "worker", "parent", "worker", "worker",
    ]
    assert [row["max_active_for_model"] for row in spawned] == [3, 4, 3, 4, 3, 4, 4]


def test_subagent_dispatch_preserves_current_route_parent_and_budget(monkeypatch):
    captured = {}

    async def dispatch(tool, content, session_id=None, owner=None, **ctx):
        captured.update(ctx, tool=tool, session_id=session_id, owner=owner)
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_document_tool_dispatch", dispatch)
    state = {"started": 0, "max_children": 4}
    description, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="delegate_subagent", content='{"objective":"check"}'),
        session_id="chat-1", owner="alice",
        security_context=ToolRunSecurityContext(run_id="parent-run"),
        current_endpoint_url="http://local/v1/chat/completions",
        current_model="model-a", current_headers={"X-Test": "yes"},
        subagent_state=state,
        disabled_tools={"bash"}, allowed_tools={"read_file", "delegate_subagent"},
    ))
    assert result["exit_code"] == 0
    assert description.startswith("delegate_subagent:")
    assert captured["current_endpoint_url"] == "http://local/v1/chat/completions"
    assert captured["current_model"] == "model-a"
    assert captured["current_headers"] == {"X-Test": "yes"}
    assert captured["parent_run_id"] == "parent-run"
    assert captured["subagent_state"] is state
    assert captured["parent_disabled_tools"] == {"bash"}
    assert captured["parent_allowed_tools"] == {"read_file", "delegate_subagent"}
    assert captured["tool"] == "delegate_subagent"


def test_subagent_timeout_fails_closed(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "same_model" if key == "agent_subagents_mode" else default)
    ctx = {
        "current_endpoint_url": "http://local/v1/chat/completions",
        "current_model": "model-a",
    }
    invalid_timeout = asyncio.run(tools.delegate_subagent(
        json.dumps({"objective": "Check", "timeout_seconds": "never"}), ctx,
    ))
    assert invalid_timeout["exit_code"] == 1
    assert "timeout" in invalid_timeout["error"].lower()


def test_manage_subagents_wait_defaults_to_first_completion(monkeypatch):
    captured = {}

    async def wait(owner, session_id, child_ids, **kwargs):
        captured.update(kwargs)
        return {"subagents": [], "completed": True, "exit_code": 0}

    monkeypatch.setattr("src.subagent_runtime.runtime.wait", wait)
    result = asyncio.run(tools.manage_subagents(json.dumps({
        "action": "wait", "child_ids": ["a", "b"],
    }), {"owner": "alice", "session_id": "s1"}))
    assert result["exit_code"] == 0
    assert captured["wait_for"] == "any"


def test_subagent_settings_and_timeline_contract_are_wired():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static/index.html").read_text(encoding="utf-8")
    settings = (root / "static/js/settings.js").read_text(encoding="utf-8")
    style = (root / "static/style.css").read_text(encoding="utf-8")
    app = (root / "static/app.js").read_text(encoding="utf-8")
    loop = (root / "src/agent_loop.py").read_text(encoding="utf-8")
    assert 'id="set-agentSubagentsMode"' in html
    assert 'id="set-agentSubagentModels"' in html
    assert 'id="set-agentSubagentModelsList"' in html
    assert 'id="set-agentSubagentModelsRefresh"' in html
    assert 'id="overflow-subagents-btn"' in html
    assert 'id="subagents-status"' in html
    assert "settingsModule.open('tools')" in app
    assert "agent_subagents_mode" in settings and "agent_subagent_models" in settings
    assert "agent_subagent_model_limits" in settings
    assert "subagent-model-limit" in settings and "subagent-model-limit" in style
    assert "/api/team/models?refresh=true" in settings
    assert "model) + '@' + String(endpointKey)" in settings
    assert "Never use create_session for subagents" in loop
    assert '"type": "tool_inventory"' in loop
    assert "manage_subagents" in loop
    assert "BEFORE waiting" in loop
    assert "distributes automatic children breadth-first" in loop
    assert ".subagent-message-row[hidden] { display:none !important; }" in style


def test_parallel_runtime_returns_immediately_and_caps_each_model_at_four(monkeypatch):
    owner = "parallel-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="parallel test", endpoint_url="http://local", model="parent", owner=owner))
    db.commit(); db.close()
    entered = []
    release = asyncio.Event()
    four_entered = asyncio.Event()

    async def held_model_loop(endpoint_url, model, messages, **kwargs):
        entered.append((model, asyncio.get_running_loop().time()))
        if len(entered) >= 4:
            four_entered.set()
        await release.wait()
        yield 'data: {"delta":"ok","round":1}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", held_model_loop)

    async def scenario():
        common = dict(owner=owner, session_id=session_id, parent_run_id="parent",
                      objective="work", assigned_context="", endpoint_url="http://local",
                      headers={}, endpoint_id="ep", timeout_seconds=60,
                      workspace=None, access_mode="ask_important")
        children = [await runtime.spawn(model="worker-a", **common) for _ in range(4)]
        await asyncio.wait_for(four_entered.wait(), timeout=2)
        assert len(entered) == 4
        assert max(t for _, t in entered) - min(t for _, t in entered) < 0.5
        assert all(row["exit_code"] == 0 for row in children)
        fifth = await runtime.spawn(model="worker-a", **common)
        assert fifth["policy"] == "model_capacity_exhausted"
        other = await runtime.spawn(model="worker-b", **common)
        assert other["exit_code"] == 0
        for _ in range(20):
            if len(entered) == 5:
                break
            await asyncio.sleep(0.01)
        assert len(entered) == 5
        release.set()
        await asyncio.gather(*(task for cid, task in list(runtime._tasks.items())
                               if cid in {row.get("child_id") for row in children + [other]}))

    try:
        asyncio.run(scenario())
    finally:
        db = SessionLocal()
        ids = [row.id for row in db.query(ChatSubagentRun).filter(ChatSubagentRun.owner == owner).all()]
        if ids:
            db.query(ChatSubagentEvent).filter(ChatSubagentEvent.child_id.in_(ids)).delete(synchronize_session=False)
            db.query(ChatSubagentRun).filter(ChatSubagentRun.id.in_(ids)).delete(synchronize_session=False)
        db.query(Session).filter(Session.id == session_id).delete(synchronize_session=False)
        db.commit(); db.close()


def test_runtime_can_reserve_one_parent_slot_by_capping_children_at_three(monkeypatch):
    owner = "parent-cap-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="parent cap test", endpoint_url="http://local",
                   model="parent", owner=owner))
    db.commit(); db.close()
    release = asyncio.Event()
    three_entered = asyncio.Event()
    entered = []

    async def held_model_loop(endpoint_url, model, messages, **kwargs):
        entered.append(model)
        if len(entered) == 3:
            three_entered.set()
        await release.wait()
        yield 'data: {"delta":"ok","round":1}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", held_model_loop)

    async def scenario():
        common = dict(owner=owner, session_id=session_id, parent_run_id="parent",
                      objective="work", assigned_context="", endpoint_url="http://local",
                      model="parent", headers={}, endpoint_id="ep", timeout_seconds=60,
                      workspace=None, access_mode="ask_important", max_active_for_model=3)
        children = [await runtime.spawn(**common) for _ in range(3)]
        await asyncio.wait_for(three_entered.wait(), timeout=2)
        fourth = await runtime.spawn(**common)
        assert fourth["policy"] == "model_capacity_exhausted"
        assert fourth["max_active_per_model"] == 3
        assert all(child["max_active_per_model"] == 3 for child in children)
        release.set()
        await asyncio.gather(*(runtime._tasks[child["child_id"]] for child in children))

    try:
        asyncio.run(scenario())
    finally:
        db = SessionLocal()
        ids = [row.id for row in db.query(ChatSubagentRun).filter(ChatSubagentRun.owner == owner).all()]
        if ids:
            db.query(ChatSubagentEvent).filter(ChatSubagentEvent.child_id.in_(ids)).delete(synchronize_session=False)
            db.query(ChatSubagentRun).filter(ChatSubagentRun.id.in_(ids)).delete(synchronize_session=False)
        db.query(Session).filter(Session.id == session_id).delete(synchronize_session=False)
        db.commit(); db.close()


def test_child_loop_inherits_parent_policy_and_persists_stream(monkeypatch):
    owner = "policy-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="policy test", endpoint_url="http://local", model="parent", owner=owner))
    db.commit(); db.close()
    captured = {}

    async def fake_loop(*args, **kwargs):
        captured.update(kwargs)
        yield 'data: {"type":"context_usage","data":{"model":"worker","context_percent":74,"auto_compact_enabled":true,"compactions":0}}\n\n'
        yield 'data: {"delta":"reason ","thinking":true,"round":1}\n\n'
        yield 'data: {"type":"tool_start","tool":"read_file","round":1}\n\n'
        yield 'data: {"type":"compacted","working_context":true,"before_tokens":7500,"after_tokens":4200,"checkpoint":{"ledger_hash":"abc"}}\n\n'
        yield 'data: {"type":"context_checkpoint","messages":[{"role":"user","content":"inspect"}],"ledger_hash":"abc","compactions":1}\n\n'
        yield 'data: {"delta":"done","round":1}\n\n'
        yield 'data: {"type":"metrics","data":{"output_tokens":1,"working_context":{"compactions":1,"auto_compact_enabled":true}}}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)

    async def scenario():
        result = await runtime.spawn(
            owner=owner, session_id=session_id, parent_run_id="parent",
            objective="inspect", assigned_context="", endpoint_url="http://local",
            model="worker", headers={}, endpoint_id="ep", timeout_seconds=30,
            workspace="/tmp", access_mode="ask_important",
            disabled_tools={"write_file"}, allowed_tools={"delegate_subagent"},
            external_untrusted_context_seen=True, delegated_credential=True,
        )
        task = runtime._tasks[result["child_id"]]
        await task
        row = runtime.get(owner, session_id, result["child_id"])
        assert row["status"] == "completed"
        assert row["result"] == "done"
        assert captured["external_untrusted_context_seen"] is True
        assert captured["delegated_credential"] is True
        assert captured["access_mode"] == "ask_important"
        assert captured["relevant_tools"] is None
        assert captured["forced_tools"] == set(CHILD_CORE_TOOLS)
        assert captured["workload"] == "subagent"
        assert "bash" not in captured["disabled_tools"]
        assert "read_file" not in captured["disabled_tools"]
        assert "write_file" in captured["disabled_tools"]
        assert "delegate_subagent" in captured["disabled_tools"]
        events = runtime.events(owner, session_id, child_id=result["child_id"], limit=100)
        assert {event["kind"] for event in events} >= {
            "created", "thinking", "delta", "tool_start", "status",
            "context_usage", "compacted", "context_checkpoint",
        }
        assert row["metrics"]["working_context"]["compactions"] == 1
        assert row["metrics"]["working_context"]["auto_compact_enabled"] is True

    try:
        asyncio.run(scenario())
    finally:
        db = SessionLocal()
        ids = [row.id for row in db.query(ChatSubagentRun).filter(ChatSubagentRun.owner == owner).all()]
        if ids:
            db.query(ChatSubagentEvent).filter(ChatSubagentEvent.child_id.in_(ids)).delete(synchronize_session=False)
            db.query(ChatSubagentRun).filter(ChatSubagentRun.id.in_(ids)).delete(synchronize_session=False)
        db.query(Session).filter(Session.id == session_id).delete(synchronize_session=False)
        db.commit(); db.close()


def test_parallel_children_receive_independent_model_contexts(monkeypatch):
    owner = "contexts-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="child contexts", endpoint_url="http://local",
                   model="parent", owner=owner))
    db.commit(); db.close()
    captured = []

    async def fake_loop(endpoint_url, model, messages, **kwargs):
        captured.append({
            "model": model,
            "history_id": id(kwargs["history_session"]),
            "messages": json.loads(json.dumps(messages)),
        })
        yield 'data: {"delta":"done","round":1}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)

    async def scenario():
        common = dict(owner=owner, session_id=session_id, parent_run_id="parent",
                      assigned_context="", endpoint_url="http://local", headers={},
                      endpoint_id="ep", timeout_seconds=30, workspace=None,
                      access_mode="ask_important")
        first = await runtime.spawn(objective="Inspect alpha only", model="worker-a", **common)
        second = await runtime.spawn(objective="Inspect beta only", model="worker-b", **common)
        await asyncio.gather(runtime._tasks[first["child_id"]], runtime._tasks[second["child_id"]])

    try:
        asyncio.run(scenario())
        assert len(captured) == 2
        assert len({item["history_id"] for item in captured}) == 2
        prompts = {item["model"]: item["messages"][-1]["content"] for item in captured}
        assert "alpha" in prompts["worker-a"] and "beta" not in prompts["worker-a"]
        assert "beta" in prompts["worker-b"] and "alpha" not in prompts["worker-b"]
    finally:
        db = SessionLocal()
        ids = [row.id for row in db.query(ChatSubagentRun).filter(ChatSubagentRun.owner == owner).all()]
        if ids:
            db.query(ChatSubagentEvent).filter(ChatSubagentEvent.child_id.in_(ids)).delete(synchronize_session=False)
            db.query(ChatSubagentRun).filter(ChatSubagentRun.id.in_(ids)).delete(synchronize_session=False)
        db.query(Session).filter(Session.id == session_id).delete(synchronize_session=False)
        db.commit(); db.close()


def test_child_retries_transient_transport_failure_only_before_first_tool(monkeypatch):
    owner = "retry-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="retry test", endpoint_url="http://local",
                   model="parent", owner=owner))
    db.commit(); db.close()
    calls = 0

    async def fake_loop(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'event: error\ndata: {"error":"Read timeout","status":504}\n\n'
            return
        yield 'data: {"delta":"recovered","round":1}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)

    async def scenario():
        child = await runtime.spawn(
            owner=owner, session_id=session_id, parent_run_id="parent",
            objective="safe retry", assigned_context="", endpoint_url="http://local",
            model="worker", headers={}, endpoint_id="ep", timeout_seconds=30,
            workspace=None, access_mode="ask_important",
        )
        await runtime._tasks[child["child_id"]]
        row = runtime.get(owner, session_id, child["child_id"])
        assert row["status"] == "completed"
        assert row["result"] == "recovered"
        assert calls == 2
        events = runtime.events(owner, session_id, child_id=child["child_id"], limit=100)
        assert [event["kind"] for event in events].count("transport_retry") == 1

    try:
        asyncio.run(scenario())
    finally:
        db = SessionLocal()
        ids = [row.id for row in db.query(ChatSubagentRun).filter(ChatSubagentRun.owner == owner).all()]
        if ids:
            db.query(ChatSubagentEvent).filter(ChatSubagentEvent.child_id.in_(ids)).delete(synchronize_session=False)
            db.query(ChatSubagentRun).filter(ChatSubagentRun.id.in_(ids)).delete(synchronize_session=False)
        db.query(Session).filter(Session.id == session_id).delete(synchronize_session=False)
        db.commit(); db.close()


def test_child_never_retries_after_tool_start(monkeypatch):
    owner = "no-retry-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="no retry test", endpoint_url="http://local",
                   model="parent", owner=owner))
    db.commit(); db.close()
    calls = 0

    async def fake_loop(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'data: {"type":"tool_start","tool":"read_file","round":1}\n\n'
        yield 'event: error\ndata: {"error":"Read timeout","status":504}\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)

    async def scenario():
        child = await runtime.spawn(
            owner=owner, session_id=session_id, parent_run_id="parent",
            objective="do not replay", assigned_context="", endpoint_url="http://local",
            model="worker", headers={}, endpoint_id="ep", timeout_seconds=30,
            workspace=None, access_mode="ask_important",
        )
        await runtime._tasks[child["child_id"]]
        row = runtime.get(owner, session_id, child["child_id"])
        assert row["status"] == "failed"
        assert calls == 1
        events = runtime.events(owner, session_id, child_id=child["child_id"], limit=100)
        assert not any(event["kind"] == "transport_retry" for event in events)

    try:
        asyncio.run(scenario())
    finally:
        db = SessionLocal()
        ids = [row.id for row in db.query(ChatSubagentRun).filter(ChatSubagentRun.owner == owner).all()]
        if ids:
            db.query(ChatSubagentEvent).filter(ChatSubagentEvent.child_id.in_(ids)).delete(synchronize_session=False)
            db.query(ChatSubagentRun).filter(ChatSubagentRun.id.in_(ids)).delete(synchronize_session=False)
        db.query(Session).filter(Session.id == session_id).delete(synchronize_session=False)
        db.commit(); db.close()


def test_child_terminal_failure_is_not_published_as_completed(monkeypatch):
    owner = "terminal-failure-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="terminal failure test", endpoint_url="http://local",
                   model="parent", owner=owner))
    db.commit(); db.close()
    calls = 0

    async def fake_loop(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'data: {"type":"context_compaction_failed","reason":"failed"}\n\n'
        yield ('data: {"type":"agent_terminal","data":{"failed":true,'
               '"failure":{"kind":"context_compaction","message":"No usable input budget"}}}\n\n')
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)

    async def scenario():
        child = await runtime.spawn(
            owner=owner, session_id=session_id, parent_run_id="parent",
            objective="terminal failure", assigned_context="", endpoint_url="http://local",
            model="worker", headers={}, endpoint_id="ep", timeout_seconds=30,
            workspace=None, access_mode="ask_important",
        )
        await runtime._tasks[child["child_id"]]
        row = runtime.get(owner, session_id, child["child_id"])
        assert row["status"] == "failed"
        assert "No usable input budget" in row["error"]
        assert calls == 1
        events = runtime.events(owner, session_id, child_id=child["child_id"], limit=100)
        kinds = [event["kind"] for event in events]
        assert "context_compaction_failed" in kinds
        assert "agent_terminal" in kinds
        assert "transport_retry" not in kinds

    try:
        asyncio.run(scenario())
    finally:
        db = SessionLocal()
        ids = [row.id for row in db.query(ChatSubagentRun).filter(ChatSubagentRun.owner == owner).all()]
        if ids:
            db.query(ChatSubagentEvent).filter(ChatSubagentEvent.child_id.in_(ids)).delete(synchronize_session=False)
            db.query(ChatSubagentRun).filter(ChatSubagentRun.id.in_(ids)).delete(synchronize_session=False)
        db.query(Session).filter(Session.id == session_id).delete(synchronize_session=False)
        db.commit(); db.close()


def test_local_transport_allows_four_subagent_prompts_in_flight(monkeypatch):
    import src.llm_core as llm_core

    monkeypatch.setenv("ODYSSEUS_LOCAL_MODEL_GATE", "true")
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda _url: True)
    monkeypatch.setattr(llm_core, "_SUBAGENT_MODEL_SLOTS", {})

    async def scenario():
        active = 0
        maximum = 0
        entered = 0
        four_entered = asyncio.Event()
        release = asyncio.Event()

        async def request():
            nonlocal active, maximum, entered
            async with llm_core._local_model_slot(
                "http://192.168.50.4:1234/v1/chat/completions",
                "worker-model", workload="subagent",
            ):
                active += 1
                entered += 1
                maximum = max(maximum, active)
                if entered == 4:
                    four_entered.set()
                await release.wait()
                active -= 1

        tasks = [asyncio.create_task(request()) for _ in range(5)]
        await asyncio.wait_for(four_entered.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert active == 4
        assert entered == 4
        release.set()
        await asyncio.gather(*tasks)
        assert entered == 5
        assert maximum == 4

    asyncio.run(scenario())


def test_long_history_post_processing_is_scoped_to_new_nodes():
    root = Path(__file__).resolve().parents[1]
    renderer = (root / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    style = (root / "static/style.css").read_text(encoding="utf-8")
    assert "const renderStartNode = box.lastElementChild" in renderer
    assert "newRoots.forEach(root => root.querySelectorAll('pre code:not(.hljs)')" in renderer
    assert "box.querySelectorAll('pre code:not(.hljs)')" not in renderer
    assert "content-visibility: auto" in style
