"""Prompt instructions must match the tool transport actually sent to a model."""

import asyncio
import json

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
