"""Rendered Team controls with deterministic owner-scoped API boundary fixtures.

Optional Playwright/Chromium layer; protocol tests run without browser packages.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_team_workspace_real_controls_and_session_isolation(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    probe = subprocess.run(["node", "-e", "require.resolve('playwright')"], capture_output=True, text=True)
    if probe.returncode:
        pytest.skip("Playwright is unavailable")
    root = Path(__file__).resolve().parents[1]
    script = r"""
      const {chromium}=require('playwright'), http=require('http'), fs=require('fs'), assert=require('node:assert/strict');
      const repo=process.argv[1],out=process.argv[2];
      const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Odysseus Team QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"></head><body><main class="chat-container" id="chat-container">
        <div id="chat-history">Ordinary chat</div><section id="team-workspace" hidden></section>
        <form id="chat-form"></form><div class="chat-input-bar"><div class="chat-input-top"><textarea id="message">Keep my draft</textarea></div>
        <div class="chat-input-bottom"><div class="chat-input-right"><div class="mode-toggle"><button class="mode-toggle-btn active" id="mode-agent-btn">Agent</button><button class="mode-toggle-btn" id="mode-chat-btn">Chat</button><button class="mode-toggle-btn" id="mode-team-btn" hidden>Team</button></div><button class="send-btn" type="submit" form="chat-form">Normal send</button></div></div></div></main>
        <script type="module">import {createTeamWorkspace} from '/static/js/team-workspace.js';
        window.normalSends=0;document.getElementById('chat-form').addEventListener('submit',e=>{e.preventDefault();window.normalSends++;});
        window.sid='chat-a';class Stream{static instances=[];constructor(url){this.url=url;Stream.instances.push(this);}close(){this.closed=true;}addEventListener(){}emit(event){this.onmessage?.({data:JSON.stringify(event)});}}
        class TestNotice{static permission='default';static permissionRequests=0;static shown=[];static async requestPermission(){this.permissionRequests++;this.permission='granted';return 'granted';}constructor(title,options){TestNotice.shown.push({title,...options});}}
        window.TestNotice=TestNotice;window.Stream=Stream;window.controller=createTeamWorkspace({getSessionId:()=>window.sid,EventSourceImpl:Stream,NotificationImpl:TestNotice,confirmImpl:()=>true});window.ready=controller.init();</script>
        </body></html>`;
      const server=http.createServer((req,res)=>{const url=new URL(req.url,'http://localhost');
        if(url.pathname.startsWith('/static/')){const file=repo+url.pathname;res.setHeader('Content-Type',file.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(file));}
        else{res.setHeader('Content-Type','text/html');res.end(html);}});
      await new Promise(r=>server.listen(0,'127.0.0.1',r));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      const errors=[],calls=[],tasks=new Map();let capability=false,uncertain=true;
      const terminals=[{id:'terminal-1',status:'running'}];
      const profiles=[{name:'Saved setup',profile:{project_path:'/work/saved',install_command:'npm ci',run_command:'npm run dev',test_command:'pytest saved',build_command:'make check',constraints:'Keep public API',trusted_host:true,external:true}}];
      const fileCheckpoints=[{id:'filecp1',status:'applied',created_at:1,files:[{path:'/work/project/main.py',before_exists:true,after_exists:true,before_sha256:'a'.repeat(64),after_sha256:'b'.repeat(64)},{path:'/work/project/deleted.py',before_exists:true,after_exists:false,before_sha256:'c'.repeat(64),after_sha256:null}]}];let rollbackConflict=true;
      const context=await browser.newContext({viewport:{width:1440,height:1000}});
      const model={endpoint_id:'jetson',model:'local-model',label:'Jetson / local-model',local:true};let completedMetric=null;
      await context.route('**/api/team/**',async route=>{const request=route.request(),url=new URL(request.url()),body=request.postDataJSON();calls.push({path:url.pathname,body,query:url.search});
        let result={};
        if(url.pathname==='/api/team/engineering/capabilities')result={enabled:capability,features:{context_policy:true}};
        else if(url.pathname.endsWith('/capabilities'))result={enabled:capability,host_enabled:true,engineering_enabled:true};
        else if(url.pathname==='/api/team/engineering/context-policy')result={last_completed_request:completedMetric,configured:false,valid:true,layers:[],revisions:{},sources:{},effective:{auto_compact:true,requested_window:0,output_reserve:4096,safety_tokens:1024,safety_percent:5,trigger_percent:75,target_percent:50,recent_groups:4,recent_tokens:2048,summary_tokens:1200,summary_timeout_seconds:150}};
        else if(url.pathname.endsWith('/models'))result={models:[model,{endpoint_id:'fixture-external',model:'fixture-model',label:'External fixture only',local:false}]};
        else if(url.pathname.endsWith('/presets'))result={presets:[{id:'coding',label:'Coding',config:{}}]};
        else if(url.pathname.endsWith('/profiles')){if(request.method()==='POST'){profiles.push(body);result=null;}else result={profiles};}
        else if(url.pathname.endsWith('/start')){const sid=url.pathname.split('/')[4];tasks.set(sid,{team_id:'team-'+sid,status:'running',metadata:{project_path:body.project_path},workers:[],last_seq:0});result=tasks.get(sid);}
        else if(url.pathname.includes('/session/'))result=tasks.get(url.pathname.split('/')[4])||{team_id:null,workers:[],last_seq:0};
        else if(url.pathname.endsWith('/workers')){const task=tasks.get('chat-a');task.workers.push({profile:body,id:'worker-1',status:'done',result:'Tests passed',artifacts:['src/main.py']});result=task;}
        else if(url.pathname.endsWith('/intents'))result={intents:uncertain?[{id:'intent1',name:'terminal.create',worker_id:'worker-1',status:'unknown',payload:{cwd:'/work/project'}}]:[]};
        else if(url.pathname.endsWith('/resolve')){uncertain=false;result=tasks.get('chat-a');}
        else if(url.pathname.endsWith('/artifacts'))result={artifacts:[{name:'Team result',data:{summary:'Verified test result',workspace:{integration:{id:'integration1',path:'/work/integration'}}}}]};
        else if(url.pathname.endsWith('/checkpoint'))result={checkpoint:{payload:{summary:'Durable checkpoint'}}};
        else if(url.pathname.endsWith('/host')){
          if(body.op==='terminal.create')result={id:'terminal-1',status:'running'};
          else if(body.op==='terminal.list')result={terminals};
          else if(body.op==='terminal.poll')result={id:'terminal-1',status:'running',output:body.args.offset?'':'\u001b[31m<img src=x onerror="window.injected=1">\u001b[0m\nready',next_offset:99};
          else if(body.op==='file.download')result={data_base64:Buffer.from('original source').toString('base64'),sha256:'hash-original'};
          else if(body.op==='file.upload')result={sha256:'hash-new',checkpoint_id:'filecp1'};
          else if(body.op==='file.checkpoint.list')result={checkpoints:fileCheckpoints};
          else if(body.op==='file.rollback'){if(rollbackConflict){await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'file changed after checkpoint; rollback refused'})});return;}fileCheckpoints[0].status='rolled_back';result={checkpoint_id:'filecp1',status:'rolled_back'};}
          else if(body.op==='file.call')result={output:'src/\nmain.py',entries:[{name:'main.py',path:'main.py',is_dir:false}]};
          else if(body.op==='git.diff')result={patch:'- old\n+ new',source_tree:'source1',worktree_tree:'work1',files:['main.py','docs/guide.md'],truncated:false};
          else if(body.op==='git.integrate')result={checkpoint_id:'checkpoint1'};
          result={ok:true,result};
        }
        await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(result)});
      });
      const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
      try{
        const base='http://127.0.0.1:'+server.address().port;await page.goto(base);await page.evaluate(()=>window.ready);
        assert.equal(await page.locator('#mode-team-btn').isVisible(),false,'feature off must hide Team');
        assert.equal(await page.evaluate(()=>TestNotice.permissionRequests),0);
        await page.getByRole('button',{name:'Normal send',exact:true}).click();assert.equal(await page.evaluate(()=>normalSends),1);
        capability=true;await page.reload();await page.evaluate(()=>window.ready);
        await page.locator('#mode-team-btn').click();
        assert.equal(await page.locator('.chat-input-bar').isVisible(),false,'old composer must not cover Team controls');
        assert.equal(await page.evaluate(()=>TestNotice.permissionRequests),0,'opening Team must not request notification permission');
        await page.getByText('Team tasks retain checkpoints.',{exact:false}).waitFor();
        assert.equal(await page.getByLabel('Team setup',{exact:true}).inputValue(),'basic');
        await page.getByLabel('Team setup',{exact:true}).selectOption('advanced');
        await page.getByText('Access permissions',{exact:true}).click();
        await page.getByRole('button',{name:'Enable browser notifications',exact:true}).click();
        assert.equal(await page.evaluate(()=>TestNotice.permissionRequests),1);
        await page.getByText('External endpoint consent and budget',{exact:true}).click();
        assert.equal(await page.getByLabel('External data scope',{exact:true}).inputValue(),'goal_only');
        await page.getByLabel('External data scope',{exact:true}).selectOption('assigned_context');
        assert.equal(await page.getByLabel('External data scope',{exact:true}).inputValue(),'assigned_context');
        assert.equal(await page.getByLabel('Approve this endpoint for this task',{exact:true}).isChecked(),false,'choosing a data scope never grants consent');
        await page.getByText('External endpoint consent and budget',{exact:true}).click();
        await page.getByLabel('Saved project profile',{exact:true}).selectOption('Saved setup');
        assert.equal(await page.getByLabel('Absolute project directory on the host').inputValue(),'','selecting a profile must not silently apply it');
        await page.getByRole('button',{name:'Load project profile',exact:true}).click();
        assert.equal(await page.getByLabel('Absolute project directory on the host').inputValue(),'/work/saved');
        assert.equal(await page.getByLabel('Test command',{exact:true}).inputValue(),'pytest saved');
        assert.equal(await page.getByLabel('Install command (optional)',{exact:true}).inputValue(),'npm ci');
        assert.equal(await page.getByLabel('Run command (optional)',{exact:true}).inputValue(),'npm run dev');
        assert.equal(await page.locator('.team-config').getByLabel('Trusted host: Unix user access',{exact:true}).isChecked(),false);
        assert.equal(await page.locator('.team-config').getByLabel('Allow explicitly approved external endpoints',{exact:true}).isChecked(),false);
        await page.getByRole('tabpanel',{name:'Tasks',exact:true}).getByLabel('Profile name',{exact:true}).fill('Copied setup');
        await page.getByRole('button',{name:'Save project profile',exact:true}).click();
        await page.waitForFunction(()=>document.querySelector('select[aria-label="Saved project profile"]')?.value==='Copied setup');
        const savedProfile=calls.find(x=>x.path.endsWith('/profiles')&&x.body?.name==='Copied setup');
        assert.deepEqual(Object.keys(savedProfile.body.profile).sort(),['build_command','constraints','install_command','project_path','run_command','test_command']);
        assert.equal(savedProfile.body.profile.install_command,'npm ci');assert.equal(savedProfile.body.profile.run_command,'npm run dev');
        assert(!calls.some(x=>x.path.endsWith('/host')),'loading or saving a profile must never execute commands');
        await page.screenshot({path:out+'/team-project-profile.png'});
        await page.getByLabel('Goal and completion criteria').fill('Implement and verify a small change');
        await page.getByLabel('Absolute project directory on the host').fill('/work/project');
        await page.getByLabel('Test command',{exact:true}).fill('pytest -q');
        await page.getByLabel('Endpoint and model').first().selectOption(JSON.stringify(['jetson','local-model']));
        await page.locator('#chat-form').evaluate(form=>form.requestSubmit());assert.equal(await page.evaluate(()=>normalSends),0,'Team must never submit to ordinary chat');
        await page.getByRole('button',{name:'Start team',exact:true}).click();await page.getByText('running',{exact:true}).first().waitFor();
        assert.equal(await page.getByRole('button',{name:'Load project profile',exact:true}).isVisible(),false,'running task profile cannot be replaced');
        const start=calls.find(x=>x.path.endsWith('/start'));assert.equal(start.body.config.project_profile.test_command,'pytest -q');
        assert.equal(start.body.config.project_profile.install_command,'npm ci');assert.equal(start.body.config.project_profile.run_command,'npm run dev');
        assert.equal(start.body.leader.model,'local-model');assert.equal(await page.locator('#message').inputValue(),'Keep my draft');
        await page.evaluate(async()=>{(await import('/static/js/i18n.js')).applyUiLanguage('ru')});
        await page.getByText('Действие с неизвестным результатом:',{exact:true}).waitFor();
        await page.getByRole('button',{name:'Зафиксировать результат завершения',exact:true}).click();
        await page.getByText('Проверьте результат, запишите подтверждения и отметьте согласие перед сверкой.',{exact:true}).waitFor();
        assert(!calls.some(x=>x.path.endsWith('/resolve')),'Russian controls cannot bypass evidence confirmation');
        assert((await page.locator('.team-card strong').allTextContents()).some(text=>text.includes('terminal.create')),'tool identity must not be translated');
        await page.evaluate(async()=>{(await import('/static/js/i18n.js')).applyUiLanguage('en')});
        await page.getByRole('button',{name:'Record completed outcome',exact:true}).click();
        assert(!calls.some(x=>x.path.endsWith('/resolve')),'uncertain action must not reconcile without human evidence');
        await page.getByLabel('Observed outcome and evidence').fill('Inspected the terminal; command completed with exit 0.');
        await page.getByLabel('I inspected the outcome; this is not permission to retry').check();
        await page.getByRole('button',{name:'Record completed outcome',exact:true}).click();
        assert(!calls.some(x=>x.path.endsWith('/resolve')),'completed action needs an observed exit code');
        await page.getByLabel('Observed exit code (required for completed actions)').fill('0');
        await page.getByRole('button',{name:'Record completed outcome',exact:true}).click();
        const resolution=calls.find(x=>x.path.endsWith('/resolve'));assert.equal(resolution.body.confirmation,true);assert.equal(resolution.body.status,'done');assert.equal(resolution.body.result.exit_code,0);
        await page.getByText('Team result',{exact:true}).waitFor();
        await page.getByRole('tab',{name:'Team',exact:true}).click();await page.getByLabel('Worker name').fill('Coder');
        await page.getByLabel('Objective',{exact:true}).fill('Implement a test');await page.getByLabel('Endpoint and model').nth(1).selectOption(JSON.stringify(['jetson','local-model']));
        await page.getByRole('button',{name:'Add worker',exact:true}).click();await page.getByText('Tests passed',{exact:true}).waitFor();
        const savedWorker=tasks.get('chat-a').workers[0], originalWorkerStatus=savedWorker.status;
        savedWorker.name='Save';savedWorker.profile.role='executor';savedWorker.profile.acceptance='Preserve running and Save';savedWorker.checkpoint_id='Save';
        await page.evaluate(async()=>{(await import('/static/js/i18n.js')).applyUiLanguage('ru')});
        const roleBox=await page.getByLabel('Роль',{exact:true}).evaluate(node=>{
          const css=getComputedStyle(node),probe=document.createElement('span');
          probe.textContent=node.selectedOptions[0].textContent;probe.style.cssText='position:absolute;visibility:hidden;white-space:pre';
          probe.style.font=css.font;document.body.append(probe);const labelHeight=probe.getBoundingClientRect().height;probe.remove();
          return {height:node.clientHeight,padding:parseFloat(css.paddingTop)+parseFloat(css.paddingBottom),labelHeight};
        });
        assert(roleBox.height-roleBox.padding>=roleBox.labelHeight,'Team select label must fit: '+JSON.stringify(roleBox));
        const routed=page.getByLabel('Эндпоинт и модель',{exact:true}).first();
        assert.equal(await routed.locator('option').filter({hasText:'Локальная модель · Jetson / local-model'}).count(),1);
        assert.equal(await routed.locator('option').filter({hasText:'Внешняя модель · External fixture only'}).count(),1);
        assert.equal(await routed.inputValue(),JSON.stringify(['jetson','local-model']),'locale changes must not change routing identity');
        for(const [status,label] of [['pending','ожидает запуска'],['planned','запланировано'],['queued','в очереди'],['running','выполняется'],['paused','приостановлено'],['waiting_approval','нужно участие пользователя'],['blocked','заблокировано'],['recovering','восстанавливается'],['cancelled','отменено'],['failed','ошибка'],['done','готово'],['accepted','принято'],['rejected','отклонено']]) {
          savedWorker.status=status;await page.evaluate(()=>controller.loadSnapshot());
          const teamPanel=page.getByRole('tabpanel',{name:'Команда',exact:true});
          assert.equal(await teamPanel.getByText(label,{exact:true}).count(),1,'worker state must be translated: '+status);
          assert.equal(await teamPanel.getByText('Save',{exact:true}).count(),1,'worker name stays verbatim');
          assert.equal(await teamPanel.locator('strong').getByText('исполнитель',{exact:true}).count(),1);
          assert.equal(await teamPanel.getByText('Критерии приёмки:',{exact:true}).count(),1);
          assert((await teamPanel.textContent()).includes('Preserve running and Save'));
          assert((await teamPanel.textContent()).includes('Контрольная точка: Save'));
          assert((await teamPanel.textContent()).includes('local-model'));
        }
        await page.getByRole('tabpanel',{name:'Команда',exact:true}).screenshot({path:out+'/team-worker-states-ru.png'});
        savedWorker.status=originalWorkerStatus;await page.evaluate(()=>controller.loadSnapshot());
        await page.evaluate(async()=>{(await import('/static/js/i18n.js')).applyUiLanguage('en')});
        // This loop intentionally exercised notification-worthy transitions; reset
        // the fixture's notification capture before the separate notification test.
        await page.evaluate(()=>{TestNotice.shown=[]});
        await page.getByRole('tab',{name:'Engineering',exact:true}).click();
        const contextScope=page.locator('[data-engineering="context-scope"]');
        await contextScope.selectOption('worker');
        await page.locator('[data-engineering="context-worker"]').selectOption('worker-1');
        await page.locator('[data-engineering="context-notice"]').filter({hasText:'Review values'}).waitFor();
        const scopeQuery=new URLSearchParams(calls.filter(call=>call.path.endsWith('/context-policy')).at(-1).query);
        assert.equal(scopeQuery.get('task_id'),'team-chat-a');assert.equal(scopeQuery.get('worker_id'),'worker-1');assert.equal(scopeQuery.get('project_id'),'');
        await page.getByRole('tab',{name:'Team',exact:true}).click();
        await page.getByRole('button',{name:'View checkpoint',exact:true}).click();await page.getByText('Durable checkpoint',{exact:false}).waitFor();
        await page.getByRole('button',{name:'accept',exact:true}).click();assert(calls.some(x=>x.path.endsWith('/workers/worker-1/accept')));
        await page.getByRole('tab',{name:'Terminals',exact:true}).click();await page.getByRole('button',{name:'New terminal',exact:true}).click();
        await page.getByLabel('Terminal output').getByText('ready',{exact:false}).waitFor();
        assert.equal(await page.evaluate(()=>window.injected),undefined);assert.equal(await page.getByLabel('Terminal output').locator('img').count(),0);
        await page.getByLabel('Terminal input',{exact:true}).fill('printf hello');await page.getByRole('button',{name:'Send input',exact:true}).click();
        assert(calls.some(x=>x.body?.op==='terminal.input'&&x.body.args.data==='printf hello\n'));
        await page.getByRole('button',{name:'Ctrl+C',exact:true}).click();assert(calls.some(x=>x.body?.op==='terminal.interrupt'));
        await page.getByLabel('Terminal input',{exact:true}).fill('Keep this unsent command');
        const beforeRemote=calls.filter(x=>x.body?.op==='terminal.list').length;
        terminals.push({id:'terminal-2',status:'running'});
        await page.evaluate(()=>{const event={team_id:'team-chat-a',seq:1,type:'host_changed',payload:{op:'terminal.create',scope:'team-chat-a',id:'terminal-2'}};Stream.instances.at(-1).emit(event);Stream.instances.at(-1).emit(event);});
        await page.waitForFunction(()=>document.querySelector('select[aria-label="Terminal"] option[value="terminal-2"]'),{},{timeout:3000});
        assert.equal(calls.filter(x=>x.body?.op==='terminal.list').length,beforeRemote+1,'new terminal event refreshes exactly once');
        assert.equal(await page.getByLabel('Terminal',{exact:true}).inputValue(),'terminal-1');
        assert.equal(await page.getByLabel('Terminal input',{exact:true}).inputValue(),'Keep this unsent command');
        terminals[1].status='stopped';
        await page.evaluate(()=>Stream.instances.at(-1).emit({team_id:'team-chat-a',seq:2,type:'host_changed',payload:{op:'terminal.stop',scope:'team-chat-a',id:'terminal-2'}}));
        await page.waitForFunction(()=>document.querySelector('select[aria-label="Terminal"] option[value="terminal-2"]')?.textContent.includes('stopped'));
        const beforeOtherScope=calls.filter(x=>x.body?.op==='terminal.list').length;
        await page.evaluate(()=>Stream.instances.at(-1).emit({team_id:'team-chat-a',seq:3,type:'host_changed',payload:{op:'terminal.create',scope:'worker-1',id:'worker-terminal'}}));
        await page.evaluate(()=>controller.loadSnapshot());
        assert.equal(calls.filter(x=>x.body?.op==='terminal.list').length,beforeOtherScope,'another host scope must not refresh this dropdown');
        assert.equal(await page.getByLabel('Terminal input',{exact:true}).inputValue(),'Keep this unsent command');
        await page.getByRole('tab',{name:'Files & Changes',exact:true}).click();await page.getByLabel('Path relative to project').fill('main.py');
        await page.getByRole('button',{name:'Open file',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.team-editor')?.value==='original source');
        await page.getByLabel('File content (UTF-8)').fill('updated source');await page.getByRole('button',{name:'Save file',exact:true}).click();
        assert(calls.some(x=>x.body?.op==='file.upload'&&x.body.args.expected_sha256==='hash-original'&&Buffer.from(x.body.args.data_base64,'base64').toString()==='updated source'));
        await page.getByLabel('Last file checkpoint').getByText('filecp1',{exact:false}).waitFor();
        await page.getByRole('button',{name:'List file checkpoints',exact:true}).click();
        const beforeRollbackReads=calls.filter(x=>x.body?.op==='file.download'||x.body?.op==='file.checkpoint.list').length;
        await page.getByRole('button',{name:'Restore file checkpoint',exact:true}).click();
        await page.getByText('file changed after checkpoint; rollback refused',{exact:true}).waitFor();
        const refusedRollback=calls.filter(x=>x.body?.op==='file.rollback');assert.equal(refusedRollback.length,1);
        assert.deepEqual(refusedRollback[0].body.args,{checkpoint_id:'filecp1',expected_sha256:{'/work/project/main.py':'b'.repeat(64),'/work/project/deleted.py':null}});
        assert.equal(calls.filter(x=>x.body?.op==='file.download'||x.body?.op==='file.checkpoint.list').length,beforeRollbackReads,'rollback must never refresh hashes or retry to override later edits');
        rollbackConflict=false;await page.getByRole('button',{name:'Restore file checkpoint',exact:true}).click();
        await page.getByLabel('Last file checkpoint').getByText('Restored file checkpoint filecp1.',{exact:false}).waitFor();
        assert.deepEqual(calls.filter(x=>x.body?.op==='file.rollback').at(-1).body.args,refusedRollback[0].body.args,'explicit retry retains displayed checkpoint hashes');
        await page.getByLabel('Worker worktree ID').fill('worktree1');await page.getByRole('button',{name:'Show diff',exact:true}).click();await page.getByText('- old\n+ new',{exact:true}).waitFor();
        await page.getByLabel('Include main.py',{exact:true}).waitFor({timeout:3000});
        assert.equal(await page.getByLabel('Include main.py',{exact:true}).isChecked(),true);
        assert.equal(await page.getByLabel('Include docs/guide.md',{exact:true}).isChecked(),true,'default includes every reviewed changed file');
        await page.getByLabel('Include main.py',{exact:true}).uncheck();await page.getByLabel('Include docs/guide.md',{exact:true}).uncheck();
        await page.getByRole('button',{name:'Integrate reviewed diff',exact:true}).click();assert(!calls.some(x=>x.body?.op==='git.integrate'),'empty selection cannot mean apply all');
        await page.getByLabel('Worker worktree ID').fill('other-worktree');
        assert.equal(await page.getByLabel('Include main.py',{exact:true}).count(),0,'changing worktree invalidates the reviewed hashes and file choices');
        await page.getByRole('button',{name:'Integrate reviewed diff',exact:true}).click();assert(!calls.some(x=>x.body?.op==='git.integrate'));
        await page.getByLabel('Worker worktree ID').fill('worktree1');await page.getByRole('button',{name:'Show diff',exact:true}).click();
        await page.getByLabel('Include main.py',{exact:true}).waitFor();
        await page.getByLabel('File content (UTF-8)').fill('changed since review');
        assert.equal(await page.getByLabel('Include main.py',{exact:true}).count(),0,'editing a file invalidates review');
        await page.getByRole('button',{name:'Show diff',exact:true}).click();await page.getByLabel('Include docs/guide.md',{exact:true}).uncheck();
        await page.screenshot({path:out+'/team-git-selection.png'});
        await page.getByRole('button',{name:'Integrate reviewed diff',exact:true}).click();
        const integration=calls.find(x=>x.body?.op==='git.integrate');assert.deepEqual(integration.body.args.paths,['main.py']);
        assert.equal(integration.body.args.expected_source_tree,'source1');assert.equal(integration.body.args.expected_worktree_tree,'work1');assert.equal(integration.body.args.id,'worktree1');
        await page.setViewportSize({width:390,height:844});await page.getByRole('tab',{name:'Terminals',exact:true}).click();
        await page.getByLabel('Terminal input',{exact:true}).fill('mobile input');await page.getByRole('button',{name:'Send input',exact:true}).click();
        assert(calls.some(x=>x.body?.op==='terminal.input'&&x.body.args.data==='mobile input\n'));
        await page.getByRole('button',{name:'Resize',exact:true}).click();assert(calls.some(x=>x.body?.op==='terminal.resize'));
        assert.equal(await page.locator('.chat-input-bar').isVisible(),false);await page.screenshot({path:out+'/team-terminal-mobile.png'});
        await page.setViewportSize({width:1440,height:1000});
        assert.equal(await page.evaluate(()=>TestNotice.shown.length),0,'initial and unchanged snapshots must be silent');
        tasks.get('chat-a').status='blocked';await page.evaluate(()=>controller.loadSnapshot());
        assert.equal(await page.evaluate(()=>TestNotice.shown.length),1);
        await page.evaluate(()=>controller.loadSnapshot());assert.equal(await page.evaluate(()=>TestNotice.shown.length),1,'unchanged blocked status must not notify again');
        tasks.get('chat-a').status='done';await page.evaluate(()=>controller.loadSnapshot());assert.equal(await page.evaluate(()=>TestNotice.shown.length),2);
        assert.equal(await page.evaluate(()=>Stream.instances.at(-1).closed),true,'terminal Team must close its SSE connection');
        await page.getByRole('button',{name:'Notifications on (this tab)',exact:true}).click();
        tasks.get('chat-a').status='blocked';await page.evaluate(()=>controller.loadSnapshot());assert.equal(await page.evaluate(()=>TestNotice.shown.length),2,'explicit off suppresses new notifications');
        await page.evaluate(()=>Stream.instances.at(-1).onerror());
        assert.equal(await page.locator('.team-notice').innerText(),'Reconnecting to team events…');
        await page.evaluate(()=>controller.loadSnapshot());
        await page.evaluate(()=>Stream.instances.at(-1).onopen());
        assert.equal(await page.locator('.team-notice').innerText(),'','successful reconnect must clear its stale notice');
        await page.getByRole('tab',{name:'Tasks',exact:true}).click();await page.screenshot({path:out+'/team-desktop.png'});
        await page.evaluate(async()=>{window.oldStream=Stream.instances.at(-1);oldStream.emit({team_id:'team-chat-a',seq:4,type:'worker.done'});oldStream.emit({team_id:'team-chat-a',seq:4,type:'worker.done'});});
        assert.equal(await page.evaluate(()=>controller.getState().after_seq),4);
        await page.getByRole('tab',{name:'Engineering',exact:true}).click();
        await page.locator('[data-engineering="context-override-trigger_percent"]').check();
        await page.locator('[data-engineering="context-trigger_percent"]').fill('70');
        completedMetric={seq:5,context_policy:{revisions:{},model:'live-observation',endpoint_id:'jetson',window:4096,input_budget:3000,message_tokens:900,schema_tokens:100,max_output_tokens:512}};
        await page.evaluate(()=>oldStream.emit({team_id:'team-chat-a',seq:5,type:'worker_metrics',payload:{worker_id:'worker-1'}}));
        await page.locator('[data-engineering="context-last-request"]').filter({hasText:'live-observation'}).waitFor();
        assert.equal(await page.locator('[data-engineering="context-trigger_percent"]').inputValue(),'70','live metrics cannot erase draft');
        completedMetric.context_policy.model='after-reconnect';
        await page.evaluate(()=>oldStream.onopen());
        await page.locator('[data-engineering="context-last-request"]').filter({hasText:'after-reconnect'}).waitFor();
        assert.equal(await page.locator('[data-engineering="context-trigger_percent"]').inputValue(),'70');
        await page.evaluate(async()=>{window.sid='chat-b';await controller.syncSession();oldStream.emit({team_id:'team-chat-a',seq:5,type:'host_changed',payload:{op:'terminal.create',scope:'team-chat-a',id:'stale-terminal'}});});
        assert.equal(await page.evaluate(()=>controller.getState().team_id),null);assert.equal(await page.evaluate(()=>oldStream.closed),true);
        assert.equal(await page.locator('[data-engineering="context-scope"] option[value="task"]').isDisabled(),true,'new chat cannot retain old task policy target');
        assert.equal(await page.locator('[data-engineering="context-worker"] option[value="worker-1"]').count(),0);
        assert.equal(await page.getByText('Tests passed',{exact:true}).count(),0,'old session event cannot populate current team');
        await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/team-mobile.png'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'mobile viewport must not overflow');
        assert((await page.locator('#team-workspace').boundingBox()).width>300,'mobile panel must retain readable width');
        assert.equal(await page.title(),'Odysseus Team QA');assert.equal(errors.length,0,errors.join('\n'));
        console.log(JSON.stringify({passed:true,calls:calls.length,screenshots:['team-desktop.png','team-mobile.png']}));
      }finally{await browser.close();await new Promise(r=>server.close(r));}
    """
    result = subprocess.run(["node", "-e", "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1);});", str(root), str(tmp_path)], text=True, capture_output=True, timeout=90, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True
