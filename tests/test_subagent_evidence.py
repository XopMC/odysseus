from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatSubagentRun, Session
from src import subagent_evidence as board
from src.tool_execution import _execute_tool_block_impl
from types import SimpleNamespace
import asyncio


def _store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(board, "SessionLocal", factory)
    db = factory()
    db.add(Session(id="s", name="chat", endpoint_url="http://x", model="m", owner="alice"))
    db.commit()
    for child in ("a", "b"):
        db.add(ChatSubagentRun(
            id=child, parent_session_id="s", parent_run_id="p", owner="alice",
            ordinal=1, name=child, objective="work", assigned_context="",
            model="m", status="completed", removed=False,
        ))
    db.commit(); db.close()


def test_append_only_candidate_requires_independent_verifier(monkeypatch):
    _store(monkeypatch)
    evidence = board.publish("alice", "s", "a", kind="reproduction", body="pytest: 3 passed")
    candidate = board.submit_candidate(
        "alice", "s", "a", title="fix", payload={"sha": "abc"},
        evidence_ids=[evidence["evidence_id"]],
    )
    refused = board.verify_candidate(
        "alice", "s", candidate["candidate_id"], "a", verdict="accepted"
    )
    assert refused["exit_code"] == 1
    accepted = board.verify_candidate(
        "alice", "s", candidate["candidate_id"], "b", verdict="accepted", notes="reproduced"
    )
    assert accepted["status"] == "accepted"
    assert board.list_candidates("alice", "s")["candidates"][0]["payload"] == {"sha": "abc"}


def test_owner_isolation(monkeypatch):
    _store(monkeypatch)
    result = board.publish("mallory", "s", "a", kind="finding", body="hidden")
    assert result["exit_code"] == 1
    assert board.list_evidence("mallory", "s")["evidence"] == []


def test_dispatcher_forwards_child_identity_to_evidence_tool(monkeypatch):
    _store(monkeypatch)
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    result = asyncio.run(_execute_tool_block_impl(
        SimpleNamespace(tool_type="publish_subagent_evidence", content='{"kind":"verified","body":"exact"}'),
        owner="alice", session_id="s", subagent_state={"child_run_id": "a"},
    ))[1]
    assert result["exit_code"] == 0
    assert board.list_evidence("alice", "s")["evidence"][0]["body"] == "exact"
