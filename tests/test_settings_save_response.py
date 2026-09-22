from pathlib import Path


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
