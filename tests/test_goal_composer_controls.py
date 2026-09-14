"""Goal/Plan controls must remain discoverable without hiding the model route."""
from pathlib import Path


def test_goal_and_plan_are_in_composer_overflow_and_model_picker_stays_visible():
    root = Path(__file__).resolve().parents[1]
    html = (root / 'static/index.html').read_text()
    app = (root / 'static/app.js').read_text()
    chat = (root / 'static/js/chat.js').read_text()
    css = (root / 'static/style.css').read_text()
    assert 'id="plan-toggle-btn"' in html and 'id="goal-toggle-btn"' in html
    assert html.index('id="plan-toggle-btn"') < html.index('id="overflow-attach-btn"')
    assert "setGoalMode" in app and "goal_mode" in app
    assert "Execute the approved goal." in chat
    assert "modelPickerWrap.classList.remove('model-picker-autohide')" in app
    assert "pickerWrap.classList.remove('picker-auto-hidden')" in app
    assert 'body.plan-mode-active .chat-input-top > .model-picker-wrap { opacity: 1' in css
