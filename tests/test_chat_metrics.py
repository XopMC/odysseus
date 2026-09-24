"""Backend-reported generation/prefill speed metrics.

llama.cpp emits a `timings` block alongside `usage` on the final stream chunk
with the TRUE decode speed (predicted_per_second) and prompt speed
(prompt_per_second). These are pure-phase numbers; the old per-message t/s was
output_tokens / wall-clock, which includes prefill + tool + network time and so
reads low (and sags as the prompt grows).

These tests lock in two things:
  1. stream_llm passes the llama.cpp `timings` through on the usage event as
     gen_tps / prefill_tps (captured-stream fixture), and omits them when the
     backend doesn't report timings (e.g. cloud APIs).
  2. _compute_final_metrics prefers the backend gen speed over wall-clock when
     present, tags tps_source accordingly, and surfaces prefill_tps.
"""
import json
import asyncio

from src import llm_core
from src.agent_loop import _compute_final_metrics


# --- captured-stream harness (mirrors test_llm_core_streaming.py) -----------

class _FakeResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def _usage_event(monkeypatch, lines):
    """Drive stream_llm against canned SSE lines; return the usage event data."""
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        usage = None
        async for chunk in llm_core.stream_llm(
            "http://127.0.0.1:8081/v1/chat/completions",
            "qwen-local",
            [{"role": "user", "content": "hi"}],
        ):
            for ln in chunk.split("\n"):
                ln = ln.strip()
                if ln.startswith("data: ") and ln[6:] != "[DONE]":
                    try:
                        ev = json.loads(ln[6:])
                    except ValueError:
                        continue
                    if ev.get("type") == "usage":
                        usage = ev["data"]
        return usage

    return asyncio.run(run())


def _stream_events(monkeypatch, lines):
    """Drive stream_llm and return all JSON data events."""
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        events = []
        async for chunk in llm_core.stream_llm(
            "http://127.0.0.1:8081/v1/chat/completions",
            "openrouter/auto",
            [{"role": "user", "content": "hi"}],
        ):
            for ln in chunk.split("\n"):
                ln = ln.strip()
                if ln.startswith("data: ") and ln[6:] != "[DONE]":
                    try:
                        events.append(json.loads(ln[6:]))
                    except ValueError:
                        pass
        return events

    return asyncio.run(run())


# A real llama.cpp final chunk carries `usage` (delta empty / choices []) with a
# sibling `timings` block. The decode speed here (78.91) is far above the
# wall-clock figure the old code would have shown.
_LLAMACPP_TIMINGS_STREAM = [
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": "Hi there"}}]}),
    'data: ' + json.dumps({
        "choices": [],
        "object": "chat.completion.chunk",
        "usage": {"prompt_tokens": 15, "completion_tokens": 42},
        "timings": {
            "prompt_n": 15, "prompt_per_second": 512.34,
            "predicted_n": 42, "predicted_per_second": 78.91,
        },
    }),
    "data: [DONE]",
]


def test_stream_llm_passes_through_llamacpp_timings(monkeypatch):
    usage = _usage_event(monkeypatch, _LLAMACPP_TIMINGS_STREAM)
    assert usage is not None, "no usage event was emitted"
    assert usage["input_tokens"] == 15
    assert usage["output_tokens"] == 42
    # The timings block is surfaced as gen_tps / prefill_tps (rounded to 2dp).
    assert usage["gen_tps"] == 78.91
    assert usage["prefill_tps"] == 512.34


def test_stream_llm_omits_tps_when_backend_has_no_timings(monkeypatch):
    # A backend (e.g. a cloud API) that reports usage but no `timings` block must
    # not invent gen_tps/prefill_tps — the caller then falls back to wall-clock.
    no_timings = [
        'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": "Hi"}}]}),
        'data: ' + json.dumps({
            "choices": [],
            "usage": {"prompt_tokens": 8, "completion_tokens": 5},
        }),
        "data: [DONE]",
    ]
    usage = _usage_event(monkeypatch, no_timings)
    assert usage is not None
    assert "gen_tps" not in usage
    assert "prefill_tps" not in usage


def test_stream_llm_reads_lm_studio_stats_from_usage_frame(monkeypatch):
    events = _stream_events(monkeypatch, [
        'data: ' + json.dumps({
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 120},
            "stats": {"tokens_per_second": 123.45},
        }),
        'data: [DONE]',
    ])
    usage = next(e["data"] for e in events if e.get("type") == "usage")
    assert usage["gen_tps"] == 123.45


def test_stream_llm_keeps_lm_studio_timings_when_usage_shares_final_delta(monkeypatch):
    # LM Studio can attach final usage/stats to the same OpenAI-compatible
    # frame that contains the final content delta. Both must survive: treating
    # these as mutually exclusive drops the backend's true decode speed, after
    # which the UI divides by end-to-end stream time and can show ~20 instead
    # of the server's 120+ tokens/s.
    events = _stream_events(monkeypatch, [
        'data: ' + json.dumps({
            "choices": [{"index": 0, "delta": {"content": "Final answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 120},
            "stats": {"tokens_per_second": 123.45},
        }),
        'data: [DONE]',
    ])
    usage_events = [event["data"] for event in events if event.get("type") == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["output_tokens"] == 120
    assert usage_events[0]["gen_tps"] == 123.45
    assert any(event.get("delta") == "Final answer" for event in events)


def test_stream_llm_keeps_lm_studio_stats_when_stats_arrive_after_usage(monkeypatch):
    # Some OpenAI-compatible streams send usage and backend stats as separate
    # terminal frames. Neither frame is guaranteed to contain the other.
    events = _stream_events(monkeypatch, [
        'data: ' + json.dumps({
            "choices": [{"index": 0, "delta": {"content": "Final answer"}}],
        }),
        'data: ' + json.dumps({
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 120},
        }),
        'data: ' + json.dumps({
            "choices": [],
            "stats": {"tokens_per_second": 123.45, "generation_time": 0.97},
        }),
        'data: [DONE]',
    ])
    usage = next(e["data"] for e in events if e.get("type") == "usage")
    backend = next(e["data"] for e in events if e.get("type") == "backend_metrics")
    assert usage["output_tokens"] == 120
    assert backend["gen_tps"] == 123.45
    assert backend["generation_time"] == 0.97


def test_stream_llm_reads_lm_studio_stats_on_final_delta_without_usage(monkeypatch):
    events = _stream_events(monkeypatch, [
        'data: ' + json.dumps({
            "choices": [{"index": 0, "delta": {"content": "Answer"}, "finish_reason": "stop"}],
            "stats": {"tokens_per_second": 121.8},
        }),
        'data: [DONE]',
    ])
    backend = next(e["data"] for e in events if e.get("type") == "backend_metrics")
    assert backend["gen_tps"] == 121.8


def test_stream_llm_surfaces_provider_resolved_model(monkeypatch):
    events = _stream_events(monkeypatch, [
        'data: ' + json.dumps({
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "choices": [{"index": 0, "delta": {"content": "Hi"}}],
        }),
        'data: ' + json.dumps({
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "choices": [],
            "usage": {"prompt_tokens": 8, "completion_tokens": 5},
        }),
        "data: [DONE]",
    ])

    actual = [e for e in events if e.get("type") == "model_actual"]
    assert actual == [{
        "type": "model_actual",
        "requested_model": "openrouter/auto",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
    }]
    usage = [e["data"] for e in events if e.get("type") == "usage"][0]
    assert usage["requested_model"] == "openrouter/auto"
    assert usage["model"] == "meta-llama/llama-3.3-70b-instruct:free"


# --- _compute_final_metrics preference logic --------------------------------

def _metrics(**overrides):
    kwargs = dict(
        messages=[{"role": "user", "content": "hi"}],
        full_response="hello world",
        total_duration=10.0,           # wall-clock: 42/10 = 4.2 t/s (reads low)
        time_to_first_token=0.5,
        context_length=4096,
        real_input_tokens=15,
        real_output_tokens=42,
        has_real_usage=True,
        tool_events=[],
        round_texts=[],
        model="qwen-local",
    )
    kwargs.update(overrides)
    return _compute_final_metrics(**kwargs)


def test_metrics_prefer_backend_gen_tps_over_wallclock():
    m = _metrics(backend_gen_tps=78.91, backend_prefill_tps=512.34)
    # Uses the backend's true decode speed, NOT 42/10 = 4.2.
    assert m["tokens_per_second"] == 78.91
    assert m["tps_source"] == "backend"
    assert m["prefill_tps"] == 512.34


def test_metrics_fall_back_to_wallclock_without_backend_timings():
    m = _metrics(backend_gen_tps=0, backend_prefill_tps=0)
    # 42 output tokens / 10s wall-clock.
    assert m["tokens_per_second"] == 4.2
    assert m["tps_source"] == "computed"
    assert "prefill_tps" not in m


def test_metrics_use_model_stream_time_not_total_agent_wall_time():
    m = _metrics(
        total_duration=36.73,
        model_stream_duration=8.4,
        real_output_tokens=1034,
    )
    assert m["tokens_per_second"] == 123.1
    assert m["tps_source"] == "stream_elapsed"
