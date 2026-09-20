from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from src import auto_research_lab as lab


def _store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(lab, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(lab, "get_setting", lambda *a: True)


def test_requires_train_and_heldout_and_never_authorizes_deploy(tmp_path, monkeypatch):
    _store(monkeypatch)
    exp = lab.create(
        "alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
        gates=[{"metric": "tests", "op": "gte", "threshold": 1}],
        objectives=["latency"],
    )
    cid = "b" * 40
    assert lab.record("alice", exp["experiment_id"], candidate_sha=cid, split="train",
                      metrics={"tests": 1}, evidence=[])["exit_code"] == 0
    assert lab.evaluate("alice", exp["experiment_id"], cid)["reason"] == "train_and_heldout_required"
    lab.record("alice", exp["experiment_id"], candidate_sha=cid, split="heldout",
               metrics={"tests": 1}, evidence=[])
    verdict = lab.evaluate("alice", exp["experiment_id"], cid)
    assert verdict["qualified"] is True
    assert verdict["deployment_authorized"] is False


def test_gates_are_frozen_and_failed_heldout_rejected(tmp_path, monkeypatch):
    _store(monkeypatch)
    exp = lab.create(
        "alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
        gates=[{"metric": "score", "op": "gte", "threshold": 10}], objectives=["score"],
    )
    cid = "c" * 40
    lab.record("alice", exp["experiment_id"], candidate_sha=cid, split="train",
               metrics={"score": 99}, evidence=[])
    lab.record("alice", exp["experiment_id"], candidate_sha=cid, split="heldout",
               metrics={"score": 9}, evidence=[])
    verdict = lab.evaluate("alice", exp["experiment_id"], cid)
    assert verdict["qualified"] is False and verdict["failed_gates"] == ["score"]


def test_recursive_loop_selects_train_pareto_before_heldout(tmp_path, monkeypatch):
    _store(monkeypatch)
    exp = lab.create(
        "alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
        gates=[{"metric": "quality", "op": "gte", "threshold": .8}],
        objectives=["quality", "speed"],
    )
    eid = exp["experiment_id"]
    for sha, quality, speed in [("b" * 40, .9, .7), ("c" * 40, .8, .6), ("d" * 40, .7, .95)]:
        assert lab.propose("alice", eid, candidate_sha=sha, parent_sha="a" * 40,
                           hypothesis="candidate")["exit_code"] == 0
        assert lab.record("alice", eid, candidate_sha=sha, split="train",
                          metrics={"quality": quality, "speed": speed}, evidence=[])["exit_code"] == 0
    selected = lab.select_pareto("alice", eid)["selected"]
    selected_shas = {item["candidate_sha"] for item in selected}
    assert selected_shas == {"b" * 40, "d" * 40}
    rejected = lab.record("alice", eid, candidate_sha="c" * 40, split="heldout",
                          metrics={"quality": 1, "speed": 1}, evidence=[])
    assert rejected["exit_code"] == 1
    assert lab.record("alice", eid, candidate_sha="b" * 40, split="heldout",
                      metrics={"quality": .9, "speed": .7}, evidence=[])["exit_code"] == 0


def test_candidate_budget_is_bounded(tmp_path, monkeypatch):
    _store(monkeypatch)
    monkeypatch.setattr(lab, "get_setting", lambda key, default=None: 1 if key == "auto_research_max_candidates" else True)
    exp = lab.create("alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
                     gates=[{"metric": "x", "op": "gte", "threshold": 1}], objectives=["x"])
    assert lab.propose("alice", exp["experiment_id"], candidate_sha="b" * 40,
                       parent_sha="a" * 40, hypothesis="one")["exit_code"] == 0
    assert lab.propose("alice", exp["experiment_id"], candidate_sha="c" * 40,
                       parent_sha="a" * 40, hypothesis="two")["exit_code"] == 1


def test_claim_is_bounded_and_pause_fences_new_work(tmp_path, monkeypatch):
    _store(monkeypatch)
    def setting(key, default=None):
        if key == "auto_research_max_parallel": return 1
        if key == "auto_research_max_candidates": return 10
        return True
    monkeypatch.setattr(lab, "get_setting", setting)
    exp = lab.create("alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
                     gates=[{"metric": "x", "op": "gte", "threshold": 1}], objectives=["x"])
    first = lab.propose("alice", exp["experiment_id"], candidate_sha="b" * 40,
                        parent_sha="a" * 40, hypothesis="one")
    second = lab.propose("alice", exp["experiment_id"], candidate_sha="c" * 40,
                         parent_sha="a" * 40, hypothesis="two")
    claim = lab.claim("alice", exp["experiment_id"], first["candidate_id"], "train")
    assert claim["status"] == "training" and claim["heldout_metrics_visible"] is False
    assert lab.claim("alice", exp["experiment_id"], second["candidate_id"], "train")["exit_code"] == 1
    lab.record("alice", exp["experiment_id"], candidate_sha="b" * 40, split="train",
               metrics={"x": 1}, evidence=[])
    assert lab.set_status("alice", exp["experiment_id"], "paused")["exit_code"] == 0
    assert lab.claim("alice", exp["experiment_id"], second["candidate_id"], "train")["exit_code"] == 1
