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
