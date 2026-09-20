from src.context_compaction_economics import decide


def test_waits_for_boundary_below_emergency():
    d = decide(at_boundary=False, used_tokens=70_000, input_budget=100_000,
               completed_boundaries=4, tokens_since_boundary=20_000)
    assert not d.compact and d.reason == "awaiting_plan_boundary"


def test_compacts_when_boundary_savings_pay_for_rewrite():
    d = decide(at_boundary=True, used_tokens=70_000, input_budget=100_000,
               completed_boundaries=4, tokens_since_boundary=20_000)
    assert d.compact and d.reason == "economic_plan_boundary"
    assert d.estimated_savings > d.rewrite_cost


def test_window_protection_does_not_wait_for_boundary():
    d = decide(at_boundary=False, used_tokens=93_000, input_budget=100_000,
               completed_boundaries=0, tokens_since_boundary=0)
    assert d.compact and d.reason == "window_protection"


def test_cache_write_read_ratio_changes_economic_decision():
    cheap = decide(at_boundary=True, used_tokens=70_000, input_budget=100_000,
                   completed_boundaries=4, tokens_since_boundary=20_000,
                   cache_write_read_ratio=1)
    expensive = decide(at_boundary=True, used_tokens=70_000, input_budget=100_000,
                       completed_boundaries=4, tokens_since_boundary=20_000,
                       cache_write_read_ratio=100)
    assert cheap.compact is True
    assert expensive.compact is False
    assert cheap.cache_write_read_ratio == 1
