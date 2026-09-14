"""Browser check launch and observation: explicit authority, stable identities."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_check_runs_browser_explicit_launch_recovery_output_and_stop(tmp_path):
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Node/Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs'),crypto=require('node:crypto');
      const {chromium}=require('playwright'),repo=process.argv[1],out=process.argv[2],api='/api/team/engineering',calls=[],errors=[];
      const profile={id:'approved-A',project_id:'p1',name:'Save <b>profile</b>',command:"printf 'approved <b>command</b>'\n",revision:3};profile.command_hash=crypto.createHash('sha256').update(profile.command).digest('hex');
      const projects=[{id:'p1',name:'Project A',root:'/work/A',host_id:'jetson',access_mode:'trusted_host',revision:5},{id:'p2',name:'Project B',root:'/work/B',host_id:'mac',access_mode:'trusted_host',revision:2}];
      const operations=Array.from({length:55},(_,i)=>({id:'other-'+String(i).padStart(3,'0'),kind:'check_run',status:'completed',scope:{project_id:'p2',profile_id:'other',kind:'check',run_id:'other-run-'+i,cancellation_stops_host_command:false}}));
      operations.push(...Array.from({length:55},(_,i)=>({id:'terminal-'+String(i).padStart(3,'0'),kind:'check_run',status:'completed',scope:{project_id:'p1',profile_id:profile.id,kind:'check',run_id:'terminal-run-'+i,cancellation_stops_host_command:false}})));
      const old={id:'old-A',kind:'check_run',status:'running',scope:{project_id:'p1',profile_id:profile.id,kind:'check',run_id:'old-run',cancellation_stops_host_command:false}};operations.push(old);
      let run=null,dropFirst=true,missingOnce=true,enabled=false,holdOutput=false,pendingOutput,onHeld;
      const held=()=>new Promise(resolve=>onHeld=resolve);let log=Buffer.from('Начало <b>RUN</b>\n');
      const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Check runs QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>html,body{height:auto;overflow:visible}body{display:block}main{max-width:1000px;margin:auto;padding:12px}</style></head><body><main id="root"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';import * as locale from '/static/js/i18n.js';window.locale=locale;window.mount=()=>window.destroy=mountEngineeringWorkspace(document.getElementById('root'),{request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},...(options.body?{body:JSON.stringify(options.body)}:{})});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});mount();</script></body></html>`;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(repo+path));return;}
        if(path==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}if(path==='/favicon.ico'){res.end();return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({path,method:req.method,body,query:url.search});
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
        if(path===api+'/capabilities'){send({enabled:true,features:{projects:true,check_profiles:true,check_runs:enabled}});return;}
        if(path===api+'/hosts'){send({hosts:[]});return;}if(path===api+'/projects'){send({projects});return;}
        if(path.endsWith('/check-profiles')){send({profiles:path.includes('/p1/')?[profile]:[],next_cursor:null});return;}
        if(path===api+'/operations'){
          assert.equal(url.searchParams.get('kind'),'check_run');const activeOnly=url.searchParams.get('active_only')==='true',limit=activeOnly?1:50;assert.equal(url.searchParams.get('limit'),String(limit));
          const project=url.searchParams.get('project_id'),after=url.searchParams.get('after_id');
          let scoped=operations.filter(item=>!project||item.scope.project_id===project);
          if(activeOnly)scoped=scoped.filter(item=>['queued','running','cancel_requested'].includes(item.status)).reverse();
          if(after&&!scoped.some(item=>item.id===after)){send({detail:'Cursor is outside project'},404);return;}
          const start=after?scoped.findIndex(item=>item.id===after)+1:0,list=scoped.slice(start,start+limit);send({operations:list,next_cursor:start+limit<scoped.length?list.at(-1).id:null});return;
        }
        if(path===api+'/projects/p1/check-runs'&&req.method==='POST'){
          assert.deepEqual(Object.keys(body).sort(),['confirmation','expected_profile_revision','expected_project_revision','idempotency_key','kind','profile_id']);
          assert.equal(body.confirmation,true);assert.equal(body.profile_id,profile.id);assert.equal(body.kind,'baseline');assert.equal(body.expected_project_revision,5);assert.equal(body.expected_profile_revision,3);
          assert.match(body.idempotency_key,/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
          // A closed keep-alive socket may make Chromium transparently resend a POST.
          // A 503 deterministically exercises the UI's ambiguous-response handling.
          if(dropFirst){dropFirst=false;send({detail:'Submission outcome unavailable'},503);return;}
          let op=operations.find(item=>item.scope.idempotency_key===body.idempotency_key);
          if(!op){op={id:'live-A',kind:'check_run',status:'queued',scope:{project_id:'p1',profile_id:profile.id,kind:body.kind,run_id:'live-run',idempotency_key:body.idempotency_key,expected_project_revision:5,expected_profile_revision:3,cancellation_stops_host_command:false}};operations.unshift(op);run={run_id:'live-run',job_id:'mapped-job',status:'running'};}
          send(op);return;
        }
        if(path.startsWith(api+'/operations/')){
          const id=path.slice((api+'/operations/').length).split('/')[0],op=operations.find(item=>item.id===id);assert(op);
          if(path.endsWith('/cancel')){assert.deepEqual(body,{});op.status='cancel_requested';send(op);return;}
          if(op.status==='queued')op.status='running';else if(op.status==='cancel_requested')op.status='cancelled';send(op);return;
        }
        if(path.startsWith(api+'/projects/p1/check-runs/')){
          if(path.includes('old-run')){send(path.endsWith('/output')?{run_id:'old-run',job_id:'old-job',status:'passed',output_base64:'',offset:0,next_offset:0,truncated:false,notice:null}:{run_id:'old-run',job_id:'old-job',status:'passed',run:{status:'passed',evidence:{exit_code:0}}});return;}
          if(path.includes('terminal-run-')){send({run_id:path.split('/').at(-1),job_id:null,status:'passed'});return;}
          assert(run);
          if(path.endsWith('/stop')){assert.deepEqual(body,{confirmation:true});run.status='cancelled';send({run_id:run.run_id,job_id:run.job_id,status:'stop_requested'});return;}
          if(path.endsWith('/output')){
            assert.equal(url.searchParams.get('limit'),'16000');const offset=Number(url.searchParams.get('offset')),end=offset===0?5:log.length,bytes=log.subarray(offset,end);
            const data={run_id:run.run_id,job_id:run.job_id,status:run.status,output_base64:bytes.toString('base64'),offset,next_offset:end,truncated:false,notice:null};
            if(holdOutput){holdOutput=false;pendingOutput=()=>send(data);onHeld();onHeld=null;return;}send(data);return;
          }
          if(missingOnce){missingOnce=false;send({detail:'Not dispatched yet'},404);return;}send(run);return;
        }
        // Other-project history entries must never be observed from Project A.
        if(path.startsWith(api+'/projects/p2/check-runs/')){send({run_id:path.split('/').at(-1),job_id:null,status:'passed'});return;}
        send({detail:'Unexpected route'},404);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));page.setDefaultTimeout(10000);
        const origin='http://127.0.0.1:'+server.address().port;await page.goto(origin);const by=key=>page.locator('[data-engineering="'+key+'"]');
        await by('notice').filter({hasText:'No hosts are configured'}).waitFor();assert.equal(await by('check-runs').count(),1,'check run controls must exist');assert.equal(await by('check-runs').isHidden(),true);assert.equal(calls.some(call=>call.path===api+'/operations'),false);
        enabled=true;await page.evaluate(()=>{destroy();mount();});await by('notice').filter({hasText:'No hosts are configured'}).waitFor();await by('project').selectOption('p1');
        await by('check-profile').selectOption(profile.id);await by('run-more').waitFor();
        assert(calls.some(call=>call.path===api+'/operations'&&new URLSearchParams(call.query).get('project_id')==='p1'),'history requests must be filtered by the selected project');
        await by('run-history').locator('option[value="old-A"]').waitFor({state:'attached'});assert.equal(await by('run-history').inputValue(),'old-A','old active operation is restored beyond the recent fifty');
        assert.equal(await by('run-history').locator('option[value="other-000"]').count(),0);assert.equal(await by('run-history').locator('option[value="terminal-054"]').count(),0,'recent history remains paged');
        assert.equal(await by('run-history').locator('option').count(),52,'fifty recent rows plus the one old active row and placeholder');
        await by('run-history').selectOption('terminal-000');await by('run-check-status').filter({hasText:'Passed'}).waitFor();
        await by('run-refresh').click();await by('run-history-notice').filter({hasText:'Check run history loaded'}).waitFor();
        assert.equal(await by('run-history').inputValue(),'terminal-000','refresh must not steal explicit operation selection');
        await by('run-more').click();await by('run-history').locator('option[value="terminal-054"]').waitFor({state:'attached'});
        assert.equal(await by('run-history').inputValue(),'terminal-000');assert.equal(await by('run-history').locator('option').count(),57,'all fifty-six project operations remain reachable and deduplicated');
        assert.equal(await by('run-history').locator('option[value="old-A"]').count(),1);assert.equal(calls.some(call=>call.path.includes('/projects/p2/check-runs')),false);old.status='completed';
        assert.equal(await by('run-launch').isDisabled(),true);assert.equal(calls.some(call=>call.method==='POST'),false);
        await by('check-command').fill('printf UNSAVED');assert.equal(await by('run-confirm').isDisabled(),true);await by('check-command').fill(profile.command);
        await by('run-kind').selectOption('baseline');assert.equal(await by('run-launch-preview').textContent(),profile.command);
        await by('run-confirm').check();await by('run-launch').click();await by('run-notice').filter({hasText:'outcome is unknown'}).waitFor();
        assert.equal(calls.filter(call=>call.method==='POST').length,1,'ambiguous network failure must not auto POST');assert.equal(await by('run-confirm').isChecked(),false);
        await by('project').selectOption('p2');await by('project').selectOption('p1');await by('check-notice').filter({hasText:'Check profiles loaded'}).waitFor();
        assert.equal(await by('run-kind').inputValue(),'baseline');assert.equal(await by('run-launch').textContent(),'Retry the same check request','switching projects keeps the ambiguous request identity');
        await by('run-confirm').check();await by('run-launch').click();await by('run-operation-status').filter({hasText:'Running'}).waitFor();
        const launches=calls.filter(call=>call.path===api+'/projects/p1/check-runs');assert.equal(launches.length,2);assert.deepEqual(launches[0].body,launches[1].body,'explicit retry keeps exact body and idempotency key');
        await by('run-check-status').filter({hasText:'Running'}).waitFor();await by('run-output').filter({hasText:'Начало <b>RUN</b>'}).waitFor();assert.equal(await by('run-output').textContent(),'Начало <b>RUN</b>\n');assert.equal(await page.locator('main b').count(),0);
        // Refresh while an old poll's output page is in flight. Only one copy may append.
        log=Buffer.concat([log,Buffer.from('NEXT <i>chunk</i>\n')]);holdOutput=true;let outputHeld=held();await outputHeld;
        const refreshed=page.waitForResponse(response=>response.url()===origin+api+'/projects/p1/check-runs/live-run');
        await by('run-refresh').click();await refreshed;
        // Chromium can coalesce identical pending GETs. Release only after the
        // refresh has reached its new poll, rather than waiting for that cache lock.
        const superseded=page.waitForResponse(response=>response.url().includes('/p1/check-runs/live-run/output'));pendingOutput();pendingOutput=null;await superseded;
        await by('run-output').filter({hasText:'NEXT <i>chunk</i>'}).waitFor();await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal(await by('run-output').textContent(),log.toString(),'superseded poll cannot append duplicate output');
        assert.equal((await by('run-notice').textContent()).includes('Unable to observe'),false,'superseded output must not cause an offset error');
        await by('run-cancel-operation').click();await by('run-operation-status').filter({hasText:'Cancelled'}).waitFor();assert.equal(await by('run-check-status').textContent(),'Running');assert.equal(run.status,'running','cancelling operation does not stop host command');
        const writesBefore=calls.filter(call=>call.method==='POST').length;await page.reload();await by('notice').filter({hasText:'No hosts are configured'}).waitFor();await by('project').selectOption('p1');
        await by('run-check-status').filter({hasText:'Running'}).waitFor();assert.equal(calls.filter(call=>call.method==='POST').length,writesBefore,'reload restores observation only');
        await page.evaluate(()=>locale.applyUiLanguage('ru'));assert.equal(await by('run-stop-command').textContent(),'Остановить команду на хосте');assert.equal(await by('run-check-status').textContent(),'Выполняется');
        assert((await by('run-copy-limits').textContent()).includes('а не в песочнице'));
        assert((await by('run-copy-limits').textContent()).includes('128 МиБ'));
        await by('check-runs').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/check-runs-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await by('run-output').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/check-runs-mobile.png',animations:'disabled'});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await page.evaluate(()=>locale.applyUiLanguage('en'));assert((await by('run-copy-limits').textContent()).includes('not a sandbox'));await by('run-stop-command').click();await by('run-check-status').filter({hasText:'Cancelled'}).waitFor();
        assert.deepEqual(calls.filter(call=>call.path.endsWith('/stop')).map(call=>call.body),[{confirmation:true}]);assert.equal(await by('run-stop-command').isDisabled(),true);
        // A delayed output response from A cannot leak into B after a project switch.
        holdOutput=true;const waitHeld=held();await by('run-refresh').click();await waitHeld;await by('project').selectOption('p2');
        const late=page.waitForResponse(response=>response.url().includes('/p1/check-runs/live-run/output'));pendingOutput();await late;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal((await by('run-output').textContent()).includes('RUN'),false);assert.equal(await by('run-confirm').isChecked(),false);
        assert.equal(calls.filter(call=>call.path===api+'/projects/p1/check-runs').length,2);assert(calls.filter(call=>call.method==='POST').every(call=>call.path===api+'/projects/p1/check-runs'||call.path===api+'/operations/live-A/cancel'||call.path===api+'/projects/p1/check-runs/live-run/stop'));
        assert.equal(await page.title(),'Check runs QA');assert.equal(page.url(),origin+'/');assert.deepEqual(errors,[]);console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=120, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
