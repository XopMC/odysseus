from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent


def test_live_and_replay_streams_share_coalesced_incremental_renderer():
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")

    assert "function _queueIncrementalStreamRender" in source
    assert source.count("_queueIncrementalStreamRender(") >= 4
    assert "delay: _adaptiveLiveRenderDelay" in source
    assert "contentDiv.innerHTML = markdownModule.mdToHtml(markdownModule.squashOutsideCode(dt))" not in source


def test_long_run_timers_and_offscreen_timeline_are_bounded():
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "const TOOL_ELAPSED_TICK_MS = 250" in source
    assert "_elapsedTicker = setInterval" in source
    assert "content-visibility: auto" in styles
    assert "#chat-history > .agent-thread:not(.streaming)" in styles


def test_canvas_theme_yields_frame_budget_to_active_agent_runs():
    source = (ROOT / "static/js/theme.js").read_text(encoding="utf-8")
    assert "window.__odysseusChatBusy || visibleMessages >= 500" in source
    assert "? 250" in source
    assert "window.setTimeout(() =>" in source
    assert source.count("_nextBgFrame(draw);") == 7  # one tail call per canvas effect
    assert "requestAnimationFrame(draw);" not in source
    assert "theme.js?v=20260921livefix4" in (ROOT / "static/sw.js").read_text(encoding="utf-8")


def test_stateful_chat_modules_have_one_browser_identity():
    """Different query strings instantiate duplicate ES modules and listeners."""
    expected = "20260921livefix4"
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
