"""Deterministic long-run replay evidence without saving user content."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("count", [1_000, 10_000, 100_000])
def test_long_replay_corpus_keeps_one_segment_and_tool_per_round(count):
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    root = Path(__file__).resolve().parents[1]
    reducer = (root / "static/js/timelineReducer.js").resolve().as_uri()
    fixture = (root / "tests/fixtures/long_replay_corpus.mjs").resolve().as_uri()
    script = r"""
      import assert from 'node:assert/strict';
      const {createTimelineReducer}=await import(process.argv[1]);
      const {longReplayCorpus}=await import(process.argv[2]);
      const count=Number(process.argv[3]);
      const reducer=createTimelineReducer(), tail=[];
      for(const event of longReplayCorpus(count)) {
        assert.equal(reducer.apply(event).accepted,true);
        tail.push(event);if(tail.length>50)tail.shift();
      }
      for(const event of tail)assert.equal(reducer.apply(event).accepted,false);
      const state=reducer.snapshot();
      assert.equal(state.lastSeq,count-1);
      assert.equal(state.segments.length,count/5);
      assert.equal(Object.keys(state.tools).length,count/5);
      assert.equal(state.segments.at(-1).thinking,'[thinking fixture]');
      assert.equal(state.segments.at(-1).text,'[answer fixture]');
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, reducer, fixture, str(count)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr
