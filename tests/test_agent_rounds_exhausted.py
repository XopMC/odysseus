"""Regression: stream_agent_loop emits `rounds_exhausted` only when the round
cap is hit while still working, and NOT on a normal finish.

The decision is a `for/else` in the loop: the `else` runs only if no `break`
fired (break = done / budget / error). A refactor that adds a stray break or
return, or moves the done-break, could silently flip this. See PR #1999 / #1997.
"""

import asyncio
import json

import pytest

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    # Skip RAG/tool-index, MCP, and settings lookups; keep the real loop body,
    # _resolve_tool_blocks, and parse_tool_blocks.
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_text, max_rounds=2):
    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools={"bash"},
    )
    return _types(_collect(gen))


def test_emits_rounds_exhausted_when_cap_hit_mid_task(monkeypatch):
    _patch_common(monkeypatch)
    # Use a system-owned interaction result so this remains a loop-control test:
    # Bash output is workspace-derived and now correctly pauses for exact user
    # approval before a later Bash call.
    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=2,
    )
    exhausted = next(e for e in events if e.get("type") == "rounds_exhausted")
    assert exhausted["resource"] == "model_rounds"
    assert exhausted["used"] == exhausted["limit"] == 2
    assert len(exhausted["run_id"]) == 32


def test_tool_budget_event_has_exact_run_identity(monkeypatch):
    _patch_common(monkeypatch)

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, max_tool_calls=1, relevant_tools={"update_plan"},
    )))
    budget = next(event for event in events if event.get("type") == "budget_exceeded")
    assert budget["limit"] == budget["used"] == 1
    assert len(budget["run_id"]) == 32


def test_token_budget_stops_before_next_model_request(monkeypatch):
    _patch_common(monkeypatch)
    requests = []

    async def stream(_candidates, _messages, **_kwargs):
        requests.append(True)
        yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
        yield 'data: {"type":"usage","data":{"input_tokens":8,"output_tokens":4}}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, max_total_tokens=10, relevant_tools={"update_plan"},
    )))
    budget = next(event for event in events if event.get("type") == "budget_exceeded")
    assert budget["resource"] == "model_tokens"
    assert budget["used"] == 12
    assert budget["usage_source"] == "real"
    assert budget["limit"] == 10
    assert len(budget["run_id"]) == 32
    assert len(requests) == 1


def test_token_budget_does_not_park_a_completed_answer(monkeypatch):
    _patch_common(monkeypatch)

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"Final answer."}\n\n'
        yield 'data: {"type":"usage","data":{"input_tokens":8,"output_tokens":4}}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, max_total_tokens=10,
    )))
    assert not any(event.get("type") == "budget_exceeded" for event in events)


def test_token_budget_uses_estimate_when_provider_omits_usage(monkeypatch):
    _patch_common(monkeypatch)
    requests = []

    async def stream(_candidates, _messages, **_kwargs):
        requests.append(True)
        yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, max_total_tokens=10, relevant_tools={"update_plan"},
    )))
    budget = next(event for event in events if event.get("type") == "budget_exceeded")
    assert budget["resource"] == "model_tokens"
    assert budget["used"] >= 10
    assert budget["usage_source"] == "estimated"
    assert len(requests) == 1


@pytest.mark.parametrize("token_cap, expected_requests", [(0, 2), (10, 1)])
def test_active_goal_low_signal_turn_uses_agent_loop_not_direct_reply(
        monkeypatch, token_cap, expected_requests):
    _patch_common(monkeypatch)
    from src import host_execution

    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: False)
    monkeypatch.setattr(al, "_classify_agent_request", lambda messages, latest: {
        "low_signal": True, "continuation": False, "domains": [],
        "retrieval_query": latest,
    })
    monkeypatch.setattr(al, "_is_casual_low_signal", lambda latest: True)
    requests = []

    async def stream(_candidates, _messages, **_kwargs):
        requests.append(True)
        yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "hello"}],
        active_goal={"id": "safe-goal", "status": "active", "checkpoint": {}},
        max_rounds=2, max_total_tokens=token_cap, relevant_tools=None,
    )))
    assert len(requests) == expected_requests
    assert any(event.get("type") == ("budget_exceeded" if token_cap else "rounds_exhausted")
               for event in events)


def test_model_request_budget_event_stops_agent_without_empty_response(monkeypatch):
    _patch_common(monkeypatch)
    requests = []

    async def stream(_candidates, _messages, **kwargs):
        requests.append(True)
        assert callable(kwargs["on_model_request"])
        assert kwargs["on_model_request"]() is None
        denied = kwargs["on_model_request"]()
        yield 'data: ' + json.dumps(denied) + '\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, max_model_requests=1,
    )))
    budget = next(event for event in events if event.get("type") == "budget_exceeded")
    assert budget["resource"] == "model_requests"
    assert budget["used"] == budget["limit"] == 1
    assert len(budget["run_id"]) == 32
    assert "_budget_nonce" not in budget
    assert len(requests) == 1


def test_wall_time_budget_fences_next_model_request(monkeypatch):
    _patch_common(monkeypatch)
    requests = []
    clock = [1000.0]
    monkeypatch.setattr(al.time, "monotonic", lambda: clock[0])

    async def stream(_candidates, _messages, **kwargs):
        requests.append(True)
        assert kwargs["on_model_request"]() is None
        clock[0] += 2.0
        yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, max_wall_seconds=1, relevant_tools={"update_plan"},
    )))
    budget = next(event for event in events if event.get("type") == "budget_exceeded")
    assert budget["resource"] == "wall_seconds"
    assert budget["used"] >= budget["limit"] == 1
    assert len(requests) == 1
    assert len(budget["run_id"]) == 32


def test_wall_time_budget_does_not_start_second_tool(monkeypatch):
    _patch_common(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(al.time, "monotonic", lambda: clock[0])
    executed = []

    async def stream(_candidates, _messages, **kwargs):
        assert kwargs["on_model_request"]() is None
        yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] one\\"}\\n```\\n```update_plan\\n{\\"plan\\":\\"- [ ] two\\"}\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, *args, **kwargs):
        executed.append(block.tool_type)
        clock[0] += 2.0
        return (block.tool_type, {"output": "ok", "exit_code": 0})

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "execute_tool_block", execute)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, max_wall_seconds=1, relevant_tools={"update_plan"},
    )))
    assert executed == ["update_plan"]
    assert any(event.get("type") == "budget_exceeded" and event.get("resource") == "wall_seconds"
               for event in events)


def test_approved_plan_keeps_step_tool_visible_on_qwen_route(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "_is_odysseus_qwen_model", lambda _model: True)

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"Plan received."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "qwen3.8-fixture", [{"role": "user", "content": "Execute approved plan"}],
        max_rounds=1, relevant_tools={"update_plan"},
        approved_plan="- [ ] Compute\n- [ ] Verify",
    )))
    inventories = [event["data"]["tools"] for event in events if event.get("type") == "tool_inventory"]
    assert inventories
    assert all("update_plan_step" in tools for tools in inventories)
    assert not any("empty response" in str(event.get("delta", "")) for event in events)


def test_child_budget_denial_parks_goal_after_durable_tool_checkpoint(monkeypatch):
    _patch_common(monkeypatch)
    requests = []

    async def stream(_candidates, _messages, **_kwargs):
        requests.append(True)
        yield 'data: {"delta":"```delegate_subagent\\n{\\"objective\\":\\"Safe review\\"}\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, *args, **kwargs):
        security = kwargs.get("security_context")
        return ("delegate_subagent", {
            "error": "Run child limit reached", "exit_code": 1,
            "policy": "run_child_budget_exhausted", "resource": "children",
            "used": 2, "limit": 2, "run_id": security.run_id,
        })

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "execute_tool_block", execute)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless bounded task"}],
        max_rounds=3, relevant_tools={"delegate_subagent"}, access_mode="full_access",
    )))
    assert len(requests) == 1
    assert any(event.get("type") == "budget_exceeded" and event.get("resource") == "children"
               for event in events)
    assert any(event.get("type") == "context_checkpoint" for event in events)


def test_no_rounds_exhausted_on_normal_finish(monkeypatch):
    _patch_common(monkeypatch)
    # A plain answer (no tool block) -> done-break on round 1 -> no event.
    events = _run_loop(monkeypatch, "All done, here is your answer.", max_rounds=2)
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_emits_intent_nudge_exhausted_when_cap_is_exhausted(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(monkeypatch, "Let me check the logs", max_rounds=5)

    guard = next((e for e in events if e.get("type") == "intent_nudge_exhausted"), None)
    assert guard is not None, events
    assert guard["reason"] == "intent_without_action_nudge_cap"
    assert guard["nudges"] == 2


def test_emits_loop_breaker_triggered_when_loop_breaker_trips(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=6,
    )

    guard = next((e for e in events if e.get("type") == "loop_breaker_triggered"), None)
    assert guard is not None, events
    assert guard["reason"] == "loop_breaker_stall"


def test_identical_action_observation_warns_then_stops_with_round_text(monkeypatch):
    _patch_common(monkeypatch)
    events = _run_loop(
        monkeypatch,
        'Still checking.\n```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=7,
    )
    warnings = [e for e in events if e.get("type") == "loop_diagnostic_nudge"]
    stops = [e for e in events if e.get("type") == "loop_breaker_triggered"
             and e.get("reason") == "repeated_action_observation"]
    assert len(warnings) == 1, events
    assert len(stops) == 1, events


def test_goal_repeated_monologue_emits_explicit_stall_not_silent_question(monkeypatch):
    _patch_common(monkeypatch)
    from src.chat_work_store import store

    goal = {"id": "goal-1", "status": "active", "revision": 1}
    monkeypatch.setattr(store, "get", lambda *_: {"goal": goal})

    def update_goal(_owner, _session, _progress, checkpoint=None, *, waiting_user=False):
        goal["status"] = "waiting_user" if waiting_user else "active"
        goal["revision"] += 1
        return dict(goal)

    monkeypatch.setattr(store, "update_goal", update_goal)

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"Same progress without a tool."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Harmless verification"}],
        max_rounds=8, relevant_tools={"bash"}, session_id="fixture-goal",
        active_goal=dict(goal),
    )))
    assert any(e.get("type") == "loop_breaker_triggered"
               and e.get("reason") == "repeated_premature_stop" for e in events)
    assert goal["status"] == "waiting_user"
    assert not any(e.get("type") == "ask_user" for e in events)


def test_unknown_prior_effect_fences_agent_before_dispatch(monkeypatch):
    _patch_common(monkeypatch)
    from src.chat_effect_inbox import inbox
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"```bash\\necho fixture\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [{"id": "unsettled"}])
    monkeypatch.setattr(inbox, "record_intent", lambda *args: (_ for _ in ()).throw(
        AssertionError("must not create another effect intent")))

    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Run the fixture"}],
        session_id="fixture-chat", owner="alice", max_rounds=1,
        relevant_tools={"bash"}, access_mode="full_access",
    )))
    assert any(e.get("type") == "agent_terminal"
               and e.get("data", {}).get("failure", {}).get("kind") == "unknown_side_effect"
               for e in events), [e for e in events if e.get("type") == "tool_output"]
    assert not any(e.get("type") == "tool_start" for e in events)


def test_effect_intent_commits_before_tool_dispatch_and_result_settles(monkeypatch):
    _patch_common(monkeypatch)
    from src.chat_effect_inbox import inbox

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"```bash\\necho fixture\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    order = []
    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [])

    def record_intent(owner, session, run, call, name, content):
        order.append("intent")
        assert (owner, session, name, content) == (
            "alice", "fixture-chat", "bash", "echo fixture")
        return {"id": "effect-1", "created": True}

    def record_result(owner, session, intent, result):
        order.append("receipt")
        assert intent == "effect-1" and result["exit_code"] == 0
        return {"status": "done"}

    async def execute(block, *args, **kwargs):
        order.append("dispatch")
        return ("bash", {"output": "ok", "exit_code": 0})

    monkeypatch.setattr(inbox, "record_intent", record_intent)
    monkeypatch.setattr(inbox, "record_result", record_result)
    monkeypatch.setattr(al, "execute_tool_block", execute)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Run fixture"}],
        session_id="fixture-chat", owner="alice", max_rounds=1,
        relevant_tools={"bash"}, access_mode="full_access",
    )))
    assert order == ["intent", "dispatch", "receipt"]
    assert any(e.get("type") == "tool_start" and e.get("tool_call_id") == "round-1-tool-0"
               for e in events)
