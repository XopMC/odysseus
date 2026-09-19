import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from src.agent_tools import model_interaction_tools as tools
from src.tool_capabilities import ToolRunSecurityContext
from src import tool_execution


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

    async def complete(url, model, messages, **kwargs):
        captured.update(url=url, model=model, messages=messages, kwargs=kwargs)
        return "Verified child result"

    monkeypatch.setattr("src.llm_core.llm_call_async", complete)
    result = asyncio.run(tools.delegate_subagent(json.dumps({
        "objective": "Check the parser", "context": "Only parser.py lines 1-20", "model": "same",
    }), {
        "current_endpoint_url": "http://local/v1/chat/completions", "current_model": "model-a",
        "current_headers": {"X-Test": "yes"}, "owner": "alice",
    }))
    assert result["exit_code"] == 0
    assert result["model"] == "model-a"
    assert len(result["child_run_id"]) == 32
    assert captured["url"] == "http://local/v1/chat/completions"
    assert captured["kwargs"]["headers"] == {"X-Test": "yes"}
    assert "no tools or additional permissions" in captured["messages"][0]["content"]
    assert "Only parser.py" in captured["messages"][1]["content"]


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
    ))
    assert result["exit_code"] == 0
    assert description.startswith("delegate_subagent:")
    assert captured["current_endpoint_url"] == "http://local/v1/chat/completions"
    assert captured["current_model"] == "model-a"
    assert captured["current_headers"] == {"X-Test": "yes"}
    assert captured["parent_run_id"] == "parent-run"
    assert captured["subagent_state"] is state
    assert captured["tool"] == "delegate_subagent"


def test_subagent_timeout_and_child_limit_fail_closed(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "same_model" if key == "agent_subagents_mode" else default)
    ctx = {
        "current_endpoint_url": "http://local/v1/chat/completions",
        "current_model": "model-a",
        "subagent_state": {"started": 4, "max_children": 4},
    }
    invalid_timeout = asyncio.run(tools.delegate_subagent(
        json.dumps({"objective": "Check", "timeout_seconds": "never"}), ctx,
    ))
    assert invalid_timeout["exit_code"] == 1
    assert "timeout" in invalid_timeout["error"].lower()
    exhausted = asyncio.run(tools.delegate_subagent(json.dumps({"objective": "Check"}), ctx))
    assert exhausted["policy"] == "budget_exhausted"


def test_subagent_settings_and_timeline_contract_are_wired():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static/index.html").read_text(encoding="utf-8")
    settings = (root / "static/js/settings.js").read_text(encoding="utf-8")
    app = (root / "static/app.js").read_text(encoding="utf-8")
    loop = (root / "src/agent_loop.py").read_text(encoding="utf-8")
    assert 'id="set-agentSubagentsMode"' in html
    assert 'id="set-agentSubagentModels"' in html
    assert 'id="overflow-subagents-btn"' in html
    assert "settingsModule.open('tools')" in app
    assert "agent_subagents_mode" in settings and "agent_subagent_models" in settings
    assert '"type": "tool_inventory"' in loop
    assert '"child_run_id": result["child_run_id"]' in loop
