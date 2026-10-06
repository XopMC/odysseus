"""Exercise the production checkpoint pager, including stale-owner-scope races."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('scenario', ['append', 'stale_button', 'stale_response'])
def test_checkpoint_pager_keeps_scope_and_earlier_cards(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    source = (Path(__file__).resolve().parents[1] / 'static/js/team-workspace.js').read_text()
    body = 'async function loadFileCheckpoints' + source.split('async function loadFileCheckpoints',1)[1].split('  function renderWorkers()',1)[0]
    script = r'''
      import assert from 'node:assert/strict';
      let generation=1;
      const current=t=>t===generation;
      function element(tag,text='') {
        return {tag,text,dataset:{},children:[],setAttribute(){},
          append(...values){for(const value of values){if(value&&typeof value==='object')value.parent=this;this.children.push(value);}},
          replaceChildren(){this.children=[];},
          querySelector(){return this.children.find(c=>c.dataset?.fileCheckpointMore);},
          remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}};
      }
      const uiElement=element, terminalPlainText=x=>x;
      const fileRollbackArguments=()=>{throw new Error('fixture has no rollback paths');};
      const button=(text,onclick)=>Object.assign(element('button',text),{onclick});
      const act=fn=>fn();
      const ui={hostScope:{value:'qa'},fileCheckpointList:element('section')};
      const calls=[];
      let release;
      const cursor={created_at:7,id:'older'};
      async function host(op,args) {
        calls.push({op,args});
        if(SCENARIO==='stale_response')await new Promise(resolve=>release=resolve);
        return {checkpoints:[{id:calls.length===1?'first':'older',status:'applied',files:[]}],
          next_cursor:calls.length===1?cursor:null};
      }
      FUNCTION
      if(SCENARIO==='stale_response') {
        ui.fileCheckpointList.append(element('article','preserved'));
        const pending=loadFileCheckpoints();
        ui.hostScope.value='different-owner-scope';generation++;
        release();await pending;
        assert.equal(ui.fileCheckpointList.children[0].text,'preserved');
        assert.equal(ui.fileCheckpointList.children.length,1);
      } else {
        await loadFileCheckpoints();
        assert.deepEqual(calls[0],{op:'file.checkpoint.list',args:{before:null,limit:200}});
        const more=ui.fileCheckpointList.querySelector();
        assert.ok(more);
        if(SCENARIO==='stale_button')ui.hostScope.value='different-owner-scope';
        await more.onclick({currentTarget:more});
        if(SCENARIO==='append') {
          assert.equal(calls.length,2);
          assert.deepEqual(calls[1].args,{before:cursor,limit:200});
          assert.equal(ui.fileCheckpointList.children.filter(c=>c.tag==='article').length,2);
          assert.equal(ui.fileCheckpointList.querySelector(),undefined);
        } else assert.equal(calls.length,1);
      }
    '''.replace('SCENARIO',json.dumps(scenario)).replace('FUNCTION',body)
    result = subprocess.run([node,'--input-type=module','-e',script],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
