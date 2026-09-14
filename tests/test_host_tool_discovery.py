"""Host capability discovery must not depend on English/RAG intent keywords."""
import asyncio
import json
import pytest
from src import agent_loop


@pytest.mark.parametrize('text', ['У тебя есть доступ к локальной консоли?', 'Доступ есть'])
@pytest.mark.parametrize('disabled', [False, True])
@pytest.mark.parametrize('selection', [None, {'ui_control', 'web_search'}])
def test_trusted_host_tools_survive_selective_retrieval(monkeypatch, text, disabled, selection):
    sent = []
    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(agent_loop, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(agent_loop, 'estimate_tokens', lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, '_agent_route_tool_mode', lambda *a, **k: (True, False, True))
    monkeypatch.setattr(agent_loop, 'blocked_tools_for_owner', lambda owner: set())
    async def stream(candidates, messages, **kwargs):
        sent.append(kwargs.get('tools') or [])
        yield 'data: ' + json.dumps({'delta': 'Checked'}) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(agent_loop, 'stream_llm_with_fallback', stream)
    async def collect():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            'http://host.docker.internal:11434/v1', 'ornith-test',
            [{'role': 'user', 'content': text}], owner='xopmc',
            relevant_tools=selection, context_length=65536,
            disabled_tools={'bash'} if disabled else set(), max_rounds=2, _is_teacher_run=True)]
    asyncio.run(collect())
    names = {s['function']['name'] for s in sent[0]}
    assert ('bash' in names) is not disabled
    assert {'read_file', 'ls', 'get_workspace'} <= names
