import asyncio

import httpx
import pytest
from fastapi import HTTPException
from src import llm_core
from src.agent_loop import _checkpoint_generation_budgets, _checkpoint_summary_prompt
from src.context_policy import ContextPolicy


@pytest.mark.parametrize("content", ["", None, [{"type": "thinking", "thinking": "reason only"}]])
def test_checkpoint_rejects_reasoning_only(monkeypatch, content):
    class Client:
        async def post(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "choices": [{"message": {"content": content, "reasoning_content": "unverified idea"}}]})
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: Client())
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda *_: None)
    with pytest.raises(HTTPException, match="no answer content"):
        asyncio.run(llm_core.llm_call_async("http://checkpoint-test/v1", "qwen3-test",
            [{"role": "user", "content": "summarize evidence"}], max_retries=1,
            require_answer_content=True))


def test_local_thinking_checkpoint_requests_final_answer_mode(monkeypatch):
    captured = {}
    class Client:
        async def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "choices": [{"message": {"content": "verified summary"}}]})
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: Client())
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda *_: None)
    monkeypatch.setattr(llm_core, "_is_self_hosted_openai_compatible", lambda _url: True)
    result = asyncio.run(llm_core.llm_call_async(
        "http://192.168.50.4:1234/v1", "qwen3-test",
        [{"role": "user", "content": "summarize evidence"}], max_retries=1,
        require_answer_content=True,
    ))
    assert result == "verified summary"
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_checkpoint_retry_adds_user_no_think_and_extra_reasoning_headroom():
    prompt = [
        {"role": "system", "content": "Return only a summary. /no_think"},
        {"role": "user", "content": "Evidence"},
    ]
    prepared = _checkpoint_summary_prompt(prompt)
    assert prepared[-1]["content"].startswith("/no_think\n")
    assert prompt[-1]["content"] == "Evidence"
    assert _checkpoint_generation_budgets(ContextPolicy(
        summary_tokens=1200, output_reserve=4096,
    )) == [3600, 8192]
