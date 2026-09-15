"""The browser must open a bounded transcript and paginate older messages."""

from pathlib import Path


SESSIONS_JS = Path(__file__).parents[1] / "static" / "js" / "sessions.js"


def test_initial_history_window_is_exactly_fifty_messages():
    source = SESSIONS_JS.read_text(encoding="utf-8")

    assert "const HISTORY_PAGE_LIMIT = 50;" in source
    assert "return HISTORY_PAGE_LIMIT;" in source
    assert "HISTORY_PAGE_LIMIT_MOBILE" not in source
    assert "HISTORY_PAGE_LIMIT_DESKTOP" not in source


def test_older_history_is_requested_only_from_top_scroll_pager():
    source = SESSIONS_JS.read_text(encoding="utf-8")

    assert "if (box.scrollTop > 90) return;" in source
    assert "_historyPager.offset - _historyPager.limit" in source
    assert "box.addEventListener('scroll', _historyPager.handler" in source
