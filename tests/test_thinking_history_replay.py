"""Regression guards for non-empty thinking blocks after reload/reconnect."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_history_reasons_fill_partial_round_reasonings_from_timeline():
    source = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    function = source.split("export function historyRoundReasonings", 1)[1].split(
        "const SEARCH_ICON", 1
    )[0]
    assert "while (values.length < roundCount) values.push('')" in function
    assert "if (!String(values[round - 1] || '').trim())" in function
    assert "JSON.parse(payload)" in function
    assert "values[0] = String(metadata.thinking)" in function


def test_replay_flush_recovers_thinking_from_shared_timeline_reducer():
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    finish = source.split("const finishReplayThinking = () =>", 1)[1].split(
        "const ensureReplayThread", 1
    )[0]
    assert "timelineReducer.snapshot?.().segments" in finish
    assert "!String(inner.textContent || '').trim()" in finish
    assert "markdownModule.mdToHtml(thinkingText)" in finish


def test_empty_history_thinking_is_lazy_loaded_from_durable_run():
    source = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    assert "function bindLazyHistoryThinking" in source
    assert "/reasoning/${encodeURIComponent(runId)}/${roundNumber}" in source
    assert "bindLazyHistoryThinking(body, metadata, roundNum" in source
