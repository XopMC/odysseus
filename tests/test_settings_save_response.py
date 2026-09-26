from pathlib import Path

from src.settings import DEFAULT_SETTINGS


def test_settings_post_rejects_non_success_http_status():
    source = (Path(__file__).resolve().parents[1] / "static/js/settings.js").read_text()
    helper = source.split("async function _postSettings(body) {", 1)[1].split("\nconst el =", 1)[0]
    assert "if (!response.ok) throw new Error" in helper
    assert "return response;" in helper
    assert "invalidateSettings();" in helper


def test_agent_settings_restore_server_policy_after_rejected_write():
    source = (Path(__file__).resolve().parents[1] / "static/js/settings.js").read_text()
    agent_settings = source.split("// A rejected admin/CSRF write", 1)[1]
    assert "currentResponse.ok" in agent_settings
    assert "setSelectedModels(current.agent_subagent_models || '')" in agent_settings
    assert "renderSubagentModels();" in agent_settings


def test_agent_output_budget_setting_is_wired_end_to_end():
    root = Path(__file__).resolve().parents[1]
    markup = (root / "static/index.html").read_text(encoding="utf-8")
    frontend = (root / "static/js/settings.js").read_text(encoding="utf-8")
    backend = (root / "routes/auth_routes.py").read_text(encoding="utf-8")

    assert DEFAULT_SETTINGS["agent_output_token_budget"] == 32768
    assert 'id="set-agentOutputTokens"' in markup
    assert 'for="set-agentOutputTokens"' in markup
    assert '"agent_output_token_budget": (4096, 131072)' in backend
    assert "settings.agent_output_token_budget ?? 32768" in frontend
    assert "payload.agent_output_token_budget = outputTokens" in frontend
    assert "outputTokensInput.addEventListener('change', save)" in frontend
    assert "current.agent_output_token_budget ?? 32768" in frontend
