"""The header follows the server's history revision, including deletions."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_header_count_accepts_newer_decrease_but_rejects_stale_poll():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const block=source.split('  const _metaCountEl =',2)[1].split('  // Scrolling',1)[0];
const label={textContent:''},history={querySelectorAll:()=>[]};
let current='session-a';
const window={sessionModule:{getCurrentSessionId:()=>current}};
const context={window,el:id=>id==='current-meta-count'?label:history,
  MutationObserver:class{observe(){}},requestAnimationFrame:fn=>fn()};
vm.runInNewContext('const _metaCountEl ='+block,context);
const set=window.__odysseusSetServerMessageCount;
set('session-a',100,{historyRevision:'2026-09-23T01:00:00'});
assert.equal(label.textContent,'· 100 msgs');
set('session-a',95,{historyRevision:'2026-09-23T01:00:00',monotonic:true});
assert.equal(label.textContent,'· 100 msgs','same-revision lag must not decrease');
set('session-a',80,{historyRevision:'2026-09-23T02:00:00',monotonic:true});
assert.equal(label.textContent,'· 80 msgs','new revision accepts a real deletion');
set('session-a',110,{historyRevision:'2026-09-23T01:00:00',monotonic:true});
assert.equal(label.textContent,'· 80 msgs','stale out-of-order poll is discarded');
set('session-a',90,{historyRevision:'2026-09-23T02:00:00',monotonic:true});
assert.equal(label.textContent,'· 90 msgs');
current='session-b';
set('session-b',3,{historyRevision:'2026-09-23T01:00:00',monotonic:true});
assert.equal(label.textContent,'· 3 msgs','another chat has an independent count');
window.__odysseusClearServerMessageCount();
assert.equal(label.textContent,'');
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "static/app.js")],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_poll_and_initial_history_forward_the_history_revision():
    source = (ROOT / "static/js/sessions.js").read_text(encoding="utf-8")
    assert "monotonic: true, historyRevision: res.data.history_revision" in source
    assert source.count("historyRevision: data.history_revision") >= 2
