"""Portable context profiles reject data/authority outside the known schema."""
import subprocess
import unittest
from pathlib import Path


class ContextProfileTests(unittest.TestCase):
    def test_version_fields_types_and_roundtrip(self):
        module = (Path(__file__).resolve().parents[1] / 'static/js/context-profile.js').as_uri()
        script = r'''
          import assert from 'node:assert/strict';
          const {parseContextProfile:parse, serializeContextProfile:serialize}=await import(process.argv[1]);
          const fields=[['auto_compact','Enabled',true],['trigger_percent','Trigger',75,10,95]];
          const wrap=overrides=>JSON.stringify({format:'odysseus-context-policy',version:1,overrides});
          assert.deepEqual(parse(serialize({trigger_percent:60,auto_compact:false},fields),fields),{trigger_percent:60,auto_compact:false});
          assert.deepEqual(parse(wrap({}),fields),{});
          for(const input of ['null','[]','{',wrap({trigger_percent:'60'}),wrap({trigger_percent:96}),
            wrap({auto_compact:0}),wrap({endpoint_id:'remote'}),wrap({api_key:'not-a-real-secret'}),
            '{"format":"odysseus-context-policy","version":1,"overrides":{"__proto__":{}}}',
            wrap({}).replace('"version":1','"version":2'),wrap({}).replace('"version":1','"version":0'),
            wrap({}).replace('"version":1','"version":1,"task_id":"other"'), ' '.repeat(32769)]) {
            assert.throws(()=>parse(input,fields),undefined,input.slice(0,200));
          }
          assert.throws(()=>serialize({secret:'no'},fields));
          assert.equal({}.polluted,undefined);
        '''
        result = subprocess.run(['node', '--input-type=module', '-e', script, module], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
