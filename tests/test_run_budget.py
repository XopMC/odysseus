from src.run_budget import soft_budget_warning


def test_soft_budget_warning_is_content_free_and_precedes_hard_cap():
    assert soft_budget_warning("model_requests", 0, 2) is None
    assert soft_budget_warning("model_requests", 1, 2) == {
        "type": "budget_warning", "resource": "model_requests",
        "used": 1, "limit": 2, "soft_limit": 1,
    }
    assert soft_budget_warning("model_tokens", 799, 1000) is None
    assert soft_budget_warning("model_tokens", 800, 1000) == {
        "type": "budget_warning", "resource": "model_tokens",
        "used": 800, "limit": 1000, "soft_limit": 800,
    }
    assert soft_budget_warning("model_tokens", 1000, 1000)["used"] == 1000


def test_soft_budget_warning_rejects_unlimited_malformed_and_unscoped_resources():
    assert soft_budget_warning("tool_calls", 100, 0) is None
    assert soft_budget_warning("tool_calls", -1, 10) is None
    assert soft_budget_warning("tool_calls", True, 10) is None
    assert soft_budget_warning("process_rss", 900, 1000) is None
    assert soft_budget_warning(["model_tokens"], 900, 1000) is None
