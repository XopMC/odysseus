import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from src.agent_tools import model_interaction_tools as tools
from src.tool_capabilities import ToolRunSecurityContext
from src import tool_execution
from src.database import ChatSubagentEvent, ChatSubagentRun, Session, SessionLocal
from src.subagent_runtime import runtime


def test_subagent_disabled_fails_before_model_dispatch(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "off" if key == "agent_subagents_mode" else default)
    result = asyncio.run(tools.delegate_subagent(json.dumps({"objective": "Review this"}), {
        "current_endpoint_url": "http://local/v1/chat/completions", "current_model": "model-a",
    }))
    assert result["policy"] == "disabled_by_policy"
    assert result["exit_code"] == 1


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
    }))
    assert result["exit_code"] == 0
    assert result["model"] == "model-a"
    assert len(result["child_id"]) == 32
    assert captured["endpoint_url"] == "http://local/v1/chat/completions"
    assert captured["headers"] == {"X-Test": "yes"}
    assert captured["assigned_context"] == "Only parser.py lines 1-20"


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
    assert "/api/team/models?refresh=true" in settings
    assert "model) + '@' + String(endpointKey)" in settings
    assert "Never use create_session for subagents" in loop
    assert '"type": "tool_inventory"' in loop
    assert "manage_subagents" in loop
    assert "BEFORE waiting" in loop
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


def test_child_loop_inherits_parent_policy_and_persists_stream(monkeypatch):
    owner = "policy-" + uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    db = SessionLocal()
    db.add(Session(id=session_id, name="policy test", endpoint_url="http://local", model="parent", owner=owner))
    db.commit(); db.close()
    captured = {}

    async def fake_loop(*args, **kwargs):
        captured.update(kwargs)
        yield 'data: {"delta":"reason ","thinking":true,"round":1}\n\n'
        yield 'data: {"type":"tool_start","tool":"read_file","round":1}\n\n'
        yield 'data: {"delta":"done","round":1}\n\n'
        yield 'data: {"type":"metrics","data":{"output_tokens":1}}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)

    async def scenario():
        result = await runtime.spawn(
            owner=owner, session_id=session_id, parent_run_id="parent",
            objective="inspect", assigned_context="", endpoint_url="http://local",
            model="worker", headers={}, endpoint_id="ep", timeout_seconds=30,
            workspace="/tmp", access_mode="ask_important",
            disabled_tools={"bash"}, allowed_tools={"read_file", "delegate_subagent"},
            external_untrusted_context_seen=True, delegated_credential=True,
        )
        task = runtime._tasks[result["child_id"]]
        await task
        row = runtime.get(owner, session_id, result["child_id"])
        assert row["status"] == "completed"
        assert row["result"] == "done"
        assert captured["external_untrusted_context_seen"] is True
        assert captured["delegated_credential"] is True
        assert captured["relevant_tools"] == {"read_file", "delegate_subagent"}
        assert captured["workload"] == "subagent"
        assert "bash" in captured["disabled_tools"]
        assert "delegate_subagent" in captured["disabled_tools"]
        events = runtime.events(owner, session_id, child_id=result["child_id"], limit=100)
        assert {event["kind"] for event in events} >= {"created", "thinking", "delta", "tool_start", "status"}

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
