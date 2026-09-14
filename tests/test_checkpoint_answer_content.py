import asyncio

import httpx
import pytest
from fastapi import HTTPException
from src import llm_core


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
