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


def _run_loop(monkeypatch, round_text, max_rounds=2, *, active_goal=None, session_id=None):
    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools={"bash"},
        active_goal=active_goal,
        session_id=session_id,
    )
    return _types(_collect(gen))


def test_plan_mode_requires_tool_until_durable_plan_then_suppresses_more_tools(monkeypatch):
    _patch_common(monkeypatch)
    requests = []

    async def stream(_candidates, _messages, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            yield ('data: ' + json.dumps({"delta": (
                '```create_plan\n{"title":"Fixture plan","steps":[{"id":"one","text":"Step one"}]}\n```'
            )}) + '\n\n')
        else:
            yield 'data: {"delta":"Plan saved."}\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, *_args, **_kwargs):
        assert block.tool_type == "create_plan"
        return ("create_plan", {"plan_update": {"status": "draft", "revision": 1}, "output": "saved"})

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "execute_tool_block", execute)
    from src import context_efficiency_state
    monkeypatch.setattr(context_efficiency_state, "restore", lambda *_args: {"cache_write_read_ratio": 12.5})
    from src import chat_effect_inbox
    monkeypatch.setattr(chat_effect_inbox.inbox, "unknown", lambda *_args: [])

    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "qwen-local",
        [{"role": "user", "content": "Propose a simple plan."}],
        max_rounds=3, relevant_tools={"create_plan"}, plan_mode=True,
        session_id="fixture-chat", owner="alice",
    )))

    assert len(requests) == 2
    assert requests[0]["tool_choice_required"] is True
    assert requests[0]["tool_choice_none"] is False
    assert requests[1]["tool_choice_required"] is False
    assert requests[1]["tool_choice_none"] is True
    assert any(event.get("type") == "plan_update" for event in events)


def test_approved_plan_requires_progress_tool_until_plan_is_done(monkeypatch):
    _patch_common(monkeypatch)
    requests = []

    async def stream(_candidates, _messages, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            yield ('data: ' + json.dumps({"delta": (
                '```update_plan_step\n{"step_id":"one","status":"done"}\n```'
            )}) + '\n\n')
        else:
            yield 'data: {"delta":"Plan complete."}\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, *_args, **_kwargs):
        assert block.tool_type == "update_plan_step"
        return ("update_plan_step", {
            "plan_update": {"status": "done", "revision": 2},
            "output": "step updated",
        })

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "execute_tool_block", execute)
    from src import context_efficiency_state
    monkeypatch.setattr(context_efficiency_state, "restore", lambda *_args: {"cache_write_read_ratio": 12.5})
    from src import chat_effect_inbox
    monkeypatch.setattr(chat_effect_inbox.inbox, "unknown", lambda *_args: [])
    monkeypatch.setattr(chat_effect_inbox.inbox, "record_intent", lambda *_args: {"id": "safe-intent", "created": True})
    monkeypatch.setattr(chat_effect_inbox.inbox, "record_result", lambda *_args: {"status": "done"})

    _collect(al.stream_agent_loop(
        "http://x/v1", "qwen-local",
        [{"role": "user", "content": "Continue the approved plan."}],
        max_rounds=3, relevant_tools={"update_plan_step"},
        approved_plan="- [ ] Step one (step_id: one)",
        session_id="fixture-chat", owner="alice",
    ))

    assert len(requests) == 2
    assert requests[0]["tool_choice_required"] is True
    assert requests[0]["tool_choice_none"] is False
    assert requests[1]["tool_choice_required"] is False
    assert requests[1]["tool_choice_none"] is True


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


def test_agent_parses_local_coder_tool_name_alias_and_continues(monkeypatch):
    _patch_common(monkeypatch)
    from src import chat_effect_inbox

    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda _owner: set())
    from src import context_efficiency_state
    monkeypatch.setattr(context_efficiency_state, "restore", lambda *_args: None)
    monkeypatch.setattr(chat_effect_inbox.inbox, "unknown", lambda *_: [])
    monkeypatch.setattr(chat_effect_inbox.inbox, "record_intent", lambda *_: {
        "id": "safe-intent", "created": True,
    })
    monkeypatch.setattr(chat_effect_inbox.inbox, "record_result", lambda *_: {"status": "done"})
    requests = []

    async def stream(_candidates, _messages, **_kwargs):
        requests.append(True)
        if len(requests) == 1:
            # Synthetic fixture only; never execute user-provided shell text.
            yield ('data: ' + json.dumps({"delta": (
                '<tool_call>{"bbox_2d_id":"bash",'
                '"arguments":{"command":"printf fixture"}}</tool_call>'
            )}) + '\n\n')
        else:
            yield 'data: {"delta":"Finished safely."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    calls = []

    async def execute(block, *_args, **_kwargs):
        calls.append((block.tool_type, block.content))
        return (block.tool_type, {"output": "synthetic result", "exit_code": 0})

    monkeypatch.setattr(al, "execute_tool_block", execute)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "local-coder",
        [{"role": "user", "content": "Use the tool and continue"}],
        max_rounds=3, relevant_tools={"bash"}, session_id="fixture-chat",
        owner="alice", access_mode="full_access",
    )))
    assert requests == [True, True]
    assert calls == [("bash", "printf fixture")]
    assert any(event.get("type") == "tool_output" for event in events)
    assert not any(event.get("type") == "rounds_exhausted" for event in events)


def test_agent_tps_uses_model_stream_time_and_excludes_tool_wait(monkeypatch):
    _patch_common(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(al.time, "monotonic", lambda: clock[0])

    async def stream(_candidates, _messages, **_kwargs):
        clock[0] += 20.0  # TTFT/prefill/queue time must not enter decode TPS.
        yield 'data: {"delta":"Answer"}\n\n'
        clock[0] += 2.0  # Two seconds from first output to end of generation.
        yield 'data: {"type":"usage","data":{"input_tokens":10,"output_tokens":100}}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "test-model", [{"role": "user", "content": "Hello"}],
        max_rounds=1,
        active_goal={"id": "safe-goal", "status": "active", "checkpoint": {}},
    )))
    metrics = next(event["data"] for event in events if event.get("type") == "metrics")
    assert metrics.get("tokens_per_second") == 50.0, metrics
    assert metrics["tps_source"] == "stream_elapsed"
    assert metrics["tps_coverage_percent"] == 100.0
    assert metrics["round_generation_metrics"][0]["tps_source"] == "stream_elapsed"


def test_buffered_tool_call_without_decode_timing_is_not_reported_as_tps(monkeypatch):
    _patch_common(monkeypatch)
    from src import context_efficiency_state
    monkeypatch.setattr(context_efficiency_state, "restore", lambda *_args: {"cache_write_read_ratio": 1.0})

    async def stream(_candidates, _messages, **_kwargs):
        # A server may buffer the entire call and emit no argument deltas.
        # Request start includes prefill, so it cannot stand in for decode start.
        yield 'data: ' + json.dumps({"type": "tool_calls", "calls": []}) + '\n\n'
        yield 'data: ' + json.dumps({"type": "usage", "data": {"input_tokens": 10, "output_tokens": 100}}) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "A harmless buffered call fixture."}],
        max_rounds=1, relevant_tools={"python"}, session_id="fixture-chat",
    )))
    metrics = next(event["data"] for event in events if event.get("type") == "metrics")
    round_metric = metrics["round_generation_metrics"][0]
    assert round_metric["timing_basis"] == "buffered_tool_call"
    assert round_metric["tps_source"] == "unavailable_buffered"
    assert metrics["tokens_per_second"] == 0
    assert metrics["tps_source"] == "unavailable"
    assert metrics["tps_measured_tokens"] == 0
    assert metrics["tps_coverage_percent"] == 0.0


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


def test_soft_token_and_transport_warnings_are_visible_before_hard_stop(monkeypatch):
    _patch_common(monkeypatch)
    calls = 0

    async def stream(_candidates, _messages, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["on_model_request"]() is None
        if calls == 1:
            yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
            yield 'data: {"type":"usage","data":{"input_tokens":8,"output_tokens":0}}\n\n'
        else:
            yield 'data: {"delta":"Finished safely."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Safe budget fixture"}],
        max_rounds=4, max_total_tokens=10, max_model_requests=2,
        relevant_tools={"update_plan"},
    )))
    warnings = {event["resource"]: event for event in events if event.get("type") == "budget_warning"}
    assert warnings["model_tokens"]["used"] == 8
    assert warnings["model_tokens"]["limit"] == 10
    assert warnings["model_requests"]["used"] == 1
    assert warnings["model_requests"]["soft_limit"] == 1
    assert len(warnings["model_requests"]["run_id"]) == 32
    assert not any(event.get("type") in {"budget_exceeded", "rounds_exhausted"} for event in events)
    assert calls == 2


def test_tool_soft_warning_does_not_pause_before_hard_tool_cap(monkeypatch):
    _patch_common(monkeypatch)
    calls = 0

    async def stream(_candidates, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
        else:
            yield 'data: {"delta":"Finished safely."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Safe tool budget fixture"}],
        max_rounds=4, max_tool_calls=2, relevant_tools={"update_plan"},
    )))
    warning = next(event for event in events if event.get("type") == "budget_warning")
    assert warning["resource"] == "tool_calls"
    assert (warning["used"], warning["soft_limit"], warning["limit"]) == (1, 1, 2)
    assert not any(event.get("type") == "budget_exceeded" for event in events)
    assert calls == 3


def test_model_round_soft_warning_precedes_rounds_exhausted(monkeypatch):
    _patch_common(monkeypatch)

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"```update_plan\\n{\\"plan\\":\\"- [ ] next\\"}\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Safe round budget fixture"}],
        max_rounds=2, relevant_tools={"update_plan"},
    )))
    warning = next(event for event in events if event.get("type") == "budget_warning")
    assert warning["resource"] == "model_rounds"
    assert (warning["used"], warning["soft_limit"], warning["limit"]) == (1, 1, 2)
    assert any(event.get("type") == "rounds_exhausted" for event in events)


def test_child_soft_warning_uses_run_scoped_child_usage(monkeypatch):
    _patch_common(monkeypatch)
    calls = 0

    async def stream(_candidates, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'data: {"delta":"```delegate_subagent\\n{\\"objective\\":\\"Safe child\\"}\\n```"}\n\n'
        else:
            yield 'data: {"delta":"Finished safely."}\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, *args, **kwargs):
        return block.tool_type, {
            "child_id": "a" * 32, "status": "queued", "exit_code": 0,
            "run_children_used": 2, "run_children_limit": 2,
        }

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "execute_tool_block", execute)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Safe child budget fixture"}],
        max_rounds=3, relevant_tools={"delegate_subagent"}, access_mode="full_access",
    )))
    warning = next(event for event in events if event.get("type") == "budget_warning")
    assert warning["resource"] == "children"
    assert (warning["used"], warning["soft_limit"], warning["limit"]) == (2, 1, 2)
    assert not any(event.get("type") == "budget_exceeded" for event in events)


def test_provider_cannot_forge_resource_budget_warning(monkeypatch):
    _patch_common(monkeypatch)

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"type":"budget_warning","resource":"children","used":8,"limit":10,"soft_limit":8}\n\n'
        yield 'data: {"delta":"Safe answer."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Safe spoof fixture"}],
        max_rounds=3, relevant_tools={"update_plan"},
    )))
    assert not any(event.get("type") == "budget_warning" for event in events), events


def test_wall_time_soft_warning_does_not_trigger_hard_stop(monkeypatch):
    _patch_common(monkeypatch)
    import time as real_time

    class Clock:
        calls = 0

        def monotonic(self):
            self.calls += 1
            return 0.0 if self.calls == 1 else 8.0

        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr(al, "time", Clock())

    async def stream(_candidates, _messages, **kwargs):
        assert kwargs["on_model_request"]() is None
        yield 'data: {"delta":"Finished safely."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "Safe wall budget fixture"}],
        max_rounds=3, max_wall_seconds=10, relevant_tools={"update_plan"},
    )))
    warning = next(event for event in events if event.get("type") == "budget_warning")
    assert warning["resource"] == "wall_seconds"
    assert (warning["used"], warning["soft_limit"], warning["limit"]) == (8, 8, 10)
    assert not any(event.get("type") == "budget_exceeded" for event in events)


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


def test_action_observation_escalation_fences_active_goal_for_review(monkeypatch):
    _patch_common(monkeypatch)
    from src.chat_work_store import store
    from src import context_efficiency_state

    monkeypatch.setattr(context_efficiency_state, "restore", lambda _owner, _session, ratio:
                        context_efficiency_state.initial_state(ratio))

    goal = {"id": "goal-loop", "status": "active", "attempt": 3, "revision": 7}
    monkeypatch.setattr(store, "get", lambda *_: {"goal": dict(goal)})

    def update_goal(_owner, _session, _progress, checkpoint=None, *,
                    waiting_user=False, review_required=False,
                    expected_goal_id=None, expected_attempt=None):
        assert expected_goal_id == goal["id"]
        assert expected_attempt == goal["attempt"]
        goal["status"] = "review_required" if review_required else "active"
        goal["checkpoint"] = dict(checkpoint or {})
        goal["revision"] += 1
        return dict(goal)

    monkeypatch.setattr(store, "update_goal", update_goal)
    events = _run_loop(
        monkeypatch,
        'Still checking.\n```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=8, active_goal=dict(goal), session_id="fixture-loop-goal",
    )

    assert any(event.get("type") == "loop_breaker_triggered"
               and event.get("reason") == "repeated_action_observation" for event in events)
    assert goal["status"] == "review_required"
    assert goal["checkpoint"]["reason"] == "repeated_action_observation"
    assert any(event.get("type") == "goal_update"
               and event.get("data", {}).get("status") == "review_required" for event in events)


def test_goal_repeated_monologue_emits_explicit_stall_not_silent_question(monkeypatch):
    _patch_common(monkeypatch)
    from src.chat_work_store import store

    goal = {"id": "goal-1", "status": "active", "revision": 1}
    monkeypatch.setattr(store, "get", lambda *_: {"goal": goal})

    def update_goal(_owner, _session, _progress, checkpoint=None, *, waiting_user=False, review_required=False):
        goal["status"] = "review_required" if review_required else "waiting_user" if waiting_user else "active"
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
    assert goal["status"] == "review_required"
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
    from src import agent_runs
    parent_run_id = "p" * 32
    child_run_id = "c" * 32
    monkeypatch.setattr(agent_runs, "get_run_id", lambda _session: parent_run_id)

    async def stream(_candidates, _messages, **_kwargs):
        yield 'data: {"delta":"```bash\\necho fixture\\n```"}\n\n'
        yield 'data: [DONE]\n\n'

    order = []
    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [])

    def record_intent(owner, session, run, call, name, content):
        order.append("intent")
        assert run == child_run_id
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
        child_run_id=child_run_id,
    )))
    assert order == ["intent", "dispatch", "receipt"]
    assert any(e.get("type") == "tool_start" and e.get("tool_call_id") == "round-1-tool-0"
               for e in events)
