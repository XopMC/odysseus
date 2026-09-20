import asyncio
import base64
import subprocess

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from concurrent.futures import ThreadPoolExecutor

from core.database import Base
from src import auto_research_lab as lab
from src import auto_research_isolation
from src.agent_tools import efficiency_tools
from src.agent_tools import model_interaction_tools


def test_candidate_worktree_freezes_a_real_git_commit(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README"], check=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "-c", "user.name=Odysseus Test",
        "-c", "user.email=test@odysseus.invalid", "commit", "-qm", "baseline",
    ], check=True)
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    root, frozen = lab._freeze_worktree(str(tmp_path), sha[:12])
    assert root == str(tmp_path.resolve()) and frozen == sha
    (tmp_path / "dirty.txt").write_text("not committed\n", encoding="utf-8")
    try:
        lab._freeze_worktree(str(tmp_path), sha)
        assert False, "dirty candidates must not enter held-out validation"
    except ValueError:
        pass


def _store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(lab, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(lab, "get_setting", lambda *a: True)
    monkeypatch.setattr(lab, "_freeze_worktree", lambda path, sha: (str(path), str(sha)))


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
    assert verdict["qualified"] is False and verdict["failed_gate_count"] == 1
    assert verdict["heldout_feedback_released"] is False


def test_recursive_loop_selects_train_pareto_before_heldout(tmp_path, monkeypatch):
    _store(monkeypatch)
    exp = lab.create(
        "alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
        gates=[{"metric": "quality", "op": "gte", "threshold": .8}],
        objectives=["quality", "speed"],
    )
    eid = exp["experiment_id"]
    (tmp_path / "heldout").mkdir()
    lab.configure_environment("alice", eid, split="heldout", root=str(tmp_path / "heldout"),
                              manifest={"runner_host_id": "test-host", "command": "true",
                                        "timeout_seconds": 60})
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


def _claim_submit(owner, eid, role, worker, output):
    claimed = lab.claim_work(owner, eid, worker_id=worker, actor_role=role)
    assert claimed["work"] and claimed["work"]["actor_role"] == role
    result = lab.submit_work(owner, eid, event_id=claimed["work"]["event_id"],
                             lease_token=claimed["lease_token"], output=output)
    assert result["exit_code"] == 0
    return result


def test_blind_recursive_workflow_revises_then_seals_heldout(tmp_path, monkeypatch):
    _store(monkeypatch)
    exp = lab.create(
        "alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
        gates=[{"metric": "quality", "op": "gte", "threshold": .8}],
        objectives=["quality", "speed"],
    )
    eid = exp["experiment_id"]
    (tmp_path / "train").mkdir(); (tmp_path / "heldout").mkdir()
    lab.configure_environment("alice", eid, split="train", root=str(tmp_path / "train"),
                              manifest={"tasks": ["train-a"]})
    lab.configure_environment("alice", eid, split="heldout", root=str(tmp_path / "heldout"),
                              manifest={"runner_host_id": "test-host", "command": "true",
                                        "timeout_seconds": 60})
    started = lab.start_lineage("alice", eid, hypothesis="reduce redundant reads", trajectory_count=2)
    assert len(started["queued"]) == 1
    assert started["queued"][0]["stage"] == "oracle"
    _claim_submit("alice", eid, "oracle_analyst", "oracle", {
        "verdict": "proceed", "trajectory_count": 2,
        "hypothesis": "reduce redundant reads",
    })
    for index in range(2):
        _claim_submit("alice", eid, "explorer", f"explorer-{index}", {"trajectory": f"trace-{index}"})
    for index in range(2):
        _claim_submit("alice", eid, "analyzer", f"analyzer-{index}", {"finding": f"finding-{index}"})
    _claim_submit("alice", eid, "reducer", "reducer", {"evidence": ["finding-0", "finding-1"]})
    _claim_submit("alice", eid, "proposer", "proposer", {
        "candidate_sha": "b" * 40, "parent_sha": "a" * 40,
        "hypothesis": "reduce redundant reads", "patch_ref": "refs/candidate",
    })
    _claim_submit("alice", eid, "implementer", "implementer-1", {"candidate_sha": "b" * 40, "patch": "v1", "tests": ["focused"]})
    _claim_submit("alice", eid, "reviewer", "reviewer-1", {"verdict": "revise", "findings": ["missing case"]})
    _claim_submit("alice", eid, "implementer", "implementer-2", {"candidate_sha": "b" * 40, "patch": "v2", "tests": ["focused", "full"]})
    _claim_submit("alice", eid, "reviewer", "reviewer-2", {"verdict": "accept", "evidence": ["full green"]})
    _claim_submit("alice", eid, "validator", "train", {"metrics": {"quality": .9, "speed": .8}, "evidence": ["train"]})
    selected = lab.select_pareto("alice", eid)["selected"]
    assert len(selected) == 1
    heldout = _claim_submit("alice", eid, "heldout_validator", "heldout", {
        "metrics": {"quality": .85, "speed": .75}, "evidence": ["must stay sealed"],
    })
    assert heldout["sealed"] is True
    snapshot = lab.workflow_snapshot("alice", eid)
    serialized = str(snapshot)
    assert "must stay sealed" not in serialized and "0.85" not in serialized
    candidates = lab.candidates("alice", eid)["candidates"]
    assert candidates[0]["status"] == "qualified"
    assert candidates[0]["heldout_metrics_sealed"] is True
    assert "heldout_metrics" not in candidates[0]
    assert any(item["kind"] == "heldout_verdict" for item in snapshot["audit"])


def test_atomic_stage_leases_enforce_parallel_limit(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'leases.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(lab, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(lab, "get_setting", lambda key, default=None: (
        True if key == "auto_research_lab_enabled" else 2 if key == "auto_research_max_parallel" else default
    ))
    monkeypatch.setattr(lab, "_freeze_worktree", lambda path, sha: (str(path), str(sha)))
    exp = lab.create("alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
                     gates=[{"metric": "x", "op": "gte", "threshold": 1}], objectives=["x"])
    lab.start_lineage("alice", exp["experiment_id"], hypothesis="h", trajectory_count=4)
    _claim_submit("alice", exp["experiment_id"], "oracle_analyst", "oracle", {
        "verdict": "proceed", "trajectory_count": 4, "hypothesis": "h",
    })
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda i: lab.claim_work(
            "alice", exp["experiment_id"], worker_id=f"w{i}", actor_role="explorer"
        ), range(4)))
    assert sum(bool(item.get("work")) for item in claimed) == 2


def test_role_worker_can_only_submit_its_leased_result(monkeypatch):
    tool = efficiency_tools.ManageAutoResearchLabTool()
    ctx = {"owner": "alice", "session_id": "s", "subagent_state": {"child_run_id": "child-1"}}
    blocked = asyncio.run(tool.execute('{"action":"workflow","experiment_id":"secret"}', ctx))
    assert blocked["exit_code"] == 1
    blocked = asyncio.run(tool.execute('{"action":"record","experiment_id":"secret","split":"heldout"}', ctx))
    assert blocked["exit_code"] == 1


def test_research_driver_spawns_all_available_roles_without_waiting(monkeypatch):
    queue = [
        {"event_id": "e1", "status": "queued", "actor_role": "explorer", "stage": "trajectory", "iteration": 1, "input": {}},
        {"event_id": "e2", "status": "queued", "actor_role": "explorer", "stage": "trajectory", "iteration": 2, "input": {}},
        {"event_id": "e3", "status": "queued", "actor_role": "explorer", "stage": "trajectory", "iteration": 3, "input": {}},
    ]
    spawned = []

    def snapshot(owner, experiment_id):
        return {"status": "open", "stages": list(queue), "exit_code": 0}

    def claim(owner, experiment_id, **kwargs):
        event = queue.pop(0)
        return {"work": event, "lease_token": "token-" + event["event_id"], "exit_code": 0}

    async def delegate(content, ctx):
        spawned.append(content)
        return {"child_run_id": "child-" + str(len(spawned)), "exit_code": 0}

    monkeypatch.setattr(lab, "workflow_snapshot", snapshot)
    monkeypatch.setattr(lab, "claim_work", claim)
    monkeypatch.setattr(lab, "recover_expired_work", lambda *args: {"requeued": 0, "exit_code": 0})
    monkeypatch.setattr(model_interaction_tools, "delegate_subagent", delegate)
    asyncio.run(efficiency_tools._drive_research("alice", "experiment", {"owner": "alice", "session_id": "s"}))
    assert len(spawned) == 3
    assert all("action=submit_work" in payload for payload in spawned)


def test_heldout_runs_only_in_hard_isolation_and_returns_numeric_metrics(tmp_path, monkeypatch):
    _store(monkeypatch)
    exp = lab.create("alice", None, baseline_sha="a" * 40, candidate_worktree=str(tmp_path),
                     gates=[{"metric": "quality", "op": "gte", "threshold": .8}],
                     objectives=["quality"])
    heldout_root = tmp_path / "heldout"
    heldout_root.mkdir()
    lab.configure_environment("alice", exp["experiment_id"], split="heldout",
                              root=str(heldout_root), manifest={
                                  "runner_host_id": "test-host",
                                  "command": "printf verdict", "timeout_seconds": 60,
                              }, validated_root=True,
                              environment_content_hash="d" * 64)
    proposed = lab.propose("alice", exp["experiment_id"], candidate_sha="b" * 40,
                           parent_sha="a" * 40, hypothesis="candidate")
    lab.record("alice", exp["experiment_id"], candidate_sha="b" * 40,
               split="train", metrics={"quality": .9}, evidence=[])
    lab.select_pareto("alice", exp["experiment_id"])
    claimed = lab.claim_work("alice", exp["experiment_id"], worker_id="driver",
                             actor_role="heldout_validator")
    event_id = claimed["work"]["event_id"]
    monkeypatch.setattr(auto_research_isolation, "SessionLocal", lab.SessionLocal)
    calls = []

    async def host_call(host, op, args, owner, scope):
        calls.append((op, dict(args)))
        if op == "runner.capabilities":
            result = {"supported_ops": ["workspace.digest", "workspace.verification-copy",
                                         "workspace.git-state", "sandbox.command.start"]}
        elif op == "workspace.git-state":
            result = {"root": str(tmp_path), "head": "b" * 40, "clean": True}
        elif op == "workspace.digest":
            result = {"sha256": "c" * 64 if args["cwd"] == str(tmp_path) else "d" * 64}
        elif op == "workspace.verification-copy":
            result = {"path": str(tmp_path / "copy")}
        elif op == "sandbox.command.start":
            assert args["sealed_environment"] == str(heldout_root)
            assert args["expected_environment_hash"] == "d" * 64
            result = {"id": "job"}
        else:
            payload = base64.b64encode(b'{"metrics":{"quality":0.91},"secret":"discard"}\n').decode()
            result = {"id": "job", "status": "exited", "exit_code": 0,
                      "output_base64": payload, "next_offset": len(base64.b64decode(payload)),
                          "check_evidence": {"run_id": event_id, "workspace_hash": "c" * 64,
                          "workspace_hash_after": "c" * 64, "environment_hash": "d" * 64,
                          "environment_hash_after": "d" * 64,
                          "command_hash": __import__("hashlib").sha256(b"printf verdict").hexdigest()}}
        return {"ok": True, "result": result}

    output = asyncio.run(auto_research_isolation.run_heldout(
        "alice", exp["experiment_id"], event_id=event_id,
        lease_token=claimed["lease_token"], host_call=host_call,
    ))
    assert output["metrics"] == {"quality": .91}
    assert output["evidence"] == [] and output["isolation"]["network"] == "none"
    assert [op for op, _ in calls].count("sandbox.command.start") == 1


def test_research_driver_never_delegates_heldout_to_a_model(monkeypatch):
    queue = [{"event_id": "e-heldout", "status": "queued", "actor_role": "heldout_validator",
              "stage": "heldout", "iteration": 1, "input": {}}]
    submitted = []

    monkeypatch.setattr(lab, "workflow_snapshot", lambda *args: (
        {"status": "open", "stages": list(queue), "exit_code": 0} if queue
        else {"status": "closed", "stages": [], "exit_code": 0}
    ))
    monkeypatch.setattr(lab, "recover_expired_work", lambda *args: {"requeued": 0, "exit_code": 0})
    monkeypatch.setattr(lab, "claim_work", lambda *args, **kwargs: {
        "work": queue.pop(0), "lease_token": "sealed-token", "exit_code": 0,
    })
    monkeypatch.setattr(lab, "submit_work", lambda *args, **kwargs: (
        submitted.append(kwargs["output"]) or {"exit_code": 0}
    ))
    async def isolated(*args, **kwargs):
        return {"metrics": {"quality": .9}, "evidence": [], "isolation": {"hard": True}}
    monkeypatch.setattr(auto_research_isolation, "run_heldout", isolated)
    async def forbidden(*args, **kwargs):
        raise AssertionError("held-out work must never become a subagent prompt")
    monkeypatch.setattr(model_interaction_tools, "delegate_subagent", forbidden)
    asyncio.run(efficiency_tools._drive_research("alice", "experiment", {"owner": "alice"}))
    assert submitted == [{"metrics": {"quality": .9}, "evidence": [],
                          "isolation": {"hard": True}}]
