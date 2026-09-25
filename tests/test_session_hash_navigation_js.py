"""A second browser must resolve a newly-created chat absent from its stale sidebar."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_unknown_session_hash_refreshes_inventory_before_navigation():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module_path = Path(__file__).resolve().parents[1] / "static/js/sessions.js"
    script = r"""
      const fs = require('node:fs');
      const assert = require('node:assert/strict');
      const code = fs.readFileSync(process.argv[1], 'utf8');
      const start = 'async function _handleSessionHashNavigation() {';
      const end = "\n}\nwindow.addEventListener('hashchange'";
      assert.ok(code.includes(start));
      const body = code.split(start, 2)[1].split(end, 1)[0];
      const handler = start + body + '\n}; return _handleSessionHashNavigation;';
      const oldId = '11111111-1111-4111-8111-111111111111';
      const newId = '22222222-2222-4222-8222-222222222222';
      async function scenario({known=false,hash='#'+newId,changeDuringFetch=false}={}) {
        const window = {location:{hash}};
        const sessions = [{id:oldId,archived:false}];
        if (known) sessions.push({id:newId,archived:false});
        let fetches = 0;
        const selected = [];
        async function loadSessions() {
          fetches++;
          sessions.push({id:newId,archived:false});
          if (changeDuringFetch) window.location.hash = '#33333333-3333-4333-8333-333333333333';
        }
        async function selectSession(id) { selected.push(id); }
        const run = new Function('window','sessions','currentSessionId',
          'loadSessions','selectSession',handler)(window,sessions,oldId,loadSessions,selectSession);
        await run();
        return {fetches,selected};
      }
      assert.deepEqual(await scenario(),{fetches:1,selected:[newId]});
      assert.deepEqual(await scenario({known:true}),{fetches:0,selected:[newId]});
      assert.deepEqual(await scenario({hash:'#document-example'}),{fetches:0,selected:[]});
      assert.deepEqual(await scenario({changeDuringFetch:true}),{fetches:1,selected:[]});
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(["node", "-e", "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1)})",
                             str(module_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
