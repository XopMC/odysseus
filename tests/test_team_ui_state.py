"""Team UI protocol safety, independent of app bootstrap or browser storage."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_team_event_cursor_and_explicit_external_consent():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    module = (Path(__file__).resolve().parents[1] / "static/js/team-workspace.js").as_uri()
    script = r"""
      import assert from 'node:assert/strict';
      const {createTeamEventCursor, createTeamNotificationTracker, terminalPlainText, normalizeTeamStart, reviewedGitFiles, fileRollbackArguments, replayTeamTimeline} = await import(process.argv[1]);
      const cursor=createTeamEventCursor('a',3);
      assert.equal(cursor.accept({team_id:'a',seq:4}),true);
      assert.equal(cursor.accept({team_id:'a',seq:4}),false);
      assert.equal(cursor.accept({team_id:'b',seq:5}),false);
      assert.equal(cursor.afterSeq,4);
      assert.equal(cursor.accept({seq:6}),false,'gaps require a canonical snapshot');
      assert.equal(cursor.needsSnapshot,true);
      cursor.reset('b',10);
      assert.equal(cursor.accept({team_id:'b',seq:11}),true);
      assert.equal(cursor.afterSeq,11);
      assert.deepEqual(reviewedGitFiles({files:['src/main.py','a b.txt','src/main.py'],truncated:false}),['src/main.py','a b.txt']);
      for(const files of [undefined,['../secret'],[':!all'],['*.py'],['a\n.py'],['/absolute']])assert.throws(()=>reviewedGitFiles({files}),/list|literal/i);
      assert.throws(()=>reviewedGitFiles({files:['main.py'],truncated:true}),/complete/i);
      const checkpoint={id:'cp1',status:'applied',files:[{path:'/work/main.py',after_exists:true,after_sha256:'b'.repeat(64)},{path:'/work/deleted.py',after_exists:false,after_sha256:null}]};
      assert.deepEqual(fileRollbackArguments(checkpoint),{checkpoint_id:'cp1',expected_sha256:{'/work/main.py':'b'.repeat(64),'/work/deleted.py':null}});
      assert.throws(()=>fileRollbackArguments({...checkpoint,status:'prepared'}),/applied/i);
      assert.throws(()=>fileRollbackArguments({...checkpoint,files:[{path:'/work/main.py',after_exists:true,after_sha256:null}]}),/hash/i);
      const notifications=createTeamNotificationTracker();
      assert.deepEqual(notifications.observe('a',{status:'done'}),[],'initial old result is not a new notification');
      assert.deepEqual(notifications.observe('a',{status:'running'}),[]);
      assert.equal(notifications.observe('a',{status:'blocked'}).length,1);
      assert.deepEqual(notifications.observe('a',{status:'blocked',delta:'unchanged status'}),[]);
      assert.equal(notifications.observe('a',{status:'done'}).length,1);
      assert.deepEqual(notifications.observe('b',{status:'done'}),[],'switching chat does not notify old history');
      assert.equal(notifications.observe('b',{status:'running',workers:[{id:'w',status:'waiting_approval'}]}).length,1);
      assert.deepEqual(notifications.observe('b',{status:'running',workers:[{id:'w',status:'waiting_approval'}]}),[]);
      notifications.reset();assert.deepEqual(notifications.observe('b',{status:'done'}),[],'reopening is only a baseline');
      assert.equal(terminalPlainText('\u001b[31m<script>x</script>\u001b[0m\u001b]0;evil\u0007'),'<'+'script>x</script>');
      const replay=replayTeamTimeline([
        {seq:1,type:'worker_message_started',payload:{worker_id:'w',message_id:'m1',model:'coder'}},
        {seq:2,type:'worker_delta',payload:{worker_id:'w',message_id:'m1',text:'first'}},
        {seq:3,type:'worker_message_completed',payload:{worker_id:'w',message_id:'m1',content:'first reply',tool_calls:[{name:'shell',arguments_preview:'pwd'}]}},
        {seq:4,type:'tool_result',payload:{worker_id:'w',tool:'shell',result:{exit_code:0,output:'/work'}}},
        {seq:5,type:'worker_message_started',payload:{worker_id:'w',message_id:'m2',model:'coder'}},
        {seq:6,type:'worker_delta',payload:{worker_id:'w',message_id:'m2',text:'second'}}
      ],[{id:'w',name:'Worker'}]);
      assert.equal(replay.length,2,'two saved model turns never merge after reload');
      assert.equal(replay[0].text,'first reply');assert.equal(replay[0].calls[0].name,'shell');assert.equal(replay[0].results[0].result.output,'/work');
      assert.equal(replay[1].text,'second');assert.equal(replay[1].complete,false);
      const local={endpoint_id:'jetson',model:'local',label:'Local',local:true};
      const paid={endpoint_id:'cloud',model:'paid',label:'Paid',local:false,api_key:'must-not-leak'};
      const draft={title:'Test',goal:'Build tests',project_path:'/work/project',leader:local,workers:[],config:{external:false}};
      assert.equal(normalizeTeamStart(draft,[local,paid]).leader.model,'local');
      const projectProfile={install_command:'npm ci',run_command:'npm run dev',test_command:'npm test',build_command:'npm run build',constraints:'Keep the API'};
      assert.deepEqual(normalizeTeamStart({...draft,config:{project_profile:projectProfile}},[local,paid]).config.project_profile,projectProfile,'all explicit project commands must survive the start draft');
      assert.throws(()=>normalizeTeamStart({...draft,leader:paid},[local,paid]),/external/i);
      assert.throws(()=>normalizeTeamStart({...draft,leader:paid,config:{external:true}},[local,paid]),/approval|price|budget/i);
      const approved={endpoint_id:'cloud',limit_microusd:10000,input_rate_per_million:100,output_rate_per_million:200,approved_context:'Only this project input',data_scope:'goal_only',consent:true};
      const payload=normalizeTeamStart({...draft,leader:paid,config:{external:true},budget_microusd:10000,external_approvals:[approved]},[local,paid]);
      assert.deepEqual(payload.leader,{endpoint_id:'cloud',model:'paid'});
      assert.equal(payload.external_approvals[0].data_scope,'goal_only');
      assert.throws(()=>normalizeTeamStart({...draft,external_approvals:[{...approved,data_scope:'everything'}]},[local,paid]),/scope/i);
      assert.throws(()=>normalizeTeamStart({...draft,workers:[{...paid,objective:'Review assigned code'}],config:{external:true},budget_microusd:10000,external_approvals:[approved]},[local,paid]),/scope/i);
      const assigned=normalizeTeamStart({...draft,workers:[{...paid,objective:'Review assigned code'}],config:{external:true},budget_microusd:10000,external_approvals:[{...approved,data_scope:'assigned_context'}]},[local,paid]);
      assert.equal(assigned.external_approvals[0].data_scope,'assigned_context');
      assert.equal(JSON.stringify(payload).includes('must-not-leak'),false);
      assert.throws(()=>normalizeTeamStart({...draft,leader:paid,config:{external:true},budget_microusd:10000,external_approvals:[{...approved,input_rate_per_million:null}]},[local,paid]),/price/i);
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(["node", "--input-type=module", "-e", script, module], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
