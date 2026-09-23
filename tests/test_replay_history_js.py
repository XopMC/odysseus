"""Bounded, content-free active-run history reconstruction for lazy pages."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("count", [1_000, 10_000, 100_000])
def test_replay_history_page_shape_and_round_boundaries(count):
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    root = Path(__file__).resolve().parents[1]
    converter = (root / "static/js/replayHistory.js").resolve().as_uri()
    fixture = (root / "tests/fixtures/long_replay_corpus.mjs").resolve().as_uri()
    script = r"""
      import assert from 'node:assert/strict';
      const {longReplayCorpus}=await import(process.argv[1]);
      const {replayEventsToHistoryMessage,replayThinkingStats,splitReplayPageAtRoundBoundary}=await import(process.argv[2]);
      const count=Number(process.argv[3]);
      let page=[],rounds=0,tools=0,first=[];
      const check=()=>{
        if(!page.length)return;
        const shaped=replayEventsToHistoryMessage(page,{runId:'e'.repeat(32),model:'fixture-model'});
        assert.equal(shaped.role,'assistant');
        assert.equal(shaped.metadata.round_texts.length,page.length/5);
        assert.equal(shaped.metadata.round_reasonings.length,page.length/5);
        assert.equal(shaped.metadata.tool_events.length,page.length/5);
        assert.equal(shaped.metadata.round_reasonings.at(-1),'[thinking fixture]');
        assert.ok(shaped.metadata.round_texts.at(-1).endsWith('[answer fixture]'));
        assert.equal(shaped.metadata.tool_events.at(-1).exit_code,0);
        rounds+=shaped.metadata.round_texts.length;
        tools+=shaped.metadata.tool_events.length;
        page=[];
      };
      for(const event of longReplayCorpus(count)){
        if(first.length<15)first.push(event);
        page.push(event);
        if(page.length===200)check();
      }
      check();
      assert.equal(rounds,count/5);
      assert.equal(tools,count/5);
      const split=splitReplayPageAtRoundBoundary(first.slice(2));
      assert.equal(split.incompletePrefix.length,3);
      assert.equal(split.completeRounds.length,10);
      assert.equal(split.completeRounds[0].type,'agent_step');
      assert.equal(replayEventsToHistoryMessage(split.completeRounds).metadata.round_texts.length,2);
      assert.equal(replayThinkingStats(10000,12000,'[thinking fixture]'),'2.0s · 5 tok');
      assert.equal(replayThinkingStats(10000,10000,'[thinking fixture]'),'0.0s · 5 tok');
      assert.ok(process.memoryUsage().heapUsed<256*1024*1024);
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, fixture, converter, str(count)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr
