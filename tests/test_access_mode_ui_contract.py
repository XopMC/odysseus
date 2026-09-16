from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_access_mode_control_is_mode_independent_and_has_three_choices():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    module = (ROOT / "static/js/accessMode.js").read_text(encoding="utf-8")
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    work = (ROOT / "static/js/chat-work.js").read_text(encoding="utf-8")
    team = (ROOT / "static/js/team-workspace.js").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert 'id="access-mode-btn"' in html
    assert 'id="access-mode-menu"' in html
    for mode in ("ask_every_time", "ask_important", "full_access"):
        assert f'data-access-mode="{mode}"' in html
        assert mode in module
    assert "accessModeModule?.getMode?.()" in chat
    assert "fd.append('access_mode', choicesForSend.accessMode)" in chat
    assert "fd.append('access_mode'" in work
    assert "access_mode: window.accessModeModule?.getMode?.()" in team
    assert "accessModeModule from './js/accessMode.js" in app
    assert "document.body.appendChild(menu)" in module
    assert "access-mode-menu-portal" in module
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")
    assert ".access-mode-option {" in css
    assert "min-height: 76px;" in css
    assert "grid-template-columns: 13px minmax(0, 1fr);" in css
