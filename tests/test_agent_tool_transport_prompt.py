"""Prompt instructions must match the tool transport actually sent to a model."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import agent_loop


@pytest.mark.parametrize("mode", [
    (False, False, True),  # Ollama-compatible /v1 defaults to fenced tools.
    (False, True, False),  # Native Ollama route with native schemas disabled.
    (True, False, True),  # Verified native-capable gateway explicitly opted in.
    (False, False, False),  # Explicitly disabled native tools on another route.
])
def test_agent_prompt_agrees_with_sent_tool_transport(monkeypatch, mode):
    requests = []
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *args, **kwargs: mode)
    # Model a permitted admin route; anonymous/public routes correctly hide Bash.
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())

    async def stream(candidates, messages, **kwargs):
        requests.append((list(messages), kwargs.get("tools")))
        payload = {"delta": "The command returned ok."}
        yield "data: " + json.dumps(payload) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)

    async def collect():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            "http://host.docker.internal:11434/v1", "qwen3.8-27b-test",
            [{"role": "user", "content": "Run the command to verify the test."}],
            relevant_tools={"bash"}, context_length=65536, max_rounds=3,
            _is_teacher_run=True,
        )]

    asyncio.run(collect())
    prompt = "\n".join(str(message.get("content", "")) for message in requests[0][0]
                       if message.get("role") == "system")
    tools = requests[0][1] or []
    if mode[0]:
        assert any(tool["function"]["name"] == "bash" for tool in tools)
        assert "Only the tool schemas provided by the API" in prompt
        assert "```bash" not in prompt
    else:
        assert tools == []
        assert "```bash" in prompt
        assert "Only the tool schemas provided by the API" not in prompt


def test_active_goal_keeps_execution_tools_and_emits_inventory(monkeypatch):
    requests = []
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, True))
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())

    async def stream(candidates, messages, **kwargs):
        route = await kwargs["candidate_request_factory"](0, *candidates[0])
        requests.append(route)
        yield "data: " + json.dumps({"delta": "Waiting for the next verified step."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)

    async def collect():
        chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
            "http://host.docker.internal:1234/v1",
            "odysseus-qwen3-test",
            [{"role": "user", "content": "Continue."}],
            relevant_tools={"ask_user"},
            active_goal={"id": "goal-1", "status": "active", "objective": "Finish verified work"},
            context_length=65536,
            max_rounds=1,
            _is_teacher_run=True,
        )]
        return [json.loads(chunk[6:]) for chunk in chunks
                if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]")]

    events = asyncio.run(collect())
    inventory = next(event["data"] for event in events if event.get("type") == "tool_inventory")
    for name in ("bash", "read_file", "get_goal", "update_goal_progress", "complete_goal", "ask_user"):
        assert name in inventory["tools"]
    prompt = "\n".join(str(message.get("content", "")) for message in requests[0]["messages"])
    assert "bash" in prompt.lower()
    assert "complete_goal" in prompt
    assert len(inventory["revision"]) == 64
    assert len(inventory["route_revision"]) == 64


def test_model_switch_is_applied_to_the_next_agent_round(monkeypatch):
    seen = []
    history_session = SimpleNamespace(
        endpoint_url="http://first.test/v1", model="model-a", headers={}, history=[],
        context_checkpoint=None, context_checkpoint_count=0,
    )
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, True))
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    import src.model_context as model_context
    monkeypatch.setattr(model_context, "budget_context_for_model", lambda url, model, fallback=0: 64000 if model == "model-a" else 128000)

    async def execute(*args, **kwargs):
        return "bash", {"output": "ok", "exit_code": 0}
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)

    async def stream(candidates, messages, **kwargs):
        seen.append((candidates[0][0], candidates[0][1]))
        await kwargs["candidate_request_factory"](0, *candidates[0])
        if len(seen) == 1:
            history_session.endpoint_url = "http://second.test/v1"
            history_session.model = "model-b"
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [{
                "name": "bash", "arguments": json.dumps({"command": "echo ok"}),
            }]}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "done"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)

    async def collect():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            history_session.endpoint_url, history_session.model,
            [{"role": "user", "content": "Run and verify."}],
            relevant_tools={"bash"}, history_session=history_session,
            context_length=64000, max_rounds=2, _is_teacher_run=True,
        )]

    chunks = asyncio.run(collect())
    assert seen == [("http://first.test/v1", "model-a"), ("http://second.test/v1", "model-b")]
    inventories = [json.loads(chunk[6:])["data"] for chunk in chunks if '"type": "tool_inventory"' in chunk]
    assert len({item["route_revision"] for item in inventories}) == 2
    contexts = [json.loads(chunk[6:])["data"] for chunk in chunks if '"type": "context_usage"' in chunk]
    assert contexts[-1]["model"] == "model-b"
    assert contexts[-1]["context_length"] == 128000
