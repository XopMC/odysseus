"""Progress warnings must not confuse a live SSE heartbeat with useful work."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_goal_health_warning_requires_active_stalled_run():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    source = Path(__file__).resolve().parents[1] / "static/js/runHealth.js"
    script = r"""
      (async()=>{
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
      const module=new vm.SourceTextModule(fs.readFileSync(process.argv[1],'utf8'));
      await module.link(()=>{throw Error('unexpected dependency')});await module.evaluate();
      const describe=module.namespace.describeProgressHealth;
      const describeUi=module.namespace.describeUiLongTasks;
      const describeBudgets=module.namespace.describeBudgetWarnings;
      const active={status:'active'},running={status:'running',run_id:'run-1',progress_health:{
        revision:0,stalled:true,seconds_without_progress:731,last_heartbeat_at:1000,
      }};
      const warning=describe(active,running,1010000);
      assert.equal(warning.minutes,12);
      assert.equal(warning.heartbeatAlive,true);
      assert.equal(warning.runId,'run-1');
      const capacityWarning=describe(active,{...running,progress_health:{...running.progress_health,tracking_capacity_exhausted:true}},1010000);
      assert.equal(capacityWarning.trackingCapacityExhausted,true);
      assert.equal(describe(active,{...running,status:'done'},1010000),null);
      assert.equal(describe({status:'waiting_user'},running,1010000),null);
      assert.equal(describe(active,{...running,progress_health:{...running.progress_health,stalled:false}},1010000),null);
      assert.equal(describe(active,{...running,progress_health:{...running.progress_health,last_heartbeat_at:null}},1010000).heartbeatAlive,false);
      assert.equal(describeUi(active,{supported:false,count:20,max_duration_ms:900}),null);
      assert.equal(describeUi({status:'paused'},{supported:true,count:20,max_duration_ms:900}),null);
      assert.equal(describeUi(active,{supported:true,count:2,max_duration_ms:900}),null);
      assert.equal(describeUi(active,{supported:true,count:3,max_duration_ms:199}),null);
      assert.deepEqual(JSON.parse(JSON.stringify(describeUi(active,{supported:true,count:3,max_duration_ms:200}))),
        {count:3,maxDurationMs:200,countLimit:3,durationLimitMs:200});
      assert.deepEqual(JSON.parse(JSON.stringify(describeBudgets(active,{status:'running',health_metrics:{budget_warnings:[
        {resource:'model_tokens',used:800,limit:1000,soft_limit:800},
        {resource:'process_rss',used:800,limit:1000,soft_limit:800},
        {resource:'children',used:true,limit:10,soft_limit:8},
      ]}}))),[{resource:'model_tokens',used:800,limit:1000,soft_limit:800}]);
      assert.deepEqual(describeBudgets({status:'paused'},{status:'running',health_metrics:{budget_warnings:[
        {resource:'model_tokens',used:800,limit:1000,soft_limit:800},
      ]}}),[]);
      })().catch(error=>{console.error(error);process.exitCode=1});
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e", script, str(source)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_ui_long_task_monitor_is_bounded_and_content_free():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    source = Path(__file__).resolve().parents[1] / "static/js/runHealth.js"
    script = r"""
      (async()=>{
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
      const module=new vm.SourceTextModule(fs.readFileSync(process.argv[1],'utf8'));
      await module.link(()=>{throw Error('unexpected dependency')});await module.evaluate();
      let callback,disconnected=0;
      class Observer {constructor(fn){callback=fn}observe(options){assert.deepEqual(Array.from(options.entryTypes),['longtask'])}disconnect(){disconnected++}}
      const monitor=module.namespace.createUiLongTaskMonitor(Observer);
      assert.equal(monitor.start(),true);
      callback({getEntries:()=>[{duration:80,name:'private URL'},{duration:250,name:'secret'}]});
      assert.deepEqual(JSON.parse(JSON.stringify(monitor.snapshot())),{supported:true,count:2,max_duration_ms:250});
      assert.ok(!JSON.stringify(monitor.snapshot()).includes('private'));
      monitor.stop();assert.equal(disconnected,1);
      monitor.reset();assert.equal(monitor.snapshot().count,0);
      assert.equal(module.namespace.createUiLongTaskMonitor(null).start(),false);
      })().catch(error=>{console.error(error);process.exitCode=1});
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e", script, str(source)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_goal_health_is_owner_snapshot_driven_and_hidden_without_active_goal():
    root = Path(__file__).resolve().parents[1]
    work = (root / "static/js/chat-work.js").read_text(encoding="utf-8")
    html = (root / "static/index.html").read_text(encoding="utf-8")
    app = (root / "static/app.js").read_text(encoding="utf-8")
    sw = (root / "static/sw.js").read_text(encoding="utf-8")
    assert "/api/chat/run/${encodeURIComponent(targetSession)}" in work
    assert "describeProgressHealth" in work
    assert "goal-work-health-indicator" in html
    assert "goal-work-health-detail" in html
    assert "describeUiLongTasks(goal, uiLongTasks.snapshot())" in work
    assert "describeBudgetWarnings(goal, runHealthSnapshot)" in work
    assert "./runHealth.js?v=20260924budgetwarn1" in work
    assert "/static/js/runHealth.js?v=20260924budgetwarn1" in sw
    assert "./js/chat-work.js?v=20260925effectrecovery1" in app
    assert "/static/js/chat-work.js?v=20260925effectrecovery1" in sw


def test_goal_warning_renders_from_real_work_module_and_clears_on_pause():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    root = Path(__file__).resolve().parents[1] / "static/js"
    script = r"""
      (async()=>{
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
      const ids={};for(const id of [
        'goal-mode-status','goal-work-state','goal-work-objective','goal-work-objective-preview','goal-work-ui-lag',
        'goal-work-budget-warning',
        'goal-work-budget-warning-indicator',
        'goal-work-progress','goal-work-pause','goal-work-resume','goal-work-cancel',
        'goal-work-quick-pause','goal-work-quick-resume','goal-work-quick-cancel',
        'goal-mode-status-toggle','goal-work-health-indicator','goal-work-health-detail',
        'wait-mode-status','wait-phase','wait-duration','wait-run-id','wait-child-id',
        'wait-model','wait-endpoint','wait-lease','wait-checkpoint','wait-recovery',
        'wait-action','wait-tool','wait-unknown-effects','chat-context-pill',
      ])ids[id]={hidden:false,textContent:'',value:'',title:'',style:{removeProperty(){}},setAttribute(k,v){this[k]=v}};
      ids['wait-unknown-effects'].children=[];
      ids['wait-unknown-effects'].replaceChildren=function(){this.children=[]};
      ids['wait-unknown-effects'].appendChild=function(child){this.children.push(child)};
      const waitClasses=new Set();
      ids['wait-mode-status'].classList={add:x=>waitClasses.add(x),remove:x=>waitClasses.delete(x),contains:x=>waitClasses.has(x)};
      ids['wait-mode-status'].querySelector=()=>({setAttribute(){}});
      let goal={id:'goal-1',objective:'Harmless fixture',status:'active',attempt:1,progress:'',revision:1};
      let uiLongTaskCallback;
      class FakePerformanceObserver {constructor(callback){uiLongTaskCallback=callback}observe(){}disconnect(){}}
      let run={run_id:'run-1',status:'running',progress_health:{stalled:true,seconds_without_progress:660,last_heartbeat_at:1000,tracking_capacity_exhausted:true}};
      let wait={run_id:'run-1',phase:'tool',phase_seconds:22,model:'fixture-model',endpoint_id:'endpoint-1',
        tool:'run_tests',current_child:{child_id:'child-1',model:'worker-model'},
        lease:{held:true,expires_at:'2099-01-01T00:00:00Z'},
        checkpoint:{durable_seq:7,context_revision:3,ledger_hash:'a'.repeat(64)},
        recovery_action:'inspect'};
      let effects=[],noRetryCalls=0,verifyCalls=0,retryAuthorizeCalls=0,staleResume=false,blockedResume=false,errorToasts=[];
      let confirmAnswers=[false,true,true];
      let delayOld=false,releaseOld;
      const document={visibilityState:'visible',getElementById:id=>ids[id]||null,querySelectorAll:()=>[],
        createElement:tag=>({tag,textContent:'',dataset:{},children:[],append(...nodes){this.children.push(...nodes)},appendChild(node){this.children.push(node)}})};
      let resumeCalls=0,reloadCalls=0,goalResumeCalls=0,contextClicks=0;
      ids['chat-context-pill'].click=()=>{contextClicks++};
      const window={location:{origin:'http://odysseus.test',reload:()=>{reloadCalls++}},
        confirm:()=>confirmAnswers.length?confirmAnswers.shift():true,
        prompt:()=> 'safe verification note',
        uiModule:{showError:message=>errorToasts.push(message)},
        sessionModule:{getCurrentSessionId:()=> 'chat-1'},
        chatModule:{resumeStream:async()=>{resumeCalls++;return false}}};
      const context=vm.createContext({window,document,console,setTimeout,clearTimeout,setInterval,clearInterval,
        PerformanceObserver:FakePerformanceObserver,
        fetch:async url=>{
          if(url.includes('/unknown-effects/')&&url.endsWith('/verify')){
            verifyCalls++;effects=[{...effects[0],status:'verified_not_applied',revision:3}];
            return{ok:true,json:async()=>({status:'verified_not_applied',revision:3})};
          }
          if(url.includes('/unknown-effects/')&&url.endsWith('/authorize-retry')){
            retryAuthorizeCalls++;effects=[{...effects[0],status:'retry_authorized',revision:4}];
            return{ok:true,json:async()=>({status:'retry_authorized',revision:4})};
          }
          if(url.includes('/unknown-effects/')&&url.endsWith('/no-retry')){noRetryCalls++;effects=[];return{ok:true,json:async()=>({status:'no_retry'})};}
          if(url.endsWith('/unknown-effects'))return{ok:true,json:async()=>({effects})};
          if(url.endsWith('/goal/resume')){goalResumeCalls++;
            if(blockedResume)return{ok:false,status:409,json:async()=>({detail:'Tool effect must be reconciled or explicitly authorized before Goal resumes'})};
            goal={...goal,status:'active',revision:goal.revision+1};
            if(staleResume){staleResume=false;return{ok:false,status:409,json:async()=>({detail:'Goal changed; reload'})};}
            return{ok:true,json:async()=>goal};}
          if(url.endsWith('/why-waiting'))return{ok:true,json:async()=>url.includes('/chat-2/')?{phase:'idle',run_id:null,goal_status:null}:wait};
          if(url.includes('/api/chat/work/'))return{ok:true,json:async()=>({goal:url.endsWith('chat-2')?null:goal,plan:null,cursor:0})};
          if(delayOld && url.endsWith('/chat-1'))return new Promise(resolve=>{releaseOld=()=>resolve({ok:true,json:async()=>run})});
          return{ok:true,json:async()=>run};
        }});
      const work=new vm.SourceTextModule(fs.readFileSync(path.join(process.argv[1],'chat-work.js'),'utf8'),{context});
      const health=new vm.SourceTextModule(fs.readFileSync(path.join(process.argv[1],'runHealth.js'),'utf8'),{context});
      const i18n=new vm.SyntheticModule(['bindUiText','unbindUiText','t'],function(){
        this.setExport('bindUiText',()=>{});this.setExport('unbindUiText',()=>{});this.setExport('t',x=>x);
      },{context});
      await work.link(spec=>spec.includes('runHealth')?health:i18n);await work.evaluate();
      const api=work.namespace.default;
      assert.equal(typeof api.refreshRunHealth,'function');
      assert.equal(typeof api.refreshWait,'function');
      assert.equal(typeof api.runWaitAction,'function');
      await api.refresh('chat-1');await api.refreshRunHealth('chat-1');
      await api.refreshWait('chat-1');
      uiLongTaskCallback({getEntries:()=>[{duration:80},{duration:250},{duration:120}]});
      await api.refresh('chat-1');
      assert.equal(ids['goal-work-ui-lag'].hidden,false);
      assert.match(ids['goal-work-ui-lag']['aria-label'],/UI long tasks: 3, maximum 250 ms/);
      run={...run,health_metrics:{budget_warnings:[{resource:'model_tokens',used:800,limit:1000,soft_limit:800}]}};
      await api.refreshRunHealth('chat-1');
      assert.equal(ids['goal-work-budget-warning'].hidden,false);
      assert.match(ids['goal-work-budget-warning'].textContent,/model tokens 800\/1000/);
      assert.equal(ids['goal-work-budget-warning-indicator'].hidden,false);
      assert.match(ids['goal-work-budget-warning-indicator']['aria-label'],/Resource budget approaching/);
      assert.equal(ids['wait-mode-status'].hidden,false);
      assert.equal(ids['wait-run-id'].textContent,'run-1');
      assert.equal(ids['wait-child-id'].textContent,'child-1');
      assert.equal(ids['wait-phase'].textContent,'tool');
      wait={...wait,endpoint_id:null,endpoint_label:null,selected_endpoint_label:'model.example:1234'};
      await api.refreshWait('chat-1');
      assert.equal(ids['wait-endpoint'].textContent,'Selected now: model.example:1234');
      assert.equal(ids['wait-checkpoint'].textContent.includes('secret-marker'),false);
      assert.equal(ids['goal-work-health-indicator'].hidden,false);
      assert.match(ids['goal-work-health-detail'].textContent,/No verified progress/);
      assert.match(ids['goal-work-health-detail'].textContent,/Progress tracking capacity reached/);
      goal={...goal,status:'paused'};await api.refresh('chat-1');
      assert.equal(ids['goal-work-health-indicator'].hidden,true);
      assert.equal(ids['goal-work-health-detail'].hidden,true);
      assert.equal(ids['goal-work-ui-lag'].hidden,true);
      assert.equal(ids['goal-work-budget-warning'].hidden,true);
      assert.equal(ids['goal-work-budget-warning-indicator'].hidden,true);
      run={...run,progress_health:{...run.progress_health,stalled:false}};
      goal={...goal,status:'active'};await api.refresh('chat-1');await api.refreshRunHealth('chat-1');
      assert.equal(ids['goal-work-health-indicator'].hidden,true);
      wait={...wait,phase:'approval',recovery_action:'answer'};await api.refreshWait('chat-1');
      assert.equal(ids['wait-phase'].textContent,'approval');
      assert.equal(ids['wait-action'].hidden,false);
      await api.runWaitAction();
      assert.match(ids['wait-recovery'].textContent,/Question card unavailable/);
      goal={...goal,status:'review_required'};await api.refresh('chat-1');
      wait={...wait,phase:'review',goal_status:'review_required',wait_reason:'repeated_premature_stop',recovery_action:'resume_goal'};
      await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Goal stalled/);
      assert.equal(ids['wait-phase'].textContent,'Review required');
      assert.equal(ids['goal-work-quick-resume'].hidden,false);
      await api.runWaitAction();
      assert.equal(goalResumeCalls,1);
      assert.equal(goal.status,'active');
      goal={...goal,status:'review_required'};await api.refresh('chat-1');
      wait={...wait,phase:'review',goal_status:'review_required',wait_reason:'repeated_action_observation',recovery_action:'resume_goal'};
      await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Repeated tool evidence cycle detected/);
      goal={...goal,status:'waiting_user'};await api.refresh('chat-1');
      wait={...wait,wait_reason:'provider_failure'};await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Model endpoint failed repeatedly/);
      await api.runWaitAction();
      assert.equal(goalResumeCalls,2);
      goal={...goal,status:'waiting_user'};await api.refresh('chat-1');
      wait={...wait,wait_reason:'context_compaction',failure_code:'summarizer_timeout',recovery_action:'inspect_context'};await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Context checkpoint failed/);
      assert.match(ids['wait-recovery'].textContent,/summarizer_timeout/);
      await api.runWaitAction();
      assert.equal(goalResumeCalls,2);
      assert.equal(contextClicks,1);
      goal={...goal,status:'waiting_user',checkpoint:{_wait_reason:'resource_budget'}};
      wait={...wait,wait_reason:'resource_budget',recovery_action:'resume_goal',
        budget:{resource:'tool_calls',used:2,limit:2,run_id:'a'.repeat(32)}};
      await api.refresh('chat-1');await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Tool-call budget reached: 2\/2/);
      wait={...wait,budget:{resource:'model_rounds',used:200,limit:200,run_id:'b'.repeat(32)}};
      await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Model-round budget reached: 200\/200/);
      wait={...wait,budget:{resource:'model_tokens',used:1200,limit:1000,usage_source:'estimated',run_id:'c'.repeat(32)}};
      await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Model-token budget reached: 1200\/1000/);
      assert.match(ids['wait-recovery'].textContent,/Estimated usage/);
      wait={...wait,budget:{resource:'model_requests',used:2,limit:2,run_id:'d'.repeat(32)}};
      await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Model-request budget reached: 2\/2/);
      wait={...wait,budget:{resource:'children',used:5,limit:5,run_id:'e'.repeat(32)}};
      await api.refreshWait('chat-1');
      assert.match(ids['wait-recovery'].textContent,/Child-agent budget reached: 5\/5/);
      effects=[{id:'effect-1',run_id:'run-1',tool_call_id:'round-1-tool-0',tool_name:'bash',action_hash:'a'.repeat(64),status:'unknown',revision:2}];
      goal={...goal,status:'waiting_user',checkpoint:{_wait_reason:'unknown_side_effect'}};
      wait={...wait,wait_reason:'unknown_side_effect',recovery_action:'inspect_effect'};
      await api.refresh('chat-1');await api.refreshWait('chat-1');await api.refreshEffects('chat-1');
      assert.equal(ids['goal-work-resume'].hidden,true);
      assert.equal(ids['wait-unknown-effects'].hidden,false);
      assert.equal(ids['wait-unknown-effects'].children.length,2);
      await api.verifyEffect(effects[0]);
      assert.equal(verifyCalls,1);
      assert.equal(effects[0].status,'verified_not_applied');
      assert.equal(ids['goal-work-resume'].hidden,true,'verified-not-applied must still require a decision');
      await api.authorizeEffectRetry(effects[0]);
      assert.equal(retryAuthorizeCalls,1);
      assert.equal(effects[0].status,'retry_authorized');
      assert.equal(ids['goal-work-resume'].hidden,false,'one-shot authorization allows explicit continuation');
      await api.chooseNoRetry(effects[0]);
      assert.equal(noRetryCalls,1);
      assert.equal(ids['goal-work-resume'].hidden,false,'no-retry unlocks explicit resume only');
      assert.equal(goal.status,'waiting_user','no-retry never auto-resumes the goal');
      effects=[{id:'effect-2',run_id:'run-2',tool_call_id:'round-1-tool-0',tool_name:'python',action_hash:'b'.repeat(64),status:'unknown',revision:2}];
      goal={...goal,status:'paused',checkpoint:{}};
      wait={...wait,phase:'paused',wait_reason:'goal_paused',recovery_action:'resume_goal'};
      blockedResume=true;await api.refresh('chat-1');await api.mutate('goal','resume');
      assert.equal(api.getSnapshot().goal.status,'paused','unknown effect must not silently resume');
      assert.equal(ids['wait-unknown-effects'].hidden,false,'blocked resume exposes the effect inbox');
      assert.equal(waitClasses.has('expanded'),true,'blocked resume opens the recovery panel');
      assert.equal(errorToasts.at(-1),'Review the tool effect before resuming.');
      effects=[];blockedResume=false;errorToasts=[];
      goal={...goal,status:'paused',checkpoint:{}};await api.refresh('chat-1');
      staleResume=true;await api.mutate('goal','resume');
      assert.equal(api.getSnapshot().goal.status,'active');
      assert.deepEqual(errorToasts,[],'a stale 409 is not an error if another client already resumed');
      wait={...wait,phase:'reconnect',recovery_action:'reconnect'};await api.refreshWait('chat-1');
      await api.runWaitAction();
      assert.equal(resumeCalls,1);assert.equal(reloadCalls,1);
      goal={...goal,status:'active',checkpoint:{}};await api.refresh('chat-1');
      run={...run,progress_health:{...run.progress_health,stalled:true}};
      delayOld=true;const stale=api.refreshRunHealth('chat-1');
      await api.refresh('chat-2');releaseOld();await stale;
      assert.equal(ids['goal-work-health-indicator'].hidden,true,'late result from prior chat must not leak into new chat');
      assert.equal(ids['wait-mode-status'].hidden,true);
      goal=null;wait={...wait,run_status:'done',goal_status:null,current_child:null};
      await api.refresh('chat-1');await api.refreshWait('chat-1');
      assert.equal(ids['wait-mode-status'].hidden,true,'finished idle chat must not keep a floating wait button');
      })().catch(error=>{console.error(error);process.exitCode=1});
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e", script, str(root)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
