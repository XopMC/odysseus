"""Real loop with deterministic transport/tools; no live DB/network mutations."""
import asyncio
import json

import src.agent_loop as al
import src.llm_core as llm
from src.model_context import estimate_tokens


def run(monkeypatch, *, failures=False, initial_messages=None):
    # This fixture represents an already approved research task. The separate
    # external-context gate suite exercises approval; never disable it in app.
    context_type = al.ToolRunSecurityContext
    def approved_context(**kwargs):
        kwargs["approval_gate_bypassed"] = True
        return context_type(**kwargs)
    monkeypatch.setattr(al, "ToolRunSecurityContext", approved_context)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(al, "estimate_tokens", estimate_tokens)
    calls, requests, summaries = [], [], []
    async def execute(block, **kwargs):
        calls.append(block.content)
        if failures:
            return block.tool_type, {"error": "HTTP 404", "exit_code": 1}
        return block.tool_type, {"output": ("Verified evidence " * 1000), "exit_code": 0}
    monkeypatch.setattr(al, "execute_tool_block", execute)
    async def summary(*args, **kwargs):
        summaries.append(args[2])
        return "Goal: audit only. Already inspected numbered sources. Pending: finish comparison."
    monkeypatch.setattr(llm, "llm_call_async", summary)
    async def stream(candidates, messages, **kwargs):
        route = await kwargs["candidate_request_factory"](0, *candidates[0])
        requests.append(route["messages"])
        n = len(requests)
        if (failures and n >= 5) or (not failures and n >= 9):
            response = "Finished checking available evidence; unresolved items are not claimed verified."
        else:
            url = "https://example.org/missing" if failures else f"https://example.org/{n}"
            response = f'Checking source {n}.\n```web_fetch\n' + json.dumps({"url": url}) + '\n```'
        yield 'data: ' + json.dumps({"delta": response}) + '\n\n'
        yield 'data: ' + json.dumps({"type": "usage", "data": {
            "input_tokens": estimate_tokens(route["messages"]), "output_tokens": 50}}) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    async def collect():
        return [json.loads(c[6:]) async for c in al.stream_agent_loop(
            "http://test/v1/chat/completions", "test-model",
            initial_messages or [{"role": "user", "content": "Audit only: compare public sources; do not modify any contracts."}],
            context_length=20000, max_tokens=512, max_rounds=12,
            relevant_tools={"web_fetch"}, disabled_tools={"bash", "python"},
        ) if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    events = asyncio.run(collect())
    return events, calls, requests, summaries


def test_midrun_compaction_continues_with_goal_and_final_metrics(monkeypatch):
    events, calls, requests, summaries = run(monkeypatch)
    checkpoints = [event for event in events if event.get("type") == "context_checkpoint"]
    assert checkpoints
    assert any("Verified evidence" in str(item.get("content")) for item in checkpoints[-1]["messages"])
    compacted = next(e for e in events if e.get("type") == "compacted" and e.get("working_context"))
    assert compacted["checkpoint"]["summary"]
    assert len(compacted["checkpoint"]["ledger_hash"]) == 64
    assert summaries
    assert len(calls) == 8
    assert len(requests) == 9
    assert all(any("do not modify" in str(m.get("content", "")) for m in msgs) for msgs in requests), [(len(msgs), estimate_tokens(msgs), [str(m.get("content", ""))[:80] for m in msgs if m.get("role") == "user" and (m.get("metadata") or {}).get("trusted") is not False]) for msgs in requests]
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["working_context"]["compactions"] >= 1
    assert metrics["working_context"]["auto_compact_enabled"] is True
    assert metrics["working_context"]["prompt_tokens"] == estimate_tokens(requests[-1])


def test_identical_failed_web_request_is_executed_only_twice_despite_prose(monkeypatch):
    events, calls, requests, _ = run(monkeypatch, failures=True)
    assert len(calls) == 2, [(e.get("type"), str(e.get("delta"))[:200], e.get("reason"), e.get("message")) for e in events]
    assert sum(e.get("type") == "tool_retry_blocked" for e in events) == 2
    assert len(requests) == 5
    assert any(e.get("type") == "metrics" for e in events)


def test_restarted_agent_receives_persisted_tool_ledger(monkeypatch):
    prior_output = "deployment probe: release e3750b1 is healthy"
    prior = {
        "role": "assistant",
        "content": "I checked the deployment.",
        "metadata": {
            "tool_events": [{
                "round": 1,
                "tool": "bash",
                "command": '{"cmd":"healthcheck"}',
                "output": prior_output,
                "exit_code": 0,
            }],
        },
    }
    _events, _calls, requests, _summaries = run(monkeypatch, initial_messages=[
        {"role": "user", "content": "Keep working until verified."},
        prior,
        {"role": "user", "content": "Continue the active Goal from its durable checkpoint."},
    ])

    first_request = json.dumps(requests[0], ensure_ascii=False)
    assert prior_output in first_request
    assert "durable agent tool ledger" in first_request
