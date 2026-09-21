from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent


def test_live_and_replay_streams_share_coalesced_incremental_renderer():
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")

    assert "function _queueIncrementalStreamRender" in source
    assert source.count("_queueIncrementalStreamRender(") >= 4
    assert "delay: _adaptiveLiveRenderDelay" in source
    assert "contentDiv.innerHTML = markdownModule.mdToHtml(markdownModule.squashOutsideCode(dt))" not in source
    assert "length >= 128000 ? 1200 : length >= 32000 ? 800 : length >= 8000 ? 400 : 200" in source
    assert "target.firstChild.appendData(delta)" in source


def test_long_run_timers_and_offscreen_timeline_are_bounded():
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "const TOOL_ELAPSED_TICK_MS = 250" in source
    assert "_elapsedTicker = setInterval" in source
    assert "content-visibility: auto" in styles
    assert "#chat-history > .agent-thread:not(.streaming)" in styles


def test_live_autoscroll_does_not_poll_layout_on_every_animation_frame():
    source = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")
    start = source.index("function _smoothScrollStep()")
    end = source.index("export function scrollHistoryInstant", start)
    implementation = source[start:end]

    assert implementation.count("scrollHeight") == 0
    assert "requestAnimationFrame(_smoothScrollStep)" not in implementation
    assert "box.scrollTop = 2147483647" in implementation


def test_streaming_turn_uses_containment_and_compositor_only_indicators():
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "#chat-history > .agent-thread.streaming" in styles
    assert "contain: layout style" in styles
    assert ".agent-thread.streaming .token-new" in styles
    activity = styles[styles.index("@keyframes thread-activity-dot"):]
    activity = activity[:activity.index(".agent-thread-node")]
    assert "top:" not in activity
    assert "transform: scale" in activity


def test_canvas_theme_keeps_30fps_with_bounded_pixel_and_allocation_cost():
    source = (ROOT / "static/js/theme.js").read_text(encoding="utf-8")
    assert "const interval = 1000 / 30" in source
    assert "visibleMessages" not in source
    assert "renderedRows" not in source
    assert "function _getBgCanvasDpr() { return 1; }" in source
    assert source.count("desynchronized: true") == 6
    assert source.count("createLinearGradient") == 0
    assert "refreshEmberSprite(color)" in source
    assert "window.setTimeout(() =>" in source
    assert source.count("_nextBgFrame(draw);") == 6  # one tail call per remaining canvas effect
    assert "requestAnimationFrame(draw);" not in source
    assert "theme.js?v=20260921livefix15" in (ROOT / "static/sw.js").read_text(encoding="utf-8")


def test_synapse_uses_compositor_only_transforms_instead_of_canvas_repaint():
    source = (ROOT / "static/js/theme.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "synapse-canvas" not in source
    assert "id = 'synapse-layer'" in source
    assert "animation-name: synapse-pulse-x" in styles
    assert "animation-name: synapse-pulse-y" in styles
    assert "translate3d" in styles


def test_stateful_chat_modules_have_one_browser_identity():
    """Different query strings instantiate duplicate ES modules and listeners."""
    expected = "20260921livefix15"
    roots = [ROOT / "static/index.html", *sorted((ROOT / "static").rglob("*.js"))]
    pattern = re.compile(
        r"(?:from\s+|import\(\s*|(?:src|href)=)\s*['\"]"
        r"[^'\"]*/(chat|sessions|models|chatRenderer)\.js"
        r"(?:\?v=([A-Za-z0-9_-]+))?['\"]"
    )
    references = {name: [] for name in ("chat", "sessions", "models", "chatRenderer")}
    for path in roots:
        for name, version in pattern.findall(path.read_text(encoding="utf-8")):
            # compare/models.js is a separate stateless helper, not the picker store.
            if path.parent.name == "compare" and name == "models":
                continue
            references[name].append((path, version))

    for name, refs in references.items():
        assert refs, f"no references found for {name}.js"
        assert all(version == expected for _, version in refs), refs
