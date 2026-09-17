import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_timeline_reducer_keeps_segments_and_deduplicates_events():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module_path = Path(__file__).resolve().parents[1] / "static/js/timelineReducer.js"
    script = r"""
      import assert from 'node:assert/strict';
      const { createTimelineReducer } = await import(process.argv[1]);
      const r = createTimelineReducer();
      const run = 'a'.repeat(32);
      const ev = (seq, type, extra = {}) => ({type, ...extra, _replay:{run_id:run, seq, segment_id:`${run}:${extra.round || 1}`, tool_call_id:extra.tool_call_id}});
      assert.equal(r.apply(ev(0, 'agent_step', {round:1})).accepted, true);
      assert.equal(r.apply(ev(1, 'delta', {delta:'thinking', thinking:true, round:1})).accepted, true);
      assert.equal(r.apply(ev(2, 'tool_start', {tool:'bash', command:'echo ok', tool_call_id:'t1', round:1})).accepted, true);
      assert.equal(r.apply(ev(3, 'tool_progress', {tail:'ok', tool_call_id:'t1', round:1})).accepted, true);
      assert.equal(r.apply(ev(4, 'tool_output', {output:'ok', exit_code:0, tool_call_id:'t1', round:1})).accepted, true);
      assert.equal(r.apply(ev(5, 'agent_step', {round:2})).accepted, true);
      assert.equal(r.apply(ev(6, 'delta', {delta:'reply', round:2})).accepted, true);
      assert.equal(r.apply(ev(6, 'delta', {delta:'duplicate', round:2})).accepted, false);
      const s = r.snapshot();
      assert.equal(s.segments.length, 2);
      assert.equal(s.segments[0].thinking, 'thinking');
      assert.deepEqual(s.segments[0].tools, ['t1']);
      assert.equal(s.tools.t1.status, 'done');
      assert.equal(s.segments[1].text, 'reply');
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(module_path.resolve().as_uri())],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}


def test_live_reload_and_paged_reconnect_reduce_to_identical_timeline():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module_path = Path(__file__).resolve().parents[1] / "static/js/timelineReducer.js"
    script = r"""
      import assert from 'node:assert/strict';
      const { createTimelineReducer } = await import(process.argv[1]);
      const run='r'.repeat(32);
      const events=[
        {type:'agent_step',round:1},
        {delta:'analysis one',thinking:true,round:1},
        {delta:'answer one',round:1},
        {type:'tool_start',tool:'bash',command:'echo one',tool_call_id:'call-1',round:1},
        {type:'tool_progress',tail:'running',tool_call_id:'call-1',round:1},
        {type:'tool_output',output:'one',exit_code:0,tool_call_id:'call-1',round:1},
        {type:'agent_step',round:2},
        {delta:'analysis two',thinking:true,round:2},
        {delta:'answer two',round:2},
      ].map((event,seq)=>({...event,_replay:{run_id:run,seq,segment_id:`${run}:${event.round}`,tool_call_id:event.tool_call_id}}));
      const live=createTimelineReducer();
      for(const event of events) live.apply(event);
      const reconnect=createTimelineReducer();
      for(const page of [events.slice(0,4),events.slice(3,7),events.slice(6)]) {
        for(const event of page) reconnect.apply(event);
      }
      assert.deepEqual(reconnect.snapshot(),live.snapshot());
      assert.equal(reconnect.snapshot().segments[0].thinking,'analysis one');
      assert.equal(reconnect.snapshot().segments[1].thinking,'analysis two');
      assert.equal(reconnect.snapshot().tools['call-1'].progress,'running');
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(module_path.resolve().as_uri())],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
