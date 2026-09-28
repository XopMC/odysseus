"""The work dock must not paint historical replay state before durable hydration."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_replay_cannot_flash_or_restore_an_old_goal_after_reload():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module_path = Path(__file__).resolve().parents[1] / "static/js/chat-work.js"
    script = r"""
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
      const code=fs.readFileSync(process.argv[1],'utf8');
      class FakeEl {
        constructor(id=''){this.id=id;this.hidden=false;this.textContent='';this.value='';this.title='';
          this.style={removeProperty(){}};this.classList={remove(){},add(){},contains(){return false}}}
        replaceChildren(){} appendChild(){} setAttribute(){} querySelector(){return null}
      }
      const nodes=new Map();
      const document={visibilityState:'hidden',getElementById:id=>{
        if(!nodes.has(id))nodes.set(id,new FakeEl(id));return nodes.get(id)},
        createElement:()=>new FakeEl()};
      const oldGoal={id:'goal',session_id:'s',status:'active',attempt:2,revision:2,
        created_at:'2026-09-28T12:00:00',objective:'old'};
      const currentGoal={...oldGoal,id:'current-goal',status:'completed',attempt:10,revision:20,
        created_at:'2026-09-28T13:57:01',objective:'current'};
      const oldPlan={id:'plan',session_id:'s',status:'executing',revision:2,created_at:'2026-09-28T12:00:00',
        steps:[{id:'a',text:'step',status:'pending'}]};
      const currentPlan={...oldPlan,status:'done',revision:20,
        steps:[{id:'a',text:'step',status:'done'}]};
      let release;
      const workResponse=new Promise(resolve=>release=resolve);
      const fetch=async url=>({ok:true,json:async()=>url.endsWith('/api/chat/work/s')
        ? workResponse : url.includes('unknown-effects')?{effects:[]}:{phase:'idle'}});
      const monitor={start(){},stop(){},snapshot(){return {}}};
      const window={location:{origin:'http://local'},chatModule:{}};
      const context=vm.createContext({console,document,window,fetch,clearTimeout(){},setTimeout(){}});
      const mod=new vm.SourceTextModule(code,{context});
      await mod.link(async spec=>{
        const entries=spec.includes('i18n')?{
          bindUiText(){},unbindUiText(){},t:x=>x,
        }:spec.includes('runHealth')?{
          describeProgressHealth:()=>null,describeUiLongTasks:()=>null,
          describeBudgetWarnings:()=>[],createUiLongTaskMonitor:()=>monitor,
        }:{removeOrdinaryAskUserCards(){}};
        return new vm.SyntheticModule(Object.keys(entries),function(){
          for(const [name,value] of Object.entries(entries))this.setExport(name,value)
        },{context});
      });
      await mod.evaluate();
      const work=mod.namespace.default;
      work.beginSessionHydration('s');
      work.handleEvent({type:'goal_update',data:oldGoal});
      work.handleEvent({type:'plan_update',data:oldPlan});
      assert.equal(nodes.get('goal-mode-status').hidden,true);
      assert.equal(nodes.get('plan-mode-status').hidden,true);
      const pending=work.refresh('s');
      await Promise.resolve();
      assert.equal(nodes.get('goal-mode-status').hidden,true);
      release({goal:currentGoal,plan:currentPlan,cursor:100});
      await pending;
      assert.match(nodes.get('goal-work-state').textContent,/completed.*10/);
      assert.match(nodes.get('plan-work-progress').textContent,/1\/1.*done/);
      work.handleEvent({type:'goal_update',data:oldGoal});
      work.handleEvent({type:'plan_update',data:oldPlan});
      assert.match(nodes.get('goal-work-state').textContent,/completed.*10/);
      assert.match(nodes.get('plan-work-progress').textContent,/1\/1.*done/);
      work.handleEvent({type:'goal_update',data:{...oldGoal,id:'previous-goal',revision:99,
        created_at:'2026-09-27T12:00:00'}});
      work.handleEvent({type:'plan_update',data:{...oldPlan,id:'previous-plan',revision:99,
        created_at:'2026-09-27T12:00:00'}});
      work.handleEvent({type:'goal_update',data:{...oldGoal,id:'newer-but-other-chat',session_id:'other',
        created_at:'2026-09-29T12:00:00'}});
      // A historical goal_guidance event was the remaining unguarded path:
      // on a real 7-step QA chat it briefly painted an older active attempt 2.
      work.handleEvent({type:'goal_guidance',data:{goal:oldGoal,guidance:'old guidance'}});
      assert.match(nodes.get('goal-work-state').textContent,/completed.*10/);
      assert.match(nodes.get('plan-work-progress').textContent,/1\/1.*done/);
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e",
         "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1)})",
         str(module_path)],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}


def test_work_replay_waits_for_snapshot_and_coalesces_generic_events():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module_path = Path(__file__).resolve().parents[1] / "static/js/chat-work.js"
    script = r"""
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
      const code=fs.readFileSync(process.argv[1],'utf8');
      class FakeEl {
        constructor(id=''){this.id=id;this.hidden=false;this.textContent='';this.value='';
          this.style={removeProperty(){}};this.classList={remove(){},add(){},contains(){return false}}}
        replaceChildren(){} appendChild(){} setAttribute(){} querySelector(){return null}
        addEventListener(){}
      }
      const nodes=new Map(),listeners=new Map(),timers=[];
      const document={visibilityState:'visible',getElementById:id=>{
        if(!nodes.has(id))nodes.set(id,new FakeEl(id));return nodes.get(id)},
        createElement:()=>new FakeEl(),querySelectorAll:()=>[],addEventListener(){}};
      const window={location:{origin:'http://local'},chatModule:{},
        addEventListener:(type,fn)=>listeners.set(type,fn)};
      let release; const firstSnapshot=new Promise(resolve=>release=resolve);
      let snapshotReads=0, failedReads=0; const requests=[],streams=[];
      const fetch=async url=>{
        requests.push(String(url));
        if(String(url).endsWith('/api/chat/work/s')){
          snapshotReads++;
          return {ok:true,json:async()=>snapshotReads===1?firstSnapshot:
            {plan:null,goal:null,cursor:2190}};
        }
        if(String(url).endsWith('/api/chat/work/t')){
          failedReads++;
          return failedReads===1?{ok:false,status:503,json:async()=>({detail:'unavailable'})}:
            {ok:true,json:async()=>({plan:null,goal:null,cursor:77})};
        }
        return {ok:true,json:async()=>url.includes('/events?')?
          {events:[],next_cursor:2190}:url.includes('unknown-effects')?{effects:[]}:{phase:'idle'}};
      };
      class EventSource {constructor(url){this.url=url;streams.push(url)}close(){}}
      const monitor={start(){},stop(){},snapshot(){return {}}};
      const context=vm.createContext({console,document,window,fetch,EventSource,
        clearTimeout(){},clearInterval(){},setInterval(){},
        setTimeout:fn=>{timers.push(fn);return timers.length}});
      const mod=new vm.SourceTextModule(code,{context});
      await mod.link(async spec=>{
        const entries=spec.includes('i18n')?{
          bindUiText(){},unbindUiText(){},t:x=>x,
        }:spec.includes('runHealth')?{
          describeProgressHealth:()=>null,describeUiLongTasks:()=>null,
          describeBudgetWarnings:()=>[],createUiLongTaskMonitor:()=>monitor,
        }:{removeOrdinaryAskUserCards(){}};
        return new vm.SyntheticModule(Object.keys(entries),function(){
          for(const [name,value] of Object.entries(entries))this.setExport(name,value)
        },{context});
      });
      await mod.evaluate();
      const work=mod.namespace.default;
      work.bind();work.beginSessionHydration('s');
      const pending=work.refresh('s');
      listeners.get('focus')();
      await Promise.resolve();
      assert.equal(streams.length,0,'SSE must not start at cursor zero before snapshot');
      assert.equal(requests.filter(url=>url.includes('/events?')).length,0,
        'poll must not replay historical events before snapshot');
      release({plan:null,goal:null,cursor:2190});await pending;
      assert.equal(streams.length,1);
      assert.match(streams[0],/after=2190$/);
      for(let i=0;i<284;i++)work.handleEvent({type:'plan_saved',seq:2191+i});
      assert.equal(snapshotReads,1,'generic replay events should share one refresh');
      for(const timer of timers.splice(0))timer();
      await Promise.resolve();await Promise.resolve();
      assert.equal(snapshotReads,2,'one coalesced refresh for the burst');
      work.beginSessionHydration('t');
      await work.refresh('t');
      assert.equal(streams.length,1,'a failed first snapshot must not open after=0 SSE');
      assert.equal(failedReads,1);
      assert.equal(timers.length,1,'failed hydration should schedule one retry');
      timers.shift()();
      await new Promise(resolve=>setImmediate(resolve));
      assert.equal(failedReads,2);
      assert.equal(streams.length,2);
      assert.match(streams[1],/after=77$/);
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e",
         "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1)})",
         str(module_path)],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
