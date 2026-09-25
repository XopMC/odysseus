"""A Goal answer must survive route fallback and an ask_user wait boundary."""

import asyncio
import json
from types import SimpleNamespace

import src.agent_loop as agent_loop
from routes.chat_routes import _restore_goal_checkpoint_messages


def test_goal_answer_replaces_both_foreground_message_views():
    question = "Сколько будет 2 + 2?"
    old_ledger = [{"role": "user", "content": "old task"}]
    ctx = SimpleNamespace(
        messages=[{"role": "system", "content": "runtime"}, {"role": "user", "content": "4"}],
        route_messages=[{"role": "system", "content": "stale route"}],
    )

    _restore_goal_checkpoint_messages(ctx, old_ledger, answer_question=question)

    assert ctx.messages == ctx.route_messages == [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": question},
        {"role": "user", "content": "4"},
    ]
    assert ctx.messages is not ctx.route_messages


def test_goal_answer_does_not_duplicate_question_already_in_checkpoint():
    question = "Сколько будет 2 + 2?"
    ledger = [{"role": "user", "content": "ask"}, {"role": "assistant", "content": question}]
    ctx = SimpleNamespace(messages=[{"role": "user", "content": "4"}], route_messages=[])

    _restore_goal_checkpoint_messages(ctx, ledger, answer_question=question)

    assert [item["content"] for item in ctx.route_messages] == ["ask", question, "4"]


def test_goal_guidance_does_not_reuse_an_old_question():
    ctx = SimpleNamespace(messages=[{"role": "user", "content": "continue"}], route_messages=[])

    _restore_goal_checkpoint_messages(ctx, [{"role": "user", "content": "task"}])

    assert [item["content"] for item in ctx.route_messages] == ["task", "continue"]


def test_goal_restore_keeps_fresh_runtime_and_project_context():
    project = {"role": "user", "content": "guarded project excerpt", "metadata": {
        "trusted": False, "source": "project memory and skills",
    }}
    ctx = SimpleNamespace(
        preface=[{"role": "system", "content": "current route policy"}],
        messages=[{"role": "system", "content": "current route policy"},
                  project, {"role": "user", "content": "continue"}],
        route_messages=[{"role": "system", "content": "current route policy"},
                        project, {"role": "assistant", "content": "stale history"}],
    )

    _restore_goal_checkpoint_messages(ctx, [{"role": "user", "content": "durable task"}])

    assert [item["content"] for item in ctx.route_messages] == [
        "current route policy", "guarded project excerpt", "durable task", "continue",
    ]


def test_ask_user_emits_durable_question_checkpoint(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_setting", lambda _key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    calls = []
    question = "Сколько будет 2 + 2?"

    async def stream(_candidates, _messages, **_kwargs):
        calls.append(1)
        block = "```ask_user\n" + json.dumps({
            "question": question, "options": [{"label": "3"}, {"label": "4"}],
        }, ensure_ascii=False) + "\n```"
        yield "data: " + json.dumps({"delta": block}, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    async def execute(_block, **_kwargs):
        return "ask_user", {
            "ask_user": {"question": question, "options": [{"label": "3"}, {"label": "4"}]},
            "output": "Asked the user: " + question,
            "exit_code": 0,
        }

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)

    async def collect():
        return [json.loads(chunk[6:]) async for chunk in agent_loop.stream_agent_loop(
            "http://test/v1/chat/completions", "test-model",
            [{"role": "user", "content": "Ask me first."}],
            max_rounds=3, relevant_tools={"ask_user"},
        ) if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]")]

    events = asyncio.run(collect())
    assert calls == [1]
    assert any(event.get("type") == "ask_user" for event in events)
    checkpoint = [event for event in events if event.get("type") == "context_checkpoint"][-1]
    assert any(item.get("role") == "assistant" and item.get("content") == question
               for item in checkpoint["messages"])
    assert any("Asked the user" in str(item.get("content"))
               for item in checkpoint["messages"])
