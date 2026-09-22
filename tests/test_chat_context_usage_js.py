"""Drive the real chat module's context rendering seam with isolated DOM/import boundaries."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_stream_context_updates_only_selected_session_and_wins_stale_get():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module_path = Path(__file__).resolve().parents[1] / "static/js/chat.js"
    script = r"""
      const fs = require('node:fs');
      const vm = require('node:vm');
      const assert = require('node:assert/strict');
      const code = fs.readFileSync(process.argv[1], 'utf8');
      const noop = () => {};
      const classes = new Set();
      const pill = {hidden: true, innerHTML: '', title: '',
        style: {setProperty: noop}, addEventListener: noop,
        classList: {add: x => classes.add(x), remove: (...xs) => xs.forEach(x => classes.delete(x)), contains: x => classes.has(x)}};
      const selected = {id: 'chat-a', model: 'mac-qwen'};
      let currentSession = 'chat-a';
      const sm = {getCurrentSessionId: () => currentSession, getSessions: () => [selected]};
      let releaseGet;
      const getResult = new Promise(resolve => {releaseGet = resolve;});
      const document = {body: {querySelectorAll: () => [], addEventListener: noop},
        addEventListener: noop, querySelectorAll: () => [], querySelector: () => null,
        getElementById: id => id === 'chat-context-pill' ? pill : null};
      const context = vm.createContext({console, document, window: {sessionModule: sm},
        MutationObserver: class {observe() {}}, setTimeout: noop, clearTimeout: noop,
        fetch: async () => ({ok: true, json: () => getResult})});
      const mod = new vm.SourceTextModule(code, {context});
      // Imported modules are genuine app boundaries; none participates in the
      // pure context renderer under test. Stub their declared export bindings.
      const names = new Set(['default']);
      for (const match of code.matchAll(/import(?:\s+\w+\s*,)?\s*\{([\s\S]*?)\}\s+from/g)) {
        for (const name of match[1].split(',')) {
          const clean = name.trim().split(/\s+as\s+/)[0];
          if (clean) names.add(clean);
        }
      }
      await mod.link(async () => new vm.SyntheticModule([...names], function() {
        for (const name of names) this.setExport(name, noop);
      }, {context}));
      await mod.evaluate();
      const apply = mod.namespace.applyStreamContextUsage;
      const snapshot = {used_tokens: 82000, prompt_tokens: 81000, context_length: 262144,
        context_percent: 1.2, source: 'backend', model: 'mac-qwen', round: 12};
      const pending = mod.namespace.refreshChatContextHeader('initial');
      await Promise.resolve();
      assert.equal(apply(snapshot, 'chat-a'), true);
      assert.match(pill.innerHTML, /31\.3%/);
      assert.match(pill.title, /Live request.*backend tokens/);
      assert.equal(pill.hidden, false);
      const correctTitle = pill.title;
      assert.equal(apply({...snapshot, used_tokens: 1}, 'chat-b'), false);
      assert.equal(apply({...snapshot, model: 'wrong-model'}, 'chat-a'), false);
      assert.equal(apply({...snapshot, source: 'billing'}, 'chat-a'), false);
      assert.equal(pill.title, correctTitle);
      releaseGet({session_id: 'chat-a', used_tokens: 1627, context_length: 262144,
        context_percent: 0.6, source: 'estimated', context_status: 'stored_chat'});
      await pending;
      assert.equal(pill.title, correctTitle);
      selected.endpoint_url='http://mac.test/v1';
      context.fetch=async()=>({ok:true,json:async()=>({session_id:'chat-a',model:'mac-qwen',
        endpoint_url:selected.endpoint_url,current_endpoint_key:'a'.repeat(64),used_tokens:82000,
        context_length:262144,context_percent:31.3,source:'backend',context_status:'active_request'})});
      await mod.namespace.refreshChatContextHeader('pin-endpoint');
      assert.equal(apply({...snapshot,endpoint_key:'a'.repeat(64)},'chat-a'),true);
      const endpointTitle=pill.title;
      assert.equal(apply({...snapshot,endpoint_key:'b'.repeat(64),used_tokens:1},'chat-a'),false);
      assert.equal(apply({...snapshot,endpoint_key:'malformed'},'chat-a'),false);
      assert.equal(pill.title,endpointTitle);
      assert.equal(apply({...snapshot,endpoint_key:'a'.repeat(64),
        used_tokens:40000,context_length:131840},'chat-a'),true,
        'same model reloaded with a new window must accept a new measurement');
      assert.match(pill.title,/40,000 \/ 131,840/);
      await mod.namespace.refreshChatContextHeader('restore-old-window');
      selected.endpoint_url='http://jetson.test/v1';
      assert.equal(apply({...snapshot,endpoint_key:'a'.repeat(64),used_tokens:1},'chat-a'),false,'switch invalidates cached route identity');
      await Promise.resolve();await Promise.resolve();
      selected.model='larger-model';
      assert.equal(apply({...snapshot,model:'larger-model',used_tokens:40000,
        context_length:524288,context_revision:1},'chat-a'),true,
        'new model route must not inherit old monotonic usage guard');
      assert.match(pill.title,/40,000 \/ 524,288/);
      const switchedTitle=pill.title;
      currentSession = 'chat-b';
      assert.equal(apply({...snapshot, used_tokens: 90000}, 'chat-a'), false);
      assert.equal(pill.title, switchedTitle);
      console.log(JSON.stringify({passed: true}));
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e", "(async () => {" + script + "})().catch(e => {console.error(e); process.exit(1);});", str(module_path)],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
