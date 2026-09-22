"""Repeated evidence must not keep an Agent run alive indefinitely."""

from src.agent_loop_detector import LoopDetector


def test_same_action_and_observation_nudges_then_escalates():
    detector = LoopDetector()
    action = [("read_file", {"path": "notes.txt"}, {"output": "unchanged"})]
    assert detector.observe(action) is None
    assert detector.observe(action) is None
    assert detector.observe(action) == "nudge"
    assert detector.observe(action) is None
    assert detector.observe(action) == "escalate"


def test_changed_observation_is_progress():
    detector = LoopDetector()
    assert detector.observe([("read_file", {"path": "notes.txt"}, {"output": "old"})]) is None
    assert detector.observe([("read_file", {"path": "notes.txt"}, {"output": "old"})]) is None
    assert detector.observe([("read_file", {"path": "notes.txt"}, {"output": "new"})]) is None
    assert detector.observe([("read_file", {"path": "notes.txt"}, {"output": "new"})]) is None


def test_changed_evidence_resets_warning_budget():
    detector = LoopDetector()
    old = [("read_file", {"path": "notes.txt"}, {"output": "old"})]
    new = [("read_file", {"path": "notes.txt"}, {"output": "new"})]
    assert [detector.observe(old) for _ in range(3)] == [None, None, "nudge"]
    assert [detector.observe(new) for _ in range(3)] == [None, None, "nudge"]


def test_two_step_cycle_is_bounded():
    detector = LoopDetector()
    a = [("search_files", {"pattern": "a"}, {"error": "not_found"})]
    b = [("search_files", {"pattern": "b"}, {"error": "not_found"})]
    assert [detector.observe(batch) for batch in (a, b, a, b, a, b, a, b)] == [
        None, None, None, None, "nudge", None, None, "escalate",
    ]


def test_distinct_actions_and_observations_do_not_trigger():
    detector = LoopDetector()
    for index in range(20):
        assert detector.observe([("read_file", {"path": str(index)}, {"output": str(index)})]) is None


def test_action_timestamp_is_not_discarded():
    detector = LoopDetector()
    for timestamp in range(10):
        assert detector.observe([("query", {"timestamp": timestamp}, {"output": "same"})]) is None


def test_error_repetition_is_not_hidden_by_volatile_fields():
    detector = LoopDetector()
    for index in range(2):
        assert detector.observe([("run_tests", {"profile": "unit"}, {
            "error": "timeout", "duration_ms": index * 1000,
        })]) is None
    assert detector.observe([("run_tests", {"profile": "unit"}, {
        "error": "timeout", "duration_ms": 2000,
    })]) == "nudge"
