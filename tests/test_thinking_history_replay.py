"""Regression guards for non-empty thinking blocks after reload/reconnect."""
from pathlib import Path
import json
import subprocess


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
    finish = source.split("const finishReplayThinking = ", 1)[1].split(
        "const ensureReplayThread", 1
    )[0]
    assert "timelineReducer.thinkingForSegment?.(replayThinkingSegmentId)" in finish
    assert "!String(inner.textContent || '').trim()" in finish
    assert "String(inner?.textContent || '').trim()" in finish
    assert "replayThinkingFinalized" in finish
    assert "markdownModule.mdToHtml(thinkingText)" in finish


def test_empty_history_thinking_is_lazy_loaded_from_durable_run():
    source = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    assert "function bindLazyHistoryThinking" in source
    assert "/reasoning/${encodeURIComponent(runId)}/${roundNumber}" in source
    assert "bindLazyHistoryThinking(body, metadata, roundNum" in source
    assert "stats.className = 'thinking-stats'" in source
    assert "stats.textContent = `${Number(data.duration || 0).toFixed(1)}s" in source
    assert "bindLazyHistoryThinking(b, metadata, 1, storedThinking)" in source


def test_explicit_empty_reasoning_round_does_not_create_false_thinking_card():
    source = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    body = source.split("export function shouldBindLazyHistoryThinking", 1)[1].split(
        "function bindLazyHistoryThinking", 1,
    )[0]
    function_source = "function shouldBindLazyHistoryThinking" + body
    script = """
      const canLazyHistoryThinking = m => /^[0-9a-f]{32}$/.test(m?.timeline_v2?.run_id || '');
    """ + function_source + """
      const base = {timeline_v2:{run_id:'a'.repeat(32)},round_reasonings:['real thinking','','']};
      console.log(JSON.stringify([
        shouldBindLazyHistoryThinking(base,1,'real thinking'),
        shouldBindLazyHistoryThinking(base,2,''),
        shouldBindLazyHistoryThinking(base,3,''),
        shouldBindLazyHistoryThinking({timeline_v2:base.timeline_v2},2,''),
      ]));
    """
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [True, False, False, True]


def test_reload_does_not_eagerly_fetch_every_saved_thinking_card():
    renderer = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    markdown = (ROOT / "static/js/markdown.js").read_text(encoding="utf-8")
    lazy = renderer.split("function bindLazyHistoryThinking", 1)[1].split(
        "// Older saved assistant rows", 1,
    )[0]
    assert "requestAnimationFrame" not in lazy
    assert "header.addEventListener('click'" in lazy
    assert "if (sec.dataset.lazyThinking === 'true') continue;" in markdown


def test_preserved_history_thinking_is_not_preparsed_into_hidden_dom():
    source = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    lazy = source.split("function bindLazyHistoryThinking", 1)[1].split(
        "// Older saved assistant rows", 1
    )[0]
    render = source.split("const preservedReasoning = reasoning || embeddedThinking", 1)[1].split(
        "if (txt || reasoning || embeddedThinking)", 1
    )[0]

    assert "inner.textContent = t('Thinking saved — open to load')" in lazy
    assert "String(data.thinking || '').trim() || fallback" in lazy
    assert "markdownModule.mdToHtml(fallback)" in lazy
    assert "const lazyReasoning = Boolean(preservedReasoning)" in render
    assert "const renderSource = lazyReasoning\n          ? txt" in render


def test_history_reducer_appends_every_timeline_delta_for_missing_round():
    source = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    body = source.split("export function historyRoundReasonings", 1)[1].split("const SEARCH_ICON", 1)[0]
    function_source = "function historyRoundReasonings" + body
    metadata = {"timeline_v2": {"events": [
        {"data": {"delta": "first ", "thinking": True, "round": 1}},
        {"data": {"delta": "second", "thinking": True, "round": 1}},
    ]}}
    script = function_source + "\nconsole.log(JSON.stringify(historyRoundReasonings(" + json.dumps(metadata) + ",1)));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["first second"]
