"""Older transcript pages remain reachable when the newest page cannot scroll."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("gesture,top,trusted,expected", [
    ("up", 0, True, 1),
    ("touch", 0, True, 1),
    ("up", 80, True, 1),
    ("up", 91, True, 0),
    ("down", 0, True, 0),
    ("up", 0, False, 0),
])
def test_history_gesture_loads_without_a_scroll_event(gesture, top, trusted, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    source = Path(__file__).parents[1] / "static/js/sessions.js"
    script = r"""
      const assert=require('node:assert/strict');
      const fs=require('node:fs');
      const vm=require('node:vm');
      const source=fs.readFileSync(process.argv[1],'utf8');
      const install=source.split('function _installHistoryPager',2)[1].split('function _getIncognitoIds',1)[0];
      let requests=0;
      const listeners={};
      const box={scrollTop:Number(process.argv[3]),scrollHeight:500,clientHeight:1400,
        addEventListener(type,fn){listeners[type]=fn},removeEventListener(){},querySelector(){return null}};
      const context={document:{getElementById(){return box}},currentSessionId:'qa',
        _historyPager:null,_clearHistoryPager(){context._historyPager=null},
        _historyPageLimit(){return 50},_historyUrl(){return '/fixture-only'},
        window:{},console,fetch(){requests++;return new Promise(()=>{})}};
      vm.createContext(context);
      vm.runInContext('function _installHistoryPager'+install,context);
      context._installHistoryPager('qa',{has_more_before:true,offset:40,next_cursor:'older',limit:50},'model');
      assert.equal(requests,0,'opening the page must not eagerly load older history');
      const kind=process.argv[2];
      const event={type:kind==='touch'?'touchmove':'wheel',deltaY:kind==='down'?1:-1,isTrusted:process.argv[4]==='true'};
      listeners[event.type](event);
      assert.equal(requests,Number(process.argv[5]),'a trusted upward gesture at the top must load even without scroll');
      listeners[event.type](event);
      assert.equal(requests,Number(process.argv[5]),'repeated gestures must not duplicate an in-flight request');
      context.currentSessionId='other';
      context._historyPager.loading=false;
      listeners[event.type](event);
      assert.equal(requests,Number(process.argv[5]),'a stale pager must not fetch for another session');
    """
    result = subprocess.run(
        [node, "-e", script, str(source), gesture, str(top), str(trusted).lower(), str(expected)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
