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


def test_live_dom_mutations_do_not_force_composer_layout_measurement():
    source = (ROOT / "static/js/init.js").read_text(encoding="utf-8")
    start = source.index("/* Keep minimized tool chips above the composer.")
    end = source.index("/* ---- Resizable sidebar", start)
    implementation = source[start:end]

    assert "ResizeObserver(_syncComposerClearance)" in implementation
    assert "window.addEventListener('resize', _syncComposerClearance)" in implementation
    assert "new MutationObserver(_syncComposerClearance)" not in implementation


def test_live_history_mutation_observer_defers_scroll_layout_and_never_reanchors_composer():
    source = (ROOT / "static/index.html").read_text(encoding="utf-8")
    start = source.index("const container = document.getElementById('chat-history');")
    end = source.index("</script>", start)
    implementation = source[start:end]
    update = implementation[implementation.index("function update()"):
                            implementation.index("let _updateRaf")]

    assert "new MutationObserver(scheduleUpdate)" in implementation
    assert "requestAnimationFrame(() =>" in implementation
    assert "reposition();" not in update
    assert "new MutationObserver(update)" not in implementation
    assert "geometryObserver.observe(attachStrip)" in implementation
    assert "geometryObserver.observe(workStatusRow)" in implementation


def test_attribute_observers_defer_geometry_reads_until_animation_frame():
    init = (ROOT / "static/js/init.js").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    snap = (ROOT / "static/js/modalSnap.js").read_text(encoding="utf-8")

    assert "new MutationObserver(_scheduleSync)" in init
    assert "new MutationObserver(_sync).observe(sidebar" not in init
    assert "new MutationObserver(_sync).observe(rail" not in init
    assert "new MutationObserver(scheduleDockOffset)" in app
    assert "new MutationObserver(updateDockOffset)" not in app
    assert "new MutationObserver(schedulePosition).observe(document.documentElement" in snap
    assert "new MutationObserver(_positionEdgeDockResizeHandles).observe" not in snap
    assert "new MutationObserver(scheduleSplitPosition).observe(document.documentElement" in snap
    assert "new MutationObserver(_position).observe" not in snap
    assert "const navObs = new MutationObserver(scheduleReanchor)" in snap


def test_plan_popover_reflows_subagents_button_instead_of_covering_it():
    work = (ROOT / "static/js/chat-work.js").read_text(encoding="utf-8")
    subagents = (ROOT / "static/js/chat-subagents.js").read_text(encoding="utf-8")

    assert "function placeSubagentsBelowPlan" in work
    assert "plan.getBoundingClientRect().bottom" in work
    assert "subagents.style.top" in work
    assert "placeSubagentsBelowPlan(node, open)" in work
    assert "card.style.removeProperty('top')" in subagents


def test_explicit_thinking_collapse_survives_live_to_history_identity_change():
    markdown = (ROOT / "static/js/markdown.js").read_text(encoding="utf-8")

    assert "const THINK_COLLAPSED_KEY = 'odysseus-thinking-collapsed'" in markdown
    assert "function _thinkingPersistenceKeys(content)" in markdown
    assert "[stable, contentHash].filter(Boolean)" in markdown
    assert "collapsed.add(key)" in markdown
    assert "keys.some(key => collapsed.has(key))" in markdown
    assert "_setThinkingExpanded(content, toggle, header, false)" in markdown


def test_subagent_live_updates_preserve_buttons_and_hidden_detail_stays_idle():
    source = (ROOT / "static/js/chat-subagents.js").read_text(encoding="utf-8")
    render = source.split("function render()", 1)[1].split("async function showDetail", 1)[0]
    assert "replaceChildren" not in render
    assert "const existing = new Map(" in render
    assert "existing.get(childId)" in render
    assert "if (!item.isConnected || reorder) list.appendChild(item);" in render
    live = source.split("source.onmessage = event =>", 1)[1].split("source.onerror", 1)[0]
    assert "listRefreshKinds.has" in live
    assert "!detail.hidden" in live
    assert "card?.classList.contains('expanded')" in live
    assert "detailTimer = setTimeout" in live
    refresh = source.split("async function refreshKeepStream", 1)[1].split("function bind()", 1)[0]
    assert "showDetail" not in refresh


def test_goal_plan_refresh_fences_late_responses_from_old_sessions():
    source = (ROOT / "static/js/chat-work.js").read_text(encoding="utf-8")
    refresh = source.split("async function refresh", 1)[1].split("function handleEvent", 1)[0]
    assert "const myGeneration = ++refreshGeneration" in refresh
    assert "myGeneration !== refreshGeneration || sessionId !== targetSession" in refresh
    assert "closeEventStream();" in refresh
    assert "snapshot = { plan: null, goal: null, cursor: 0 };" in refresh


def test_subagent_detail_is_generation_fenced_incremental_and_bounded():
    source = (ROOT / "static/js/chat-subagents.js").read_text(encoding="utf-8")
    detail = source.split("async function showDetail", 1)[1].split("async function refresh", 1)[0]
    assert "const myGeneration = ++detailGeneration" in detail
    assert "sessionId !== expectedSession || selectedId !== childId" in detail
    assert "after=${detailCursor}" in detail
    assert "reset ? 1000 : 200" in detail
    assert "if (output.textContent !== detailText)" in detail
    assert "detailText.length > MAX_DETAIL_CHARS" in detail


def test_pending_resume_exposes_stop_before_response_headers_arrive():
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    foreground = source.split("function _getForegroundStreamState", 1)[1].split(
        "function _syncForegroundStreamGlobals", 1,
    )[0]
    resume = source.split("export async function resumeStream", 1)[1].split(
        "export function cancelResumedStream", 1,
    )[0]
    cancel = source.split("export function cancelResumedStream", 1)[1].split(
        "export async function checkBackgroundStreams", 1,
    )[0]
    assert "_activeStreams.get(sid) || _resumingStreams.get(sid)" in foreground
    assert "_resumingStreams.set(sessionId, subscription);\n    // A detached run" in resume
    assert "updateSubmitButton('streaming', pendingResumeSubmitBtn)" in resume
    assert "_resumingStreams.delete(sessionId);\n      _syncForegroundStreamGlobals();" in resume
    assert "_resumingStreams.delete(sessionId);\n    _syncForegroundStreamGlobals();" in cancel


def test_context_popup_does_not_mislabel_active_run_as_manual_compaction():
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    assert "rows.push(['Run status', 'Active'])" in source
    assert "rows.push(['Manual compact', 'Run active'])" not in source


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
    assert "theme.js?v=20260921livefix20" in (ROOT / "static/sw.js").read_text(encoding="utf-8")


def test_synapse_uses_compositor_only_transforms_instead_of_canvas_repaint():
    source = (ROOT / "static/js/theme.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "synapse-canvas" not in source
    assert "id = 'synapse-layer'" in source
    assert "animation-name: synapse-pulse-x" in styles
    assert "animation-name: synapse-pulse-y" in styles
    assert "translate3d" in styles
    assert "const MAX_PULSES = 8" in source
    assert "animation-timing-function: linear" in styles
    assert "steps(var(--synapse-steps" not in styles


def test_stateful_chat_modules_have_one_browser_identity():
    """Different query strings instantiate duplicate ES modules and listeners."""
    expected = {"chat": "20260924compactpreview1", "sessions": "20260923countrev1",
                "models": "20260922approval1", "chatRenderer": "20260924actionpreview1"}
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
        assert all(version == expected[name] for _, version in refs), refs

    # The outer entrypoint must move with stateful module revisions. Otherwise
    # Safari can reuse an older app.js which imports a second, older sessions.js
    # identity even though index.html also preloads the new one.
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    worker = (ROOT / "static/sw.js").read_text(encoding="utf-8")
    init_versions = set(re.findall(r"/static/js/init\.js\?v=([A-Za-z0-9_-]+)", index))
    assert len(init_versions) == 1, "init preload/script must not create duplicate module identities"
    assert f"/static/js/init.js?v={init_versions.pop()}" in worker
    app_versions = set(re.findall(r"/static/app\.js\?v=([A-Za-z0-9_-]+)", index))
    assert len(app_versions) == 1, "preload and script must use the same app.js URL"
    assert f"/static/app.js?v={app_versions.pop()}" in worker


def test_manual_compaction_preflight_is_presented_as_unchanged_not_success_or_error():
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    assert "data.reason !== 'native_not_compactable'" in chat
    assert "{ status: 'unchanged', reason: String(data.reason || '') }" in chat
    assert "result.reason === 'native_not_compactable' ? 'No safe cut' : 'Unchanged'" in chat
