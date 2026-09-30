import asyncio

import pytest

from src import context_compaction_retry as retry
from src import agent_context


def test_five_retries_and_ten_minute_timeout_each(monkeypatch):
    calls = 0
    timeouts = []
    original_wait_for = retry.asyncio.wait_for

    async def instant_pause(_index):
        return None

    async def measured_wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout)

    async def failing():
        nonlocal calls
        calls += 1
        raise ConnectionError("upstream unavailable")

    monkeypatch.setattr(retry, "_retry_pause", instant_pause)
    monkeypatch.setattr(retry.asyncio, "wait_for", measured_wait_for)
    with pytest.raises(ConnectionError):
        asyncio.run(retry.summarize_with_retries(
            failing, normalize=str, valid=bool,
        ))
    assert calls == 6
    assert timeouts == [600] * 6


def test_automatic_compaction_retries_invalid_summary_then_succeeds(monkeypatch):
    async def instant_pause(_index):
        return None

    monkeypatch.setattr(retry, "_retry_pause", instant_pause)
    messages = [{"role": "user", "content": "Exact goal"}]
    messages += [{"role": "assistant", "content": "verified work " * 700}
                 for _ in range(8)]
    calls = 0

    async def summarize(_prompt):
        nonlocal calls
        calls += 1
        return "" if calls < 3 else "Verified work and next step."

    shaped, status = asyncio.run(agent_context.compact_working_context(
        messages, 5000, summarize,
    ))
    assert status == "compacted"
    assert calls == 3
    assert any(item.get("content") == "Exact goal" for item in shaped)
    assert len(shaped) < len(messages)


def test_five_retries_of_invalid_summaries_never_replace_previous_ledger(monkeypatch):
    async def instant_pause(_index):
        return None

    monkeypatch.setattr(retry, "_retry_pause", instant_pause)
    messages = [{"role": "user", "content": "Exact goal"}]
    messages += [{"role": "assistant", "content": "verified work " * 700}
                 for _ in range(8)]
    calls = 0

    async def summarize(_prompt):
        nonlocal calls
        calls += 1
        return ""

    shaped, status = asyncio.run(agent_context.compact_working_context(
        messages, 5000, summarize,
    ))
    assert status == "failed"
    assert calls == 6
    assert shaped is messages


def test_overlong_manual_summary_is_explicitly_bounded_without_erasing_source():
    from src.model_context import estimate_tokens
    original = "first verified fact " * 1500 + "latest next step " * 1500
    fitted = retry.bound_checkpoint_summary(original, max_tokens=1200)
    assert fitted.startswith("first verified fact")
    assert fitted.rstrip().endswith("latest next step")
    assert "middle omitted" in fitted
    assert estimate_tokens([{"role": "assistant", "content": fitted}]) <= 1200
    assert len(original) > len(fitted)
