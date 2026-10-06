"""Unknown receipts are not Goal-wide approval gates, even after reload."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_unknown_receipt_keeps_resume_visible_and_full_access_has_no_decision_panel():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module = Path(__file__).resolve().parents[1] / "static/js/chat-work.js"
    script = r"""
    const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
    class El {
      constructor(){this.hidden=false;this.textContent='';this.value='';this.children=[];
        this.style={removeProperty(){}};this.dataset={};
        this.classList={remove(){},add(){},contains(){return false}};}
      replaceChildren(){this.children=[]} appendChild(x){this.children.push(x)}
      setAttribute(){} querySelector(){return null}
    }
    const nodes=new Map(),document={visibilityState:'hidden',
      getElementById:id=>{if(!nodes.has(id))nodes.set(id,new El());return nodes.get(id)},
      createElement:()=>new El()};
    let mode='ask_important',revision=7,conflict=false;
    const goal=()=>({id:'g',session_id:'s',status:'waiting_user',revision,
      checkpoint:{_wait_reason:'unknown_side_effect'},created_at:'2026-10-06T12:00:00'});
    const wait={phase:'user',goal_status:'waiting_user',wait_reason:'unknown_side_effect',
      recovery_action:'resume_goal',unknown_effect_count:1,blocking_effect_count:1};
    const fetch=async(url,options)=>{
      if(options?.method==='POST'){conflict=true;revision++;
        return {ok:false,status:409,json:async()=>({detail:'Goal revision changed; reload'})};}
      return {ok:true,json:async()=>String(url).endsWith('/api/chat/work/s')
        ? {goal:goal(),cursor:1}:String(url).includes('unknown-effects')
        ? {effects:[{id:'e',status:'unknown',tool_name:'apply_patch'}]}:wait};
    };
    const window={location:{origin:'http://local'},chatModule:{},
      accessModeModule:{getMode:()=>mode}};
    const monitor={start(){},stop(){},snapshot(){return {}}};
    const context=vm.createContext({console,document,window,fetch,clearTimeout(){},setTimeout(){}});
    const mod=new vm.SourceTextModule(fs.readFileSync(process.argv[1],'utf8'),{context});
    await mod.link(async spec=>{
      const entries=spec.includes('i18n')?{bindUiText(){},unbindUiText(){},t:x=>x}
        :spec.includes('runHealth')?{describeProgressHealth:()=>null,describeUiLongTasks:()=>null,
          describeBudgetWarnings:()=>[],createUiLongTaskMonitor:()=>monitor}
        :{removeOrdinaryAskUserCards(){}};
      return new vm.SyntheticModule(Object.keys(entries),function(){
        for(const [name,value] of Object.entries(entries))this.setExport(name,value)
      },{context});
    });
    await mod.evaluate();const work=mod.namespace.default;
    work.beginSessionHydration('s');await work.refresh('s');
    await work.refreshWait('s');await work.refreshEffects('s');work.render();
    for(const access of ['ask_important','full_access']) {
      mode=access;work.render();
      assert.equal(nodes.get('goal-work-resume').hidden,false);
      assert.equal(nodes.get('goal-work-quick-resume').hidden,false);
      assert.equal(nodes.get('wait-unknown-effects').hidden,access==='full_access');
    }
    await work.mutate('goal','resume');
    assert.equal(conflict,true);
    assert.equal(nodes.get('wait-unknown-effects').hidden,true);
    assert.equal(nodes.get('goal-work-resume').hidden,false);
    """
    result = subprocess.run(["node", "--experimental-vm-modules", "-e",
        "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1)})", str(module)],
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
