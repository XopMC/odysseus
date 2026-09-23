"""Deterministic long-run replay evidence without saving user content."""

import shutil
import subprocess
import json
import tempfile
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from src.chat_replay_log import ReplayLog
from src import agent_runs
from core.models import ChatMessage, Session
from src.model_context import estimate_tokens


def _server_event(seq: int) -> dict:
    """Python mirror of the frozen, content-free browser corpus."""
    run = "e" * 32
    round_number = seq // 5 + 1
    tool_id = f"fixture-tool-{round_number}"
    phase = seq % 5
    if phase == 0:
        event = {"type": "agent_step", "round": round_number}
    elif phase == 1:
        event = {"delta": "[thinking fixture]", "thinking": True, "round": round_number}
    elif phase == 2:
        event = {"type": "tool_start", "tool": "fixture_tool", "tool_call_id": tool_id, "round": round_number}
    elif phase == 3:
        event = {"type": "tool_output", "tool": "fixture_tool", "tool_call_id": tool_id,
                 "exit_code": 0, "round": round_number}
    else:
        event = {"delta": "[answer fixture]", "round": round_number}
    replay = {"run_id": run, "seq": seq, "segment_id": f"{run}:{round_number}"}
    if event.get("tool_call_id"):
        replay["tool_call_id"] = tool_id
    event["_replay"] = replay
    return event


def _corpus_digest(count: int) -> str:
    digest = hashlib.sha256()
    for seq in range(count):
        digest.update(json.dumps(_server_event(seq), sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


@pytest.mark.parametrize("count", [1_000, 10_000, 100_000])
def test_durable_long_replay_cursor_restart_and_reasoning_index(count):
    manifest = json.loads((Path(__file__).parent / "fixtures/long_replay_corpus_manifest.json").read_text())
    assert manifest["schema"] == "odysseus-long-replay-v1"
    assert _corpus_digest(count) == manifest["sha256"][str(count)]
    with tempfile.TemporaryDirectory() as directory:
        log = ReplayLog(directory, "e" * 32, "safe-fixture", create=True)
        for seq in range(count):
            log.append("data: " + json.dumps(_server_event(seq), separators=(",", ":")) + "\n\n")
        assert len(log) == count
        interrupted = ReplayLog(directory, "e" * 32, "safe-fixture")
        assert interrupted.page(-1, 100)["status"] == "interrupted"
        first = interrupted.page(-1, 100)
        assert first == interrupted.page(-1, 100), "retrying a cursor must not duplicate or reorder frames"
        assert [row["seq"] for row in first["events"]] == list(range(100))

        cursor, seen = -1, 0
        while True:
            page = interrupted.page(cursor, 100)
            assert 0 < len(page["events"]) <= 100
            assert page["events"][0]["seq"] == cursor + 1
            cursor = page["next_seq"]
            seen += len(page["events"])
            if not page["has_more"]:
                break
        assert seen == count and cursor == count - 1
        assert json.loads(interrupted[count - 1].split("data: ", 1)[1])["_replay"]["seq"] == count - 1
        tail = interrupted.page_before(count, 200)
        assert [item["seq"] for item in tail["events"]] == list(range(count - 200, count))
        assert tail["previous_cursor"] == count - 200
        assert interrupted.page_before(tail["previous_cursor"], 200)["events"][-1]["seq"] == count - 201

        log.checkpoint("done")
        completed = ReplayLog(directory, "e" * 32, "safe-fixture")
        assert completed.page(count - 2, 100)["status"] == "done"
        assert completed.reasoning_sequences(1) == [1]
        assert completed.reasoning_sequences(count // 5) == [count - 4]
        assert completed.path(".reasoning-index").exists()
        with patch.dict("os.environ", {"ODYSSEUS_DURABLE_CHAT_REPLAY": "1"}), \
             patch.object(agent_runs, "replay_root", return_value=directory):
            artifact = agent_runs.reasoning_artifact("safe-fixture", "e" * 32, count // 5)
            assert artifact["thinking"] == "[thinking fixture]"
            with pytest.raises(FileNotFoundError):
                agent_runs.reasoning_artifact("other-owner-session", "e" * 32, count // 5)


@pytest.mark.parametrize("count", [1_000, 10_000, 100_000])
def test_long_replay_corpus_keeps_one_segment_and_tool_per_round(count):
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    root = Path(__file__).resolve().parents[1]
    reducer = (root / "static/js/timelineReducer.js").resolve().as_uri()
    fixture = (root / "tests/fixtures/long_replay_corpus.mjs").resolve().as_uri()
    manifest = (root / "tests/fixtures/long_replay_corpus_manifest.json").resolve().as_uri()
    script = r"""
      import assert from 'node:assert/strict';
      import fs from 'node:fs';
      import {createHash} from 'node:crypto';
      const {createTimelineReducer}=await import(process.argv[1]);
      const {longReplayCorpus}=await import(process.argv[2]);
      const count=Number(process.argv[3]);
      const frozen=JSON.parse(fs.readFileSync(new URL(process.argv[4]),'utf8'));
      const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'
        ?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
      const digest=createHash('sha256');
      const reducer=createTimelineReducer(), tail=[];
      for(const event of longReplayCorpus(count)) {
        digest.update(JSON.stringify(canonical(event))+'\n');
        assert.equal(reducer.apply(event).accepted,true);
        tail.push(event);if(tail.length>50)tail.shift();
      }
      assert.equal(digest.digest('hex'),frozen.sha256[String(count)]);
      for(const event of tail)assert.equal(reducer.apply(event).accepted,false);
      const state=reducer.snapshot();
      assert.equal(state.lastSeq,count-1);
      assert.equal(state.segments.length,count/5);
      assert.equal(Object.keys(state.tools).length,count/5);
        assert.equal(state.segments.at(-1).thinking,'[thinking fixture]');
        assert.equal(state.segments.at(-1).text,'[answer fixture]');
        assert.ok(process.memoryUsage().heapUsed<256*1024*1024,
          '100K-event reducer must not consume an unbounded browser heap');
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, reducer, fixture, str(count), manifest],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("count", [1_000, 10_000, 100_000])
def test_backend_corpus_matches_frozen_browser_generator(count):
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    fixture = (Path(__file__).resolve().parents[1] / "tests/fixtures/long_replay_corpus.mjs").resolve().as_uri()
    script = r"""
      const {longReplayCorpus}=await import(process.argv[1]);
      const count=Number(process.argv[2]), wanted=new Set([0,Math.floor(count/2),count-1]), found=[];
      let seq=0;for(const event of longReplayCorpus(count)){if(wanted.has(seq))found.push(event);seq++;}
      console.log(JSON.stringify(found));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, fixture, str(count)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [_server_event(seq) for seq in (0, count // 2, count - 1)]


def test_hundred_thousand_archived_events_do_not_enter_model_context():
    archived = ChatMessage(
        "assistant", "old archived output " * 1000,
        {"timeline_v2": {"events": [_server_event(seq) for seq in range(100_000)]}},
    )
    session = Session(
        id="safe-corpus", name="Safe replay fixture", endpoint_url="http://model.test/v1",
        model="fixture-model", history=[archived, ChatMessage("user", "current safe prompt")],
        context_checkpoint=ChatMessage("system", "short verified summary"),
        context_checkpoint_count=1,
    )
    context = session.get_context_messages()
    assert [item["content"] for item in context] == ["short verified summary", "current safe prompt"]
    assert estimate_tokens(context) < 100
