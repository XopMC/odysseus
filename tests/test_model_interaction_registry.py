"""Tests for the model-interaction tools after their move to the agent_tools
registry (#3629): chat_with_model, ask_teacher, list_models.

The implementations now live in src/agent_tools/model_interaction_tools.py
(moved out of src/ai_interaction.py). These assert (1) the handlers are
registered in TOOL_HANDLERS, (2) each handler runs the moved logic and threads
session_id/owner from the ctx, and (3) tool_execution.py dispatches them
through the registry rather than the legacy dispatch_ai_tool elif.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import src.ai_interaction as ai_interaction
import src.llm_core as llm_core
import src.database as database
from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import model_interaction_tools as mit

_MODEL_TOOLS = ("chat_with_model", "manage_subagents", "ask_teacher", "list_models")


def test_model_interaction_tools_registered():
    for name in _MODEL_TOOLS:
        assert name in TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS"


def test_chat_with_model_threads_owner_and_returns(monkeypatch):
    seen = {}

    def fake_resolve(spec, owner=None):
        seen["spec"] = spec
        seen["owner"] = owner
        return ("http://x", "model-x", {})

    async def fake_call(url, model, messages, headers=None, timeout=None):
        seen["message"] = messages[-1]["content"]
        return "hi back"

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    res = asyncio.run(mit.ChatWithModelTool().execute(
        "model-x\nhello there", {"owner": "alice", "session_id": "s1"}))

    assert res == {"model": "model-x", "response": "hi back"}
    assert seen["owner"] == "alice"
    assert seen["spec"] == "model-x"
    assert seen["message"] == "hello there"


def test_ask_teacher_threads_owner_and_marks_teacher(monkeypatch):
    seen = {}

    def fake_resolve(spec, owner=None):
        seen["owner"] = owner
        return ("http://x", "teacher-x", {})

    async def fake_call(url, model, messages, headers=None, timeout=None):
        return "do this and that"

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    res = asyncio.run(mit.AskTeacherTool().execute(
        "teacher-x\nI am stuck", {"owner": "bob"}))

    assert res["teacher"] is True
    assert res["response"] == "do this and that"
    assert seen["owner"] == "bob"


def test_list_models_no_endpoints(monkeypatch):
    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return []

    class _S:
        def query(self, *a, **k):
            return _Q()

        def close(self):
            pass

    monkeypatch.setattr(database, "SessionLocal", lambda: _S())

    res = asyncio.run(mit.ListModelsTool().execute("", {}))
    assert res == {"results": "No enabled model endpoints configured."}


def test_list_models_uses_cached_inventory_and_hides_hidden_models(monkeypatch):
    endpoint = SimpleNamespace(
        is_enabled=True, name="LM Studio", endpoint_kind="local",
        cached_models=json.dumps(["qwen3.8-27b", "hidden-model"]),
        pinned_models=json.dumps(["qwen3.8-27b-worker"]),
        hidden_models=json.dumps(["hidden-model"]),
    )
    class _Q:
        def filter(self, *a, **k):
            return self
        def all(self):
            return [endpoint]
    class _S:
        def query(self, *a, **k):
            return _Q()
        def close(self):
            pass
    monkeypatch.setattr(database, "SessionLocal", lambda: _S())

    result = asyncio.run(mit.ListModelsTool().execute("qwen", {}))
    assert "Cached model inventory" in result["results"]
    assert "qwen3.8-27b" in result["results"]
    assert "qwen3.8-27b-worker" in result["results"]
    assert "hidden-model" not in result["results"]


def test_list_models_empty_cache_does_not_probe_provider(monkeypatch):
    endpoint = SimpleNamespace(
        is_enabled=True, name="LM Studio", endpoint_kind="local",
        cached_models="[]", pinned_models="[]", hidden_models="[]",
    )
    class _Q:
        def filter(self, *a, **k):
            return self
        def all(self):
            return [endpoint]
    class _S:
        def query(self, *a, **k):
            return _Q()
        def close(self):
            pass
    monkeypatch.setattr(database, "SessionLocal", lambda: _S())

    result = asyncio.run(mit.ListModelsTool().execute("", {}))
    assert "No cached models found" in result["results"]
    assert "endpoint settings" in result["results"]


def test_dispatched_via_registry_not_dispatch_ai_tool():
    """The model tools route through the registry (_document_tool_dispatch), and
    are no longer in the dispatch_ai_tool elif tuple."""
    source = (Path(__file__).resolve().parent.parent / "src" / "tool_execution.py").read_text(encoding="utf-8")
    assert 'elif tool in ("chat_with_model", "delegate_subagent", "manage_subagents", "ask_teacher", "list_models", "publish_subagent_evidence"):' in source

    marker = "from src.ai_interaction import dispatch_ai_tool"
    idx = source.index(marker)
    branch_head = source.rfind("elif tool in (", 0, idx)
    legacy_tuple = source[branch_head:idx]
    for name in _MODEL_TOOLS:
        assert f'"{name}"' not in legacy_tuple, f"{name} still routed via dispatch_ai_tool"
