"""Context-policy browser behavior at the authenticated HTTP request seam."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_context_policy_scoped_edit_validation_conflict_and_locale(tmp_path):
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Node/Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const {chromium}=require('playwright'),fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
      const repo=process.argv[1],out=process.argv[2],api='/api/team/engineering',calls=[],errors=[],events=[];
      const foundationCopy=[
        ['Foundation: register projects, review host access policy and inspect the tool catalog. Isolated execution and the full engineering workflow are not available here yet.','Базовый этап: регистрация проектов, просмотр политики доступа к хосту и каталога инструментов. Изолированное выполнение и полный рабочий процесс разработки здесь пока недоступны.'],
        ['The selected Engineering project applies to NEW Team runs only. Existing and legacy tasks keep their current settings. Choose Legacy / no Engineering project to start without this binding.','Выбранный проект разработки применяется только к НОВЫМ командным задачам. Существующие задачи сохраняют свои настройки. Чтобы начать без этой привязки, выберите «Прежний режим / без проекта разработки».'],
        ['Choose an existing folder on a configured host. Registration does not create a folder or grant execution access.','Выберите существующую папку на настроенном хосте. Регистрация не создаёт папку и не разрешает выполнение команд.'],
        ['Trusted-host access permits tools to act on the real host under its configured permissions. It is not a sandbox. Review the selected host and folder before confirming.','Доступ к доверенному хосту позволяет инструментам работать на реальном хосте в пределах настроенных разрешений. Это не изолированная среда. Перед подтверждением проверьте выбранный хост и папку.'],
      ];
      const defaults={auto_compact:true,requested_window:0,output_reserve:4096,safety_tokens:1024,safety_percent:5,trigger_percent:75,target_percent:50,recent_groups:4,recent_tokens:2048,summary_tokens:1200,summary_timeout_seconds:150};
      const layers={owner:{revision:2,overrides:{output_reserve:8192,trigger_percent:60,target_percent:40}},'project:p1':{revision:3,overrides:{target_percent:65}}};let failNext=false,holdProject=false,releaseProject;
      let catalog='empty',capabilitiesError=false,completedObservation=null,failObservation=false,failHistory=false;
      let markProjectReady;const projectReady=new Promise(resolve=>markProjectReady=resolve);
      const read=(id,task='',worker='',session='')=>{
        const scopes=id?['owner','project:'+id]:['owner'],effective={...defaults},sources=Object.fromEntries(Object.keys(defaults).map(key=>[key,'default'])),revisions={},resolved=[];
        if(task)scopes.push('task:'+task);if(worker)scopes.push('worker:'+JSON.stringify([task,worker]));
        if(session)scopes.push('session:'+session);
        for(const scope of scopes){const row=layers[scope]||{revision:0,overrides:{}};Object.assign(effective,row.overrides);for(const key of Object.keys(row.overrides))sources[key]=scope;revisions[scope]=row.revision;resolved.push({scope,...row});}
        const valid=effective.target_percent<effective.trigger_percent;
        return {configured:true,effective,sources,revisions,layers:resolved,valid,last_completed_request:task==='task-a'&&worker==='w2'?completedObservation:null,validation_error:valid?null:'target_percent must be smaller than trigger_percent'};
      };
      const html=`<!doctype html><html><head><title>Context policy QA</title><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>html,body{height:auto;overflow:visible}body{display:block}main{max-width:1000px;margin:auto;padding:12px}</style></head><body><main id="root"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';import * as locale from '/static/js/i18n.js';window.locale=locale;mountEngineeringWorkspace(document.getElementById('root'),{request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},...(options.body?{body:JSON.stringify(options.body)}:{})});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});</script></body></html>`;
      let savedPreset=null;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css; charset=utf-8':'text/javascript; charset=utf-8');res.end(fs.readFileSync(repo+path));return;}
        if(path==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html.replace('mountEngineeringWorkspace(document','window.workspace=mountEngineeringWorkspace(document'));return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({path,method:req.method,body,query:url.search});
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
        if(path===api+'/capabilities'){if(capabilitiesError){send({detail:'Delete'},503);return;}send({enabled:true,features:{projects:true,policy:true,tool_catalog:true,context_policy:true}});return;}
        if(path===api+'/hosts'){send({hosts:[]});return;}if(path===api+'/projects'){send({projects:[{id:'p1',name:'Project <b>Save</b>',root:'/work/Save',host_id:'jetson',access_mode:null,revision:1}]});return;}
        if(path==='/qa/catalog'){catalog='items';send({ok:true});return;}if(path==='/qa/catalog-error'){catalog='error';send({ok:true});return;}if(path==='/qa/capabilities-error'){capabilitiesError=true;send({ok:true});return;}
        if(path===api+'/tools'){if(catalog==='error'){send({detail:'Delete'},503);return;}send({tools:catalog==='empty'?[]:[{id:'Save',name:'Available',available:true,effect:'Policy',reason:'Unavailable'},{id:'Delete',name:'Delete',available:false},{id:'raw',name:'Raw tool name',available:true}]});return;}
        if(path==='/qa/conflict'){failNext=true;send({ok:true});return;}
        if(path==='/qa/fail-observation'){failObservation=true;send({ok:true});return;}
        if(path===api+'/context-policy'&&req.method==='GET'&&failObservation){failObservation=false;send({detail:'Unavailable'},503);return;}
        if(path==='/qa/observation'){completedObservation={seq:123,created_at:123,context_policy:{effective:read('','task-a','w2').effective,revisions:read('','task-a','w2').revisions,source:'estimated',window:65536,input_budget:60000,message_tokens:1000,schema_tokens:500,max_output_tokens:4096,endpoint_id:'fixture-endpoint',model:'fixture-model',summary_request:false}};send({ok:true});return;}
        if(path==='/qa/hold-project'){holdProject=true;send({ok:true});return;}
        if(path==='/qa/release-project'){releaseProject();send({ok:true});return;}
        if(path===api+'/context-policy'&&req.method==='GET'){if(holdProject&&url.searchParams.get('project_id')){holdProject=false;releaseProject=()=>send(read('p1'));markProjectReady();return;}send(read(url.searchParams.get('project_id'),url.searchParams.get('task_id'),url.searchParams.get('worker_id'),url.searchParams.get('session_id')));return;}
        if(path===api+'/context-policy'&&req.method==='POST'){
          assert.deepEqual(Object.keys(body).filter(key=>key!=='session_id').sort(),['expected_revisions','overrides','project_id','task_id','worker_id']);
          if(failNext){failNext=false;layers.owner.revision++;send({detail:'Context policy changed; reload before saving'},409);return;}
          assert.deepEqual(body.expected_revisions,read(body.project_id,body.task_id,body.worker_id,body.session_id).revisions);
          const scope=body.session_id?'session:'+body.session_id:body.worker_id?'worker:'+JSON.stringify([body.task_id,body.worker_id]):body.task_id?'task:'+body.task_id:body.project_id?'project:'+body.project_id:'owner';layers[scope]={revision:(layers[scope]?.revision||0)+1,overrides:body.overrides};
          const result=read(body.project_id,body.task_id,body.worker_id,body.session_id);assert.equal(result.valid,true);events.push({seq:events.length+1,scope,revision:layers[scope].revision,overrides:body.overrides,created_at:123});send(result);return;
        }
        if(path===api+'/context-policy/events'){if(failHistory){failHistory=false;send({detail:'History unavailable'},503);return;}const after=Number(url.searchParams.get('after_seq'));send({events:events.filter(event=>event.seq>after),next_cursor:events.length});return;}
        if(path===api+'/context-presets'&&req.method==='GET'){send({items:savedPreset&&savedPreset.name.toLowerCase().includes((url.searchParams.get('query')||'').toLowerCase())?[savedPreset]:[],next_cursor:null});return;}
        if(path===api+'/context-presets'&&req.method==='POST'){savedPreset={id:'preset-1',name:body.name,values:body.values,kind:body.kind||'full',revision:(savedPreset?.revision||0)+1};send(savedPreset);return;}
        if(path===api+'/context-presets/preset-1'&&req.method==='PATCH'){assert.deepEqual(Object.keys(body).sort(),['expected_revision','name']);savedPreset={...savedPreset,name:body.name,revision:savedPreset.revision+1};send(savedPreset);return;}
        if(path===api+'/context-presets/preset-1'&&req.method==='DELETE'){savedPreset=null;send({deleted:true});return;}
        send({detail:'Unknown route'},404);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));
        await page.goto('http://127.0.0.1:'+server.address().port);const by=key=>page.locator('[data-engineering="'+key+'"]');
        await by('notice').filter({hasText:'No hosts are configured'}).waitFor();assert.equal(await by('context-policy').count(),1,'context policy controls must exist behind capability');
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-output_reserve"]')?.value==='8192');
        assert.equal(await page.title(),'Context policy QA');assert.equal(await by('context-scope').inputValue(),'owner');assert.equal(calls.some(call=>call.method==='POST'),false);
        assert.equal(await page.locator('[data-context-field]').count(),11);assert.equal(await by('context-source-output_reserve').textContent(),'Owner defaults');
        const originalTrigger=await by('context-trigger_percent').inputValue();
        await by('context-preset').selectOption('long');
        assert.equal(await by('context-trigger_percent').inputValue(),originalTrigger,'selecting only previews');
        assert((await by('context-preset-preview').textContent()).includes('65'));
        await by('context-preset-apply').click();
        assert.equal(await by('context-trigger_percent').inputValue(),'65');
        assert.equal(await by('context-output_reserve').inputValue(),'8192','preset preserves output reserve');
        assert.equal(calls.some(call=>call.method==='POST'),false,'draft preset never saves or invokes model');
        await by('context-reload').click();await by('context-notice').filter({hasText:'Review values'}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),originalTrigger,'reload discards unsaved preset');
        await by('context-output_reserve').fill('-1');await by('context-save').click();
        await by('context-notice').filter({hasText:'Use a whole number'}).waitFor();assert.equal(calls.some(call=>call.method==='POST'),false);
        await by('context-output_reserve').fill('4096');await page.evaluate(()=>fetch('/qa/conflict'));await by('context-save').click();
        await by('context-notice').filter({hasText:'changed elsewhere'}).waitFor();assert.equal(await by('context-output_reserve').inputValue(),'4096','409 preserves unsaved draft');assert.equal(await by('context-save').isDisabled(),true);
        const postsAfterConflict=calls.filter(call=>call.method==='POST').length;await by('context-reload').click();
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-output_reserve"]').value==='8192');
        assert.equal(calls.filter(call=>call.method==='POST').length,postsAfterConflict,'reload cannot retry save');await by('context-output_reserve').fill('4096');await by('context-save').click();
        await by('context-notice').filter({hasText:'Context policy saved'}).waitFor();assert.equal(layers.owner.overrides.output_reserve,4096);
        await page.reload();await page.waitForFunction(()=>document.querySelector('[data-engineering="context-output_reserve"]')?.value==='4096');
        await by('project').selectOption('p1');await page.evaluate(()=>fetch('/qa/hold-project'));await by('context-scope').selectOption('project');
        await by('context-notice').filter({hasText:'Loading context policy'}).waitFor();
        await projectReady;
        await by('context-scope').selectOption('owner');await page.waitForFunction(()=>document.querySelector('[data-engineering="context-output_reserve"]').disabled===false);
        const delayedProject=page.waitForResponse(response=>response.url().includes('/context-policy?project_id=p1'));
        await page.evaluate(()=>fetch('/qa/release-project'));await delayedProject;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal(await by('context-scope').inputValue(),'owner');assert.equal(await by('context-target_percent').inputValue(),'40','late project response cannot replace owner policy');
        await by('context-scope').selectOption('project');
        await by('context-validation').filter({hasText:'Target percentage must be lower'}).waitFor();assert.equal(await by('context-target_percent').inputValue(),'65');assert.equal(await by('context-source-target_percent').textContent(),'Project override');
        await by('context-target_percent').fill('45');await by('context-save').click();await by('context-notice').filter({hasText:'Context policy saved'}).waitFor();assert.deepEqual(layers['project:p1'].overrides,{target_percent:45});
        await by('context-reset').click();await by('context-notice').filter({hasText:'Context policy saved'}).waitFor();assert.deepEqual(layers['project:p1'].overrides,{});
        assert.equal(await by('context-target_percent').inputValue(),'40');assert.equal(await by('context-override-target_percent').isChecked(),false);assert.equal(await by('context-source-target_percent').textContent(),'Owner defaults');
        await by('context-advanced').locator('summary').click();await by('context-override-summary_timeout_seconds').check();await by('context-summary_timeout_seconds').fill('151');
        await by('context-save').click();await by('context-notice').filter({hasText:'Context policy saved'}).waitFor();assert.deepEqual(layers['project:p1'].overrides,{summary_timeout_seconds:151});
        for(const auto_compact of [true,false])events.push({seq:events.length+1,scope:'owner',revision:events.length+1,overrides:{auto_compact},created_at:123});
        await by('context-events-refresh').click();await by('context-events').getByText('Disabled',{exact:true}).waitFor();
        assert.equal(await by('context-events').getByText('Enabled',{exact:true}).count(),1);
        await page.evaluate(()=>locale.applyUiLanguage('ru'));assert.equal(await by('context-save').textContent(),'Сохранить политику контекста');assert.equal(await by('context-source-target_percent').textContent(),'Настройки пользователя');
        assert.equal(await by('context-events').getByText('Включено',{exact:true}).count(),1);
        assert.equal(await by('context-events').getByText('Отключено',{exact:true}).count(),1);
        assert.equal(/\b(?:true|false|Enabled|Disabled)\b/.test(await by('context-events').textContent()),false);
        assert.equal(await by('context-preset').locator('option[value="long"]').textContent(),'Долгая агентская задача');
        assert.equal(await by('context-preset-apply').textContent(),'Внести пресет в черновик');
        assert.equal(await page.getByText('Availability is reported by the server for the selected project. This catalog does not execute tools.',{exact:true}).count(),0);
        assert.equal(await page.getByText('Доступность для выбранного проекта сообщает сервер. Этот каталог не запускает инструменты.',{exact:true}).count(),1);
        assert.equal(await by('tools').textContent(),'Для этого проекта инструменты не указаны.');
        for(const [english,russian] of foundationCopy){assert.equal(await page.getByText(english,{exact:true}).count(),0,'authored foundation copy must leave English');assert.equal(await page.getByText(russian,{exact:true}).count(),1);}
        assert.equal(await by('binding-notice').textContent(),foundationCopy[1][1]);
        assert.equal(await by('notice').textContent(),'Проверьте политику выбранного проекта.');
        assert((await by('project-details').textContent()).includes('Хост: jetson'));assert((await by('project-details').textContent()).includes('Папка: /work/Save'));
        assert((await by('project-details').textContent()).includes('Политика: Только чтение — доступ к доверенному хосту не подтверждён'));
        assert((await by('project-details').textContent()).includes('Project <b>Save</b>'),'project names stay unchanged');
        await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:out+'/engineering-foundation-ru-desktop.png',animations:'disabled'});
        assert.equal(await by('context-scope').locator('option[value="owner"]').textContent(),'Настройки пользователя');assert.equal(await page.locator('main b').count(),0);
        assert(await by('context-override-target_percent').evaluate(node=>node.getBoundingClientRect().width<32),'override checkbox must not occupy a full input-width column');
        await by('context-validation').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-policy-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await by('context-save').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-policy-mobile.png',animations:'disabled'});
        await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:out+'/engineering-foundation-ru-mobile.png',animations:'disabled'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await by('context-summary_timeout_seconds').fill('0');await by('context-save').click();await by('context-notice').filter({hasText:'Введите целое число'}).waitFor();
        await page.evaluate(()=>fetch('/qa/catalog'));await by('project').selectOption('');
        assert.equal(await by('project-details').textContent(),'Проект разработки не выбран. Новые командные задачи используют прежние настройки.');
        await by('project').selectOption('p1');await by('tools').locator('strong').filter({hasText:'Raw tool name'}).waitFor();
        assert.deepEqual(await by('tools').locator('strong').allTextContents(),['Available','Delete','Raw tool name'],'tool names bypass the UI catalog');
        assert.equal(await by('tools').getByText('Unavailable',{exact:true}).count(),1,'server reason stays verbatim');
        assert.equal(await by('tools').getByText('Policy',{exact:true}).count(),1,'server effect stays verbatim');
        assert.equal(await by('tools').getByText('Недоступно',{exact:true}).count(),1);assert.equal(await by('tools').getByText('Доступно',{exact:true}).count(),2);
        assert.equal(await by('tools').getByText('Воздействие не указано',{exact:true}).count(),2);
        assert.equal(await by('tools').getByText('Сервер не указал причину доступности или недоступности.',{exact:true}).count(),1);
        assert.equal(await by('tools').getByText('Доступно согласно политике этого проекта.',{exact:true}).count(),1);
        await page.evaluate(()=>fetch('/qa/catalog-error'));await by('project').selectOption('');await by('project').selectOption('p1');
        await by('tools').filter({hasText:'Не удалось загрузить инструменты: Delete'}).waitFor();
        await page.evaluate(()=>locale.applyUiLanguage('en'));assert.equal(await by('context-save').textContent(),'Save context policy');
        for(const [english,russian] of foundationCopy){assert.equal(await page.getByText(english,{exact:true}).count(),1);assert.equal(await page.getByText(russian,{exact:true}).count(),0);}
        assert((await by('project-details').textContent()).includes('Folder: /work/Save'));assert((await by('project-details').textContent()).includes('Project <b>Save</b>'));
        assert.equal(await by('tools').textContent(),'Unable to load tools: Delete');
        await by('context-preset').selectOption('compact');await by('context-preset-apply').click();
        await by('context-save').click();await by('context-notice').filter({hasText:'Context policy saved'}).waitFor();
        assert.equal(layers['project:p1'].overrides.trigger_percent,60);assert.equal(layers['project:p1'].overrides.target_percent,35);
        assert.equal(await by('context-scope').locator('option[value="task"]').isDisabled(),true);
        await page.evaluate(()=>workspace.setTaskContext({id:'task-a',name:'Task <b>Save</b>',workers:[{id:'w1',name:'Writer'},{id:'w2',name:'Reviewer'}]}));
        await by('context-scope').selectOption('task');await by('context-notice').filter({hasText:'Review values'}).waitFor();
        await by('context-override-trigger_percent').check();await by('context-trigger_percent').fill('70');
        await by('context-save').click();await by('context-notice').filter({hasText:'Context policy saved'}).waitFor();
        assert.deepEqual(layers['task:task-a'].overrides,{trigger_percent:70});
        assert.equal(calls.filter(call=>call.method==='POST').at(-1).body.project_id,'','saved task determines its project, not UI selection');
        await by('context-scope').selectOption('worker');assert.equal(await by('context-save').isDisabled(),true);
        await by('context-worker').selectOption('w1');await by('context-notice').filter({hasText:'Review values'}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),'70','worker inherits task');
        await by('context-override-trigger_percent').check();await by('context-trigger_percent').fill('65');
        await by('context-save').click();await by('context-notice').filter({hasText:'Context policy saved'}).waitFor();
        assert.deepEqual(layers['worker:["task-a","w1"]'].overrides,{trigger_percent:65});
        await by('context-worker').selectOption('w2');await by('context-notice').filter({hasText:'Review values'}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),'70','sibling does not inherit worker override');
        assert.equal(await by('context-override-trigger_percent').isChecked(),false);
        await by('context-override-trigger_percent').check();await by('context-trigger_percent').fill('68');
        await page.evaluate(()=>workspace.setTaskContext({id:'task-a',name:'Task <b>Save</b>',workers:[{id:'w1',name:'Writer'},{id:'w2',name:'Reviewer renamed'}]}));
        assert.equal(await by('context-trigger_percent').inputValue(),'68','ordinary snapshot update preserves unsaved worker draft');
        assert.equal(await by('context-worker').inputValue(),'w2');
        await page.evaluate(()=>locale.applyUiLanguage('ru'));
        assert.equal(await by('context-scope').locator('option[value="task"]').textContent(),'Текущая командная задача');
        assert.equal(await by('context-scope').locator('option[value="worker"]').textContent(),'Выбранный исполнитель команды');
        const importInput=by('context-import-file'),profile=overrides=>({format:'odysseus-context-policy',version:1,overrides});
        const upload=async value=>importInput.setInputFiles({name:'profile.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(value))});
        const postsBeforeImport=calls.filter(call=>call.method==='POST').length;
        await upload({...profile({trigger_percent:67}),version:99});
        await by('context-notice').filter({hasText:'Формат или версия профиля'}).waitFor();
        assert.equal(await by('context-import-apply').isDisabled(),true);
        assert.equal(await by('context-trigger_percent').inputValue(),'68');
        await upload(profile({unknown_field:true}));
        await by('context-notice').filter({hasText:'неизвестные поля'}).waitFor();
        await upload(profile({target_percent:90}));
        await by('context-notice').filter({hasText:'Целевое заполнение'}).waitFor();
        assert.equal(await by('context-import-apply').isDisabled(),true);
        const downloadPromise=page.waitForEvent('download');await by('context-export').click();const download=await downloadPromise;
        assert.equal(download.suggestedFilename(),'odysseus-context-policy-v1.json');
        assert.deepEqual(JSON.parse(fs.readFileSync(await download.path(),'utf8')),profile({}),'export excludes unsaved draft and task identity');
        await upload(profile({trigger_percent:67,recent_groups:7}));
        await by('context-notice').filter({hasText:'Предпросмотр профиля загружен'}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),'68','import previews without editing');
        assert((await by('context-import-preview').textContent()).includes('68 → 67'));
        assert.equal(await by('context-import-choose').textContent(),'Выбрать файл профиля');
        assert.equal(await by('context-export').textContent(),'Экспортировать сохранённые переопределения');
        await by('context-import-choose').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-import-ru-mobile.png',animations:'disabled'});
        await page.setViewportSize({width:1440,height:1000});await by('context-import-choose').scrollIntoViewIfNeeded();
        await page.screenshot({path:out+'/context-import-ru-desktop.png',animations:'disabled'});await page.setViewportSize({width:390,height:844});
        await by('context-trigger_percent').fill('69');
        assert((await by('context-import-preview').textContent()).includes('69 → 67'),'preview follows draft edits');
        await by('context-import-apply').click();
        assert.equal(await by('context-trigger_percent').inputValue(),'67');
        assert.equal(calls.filter(call=>call.method==='POST').length,postsBeforeImport,'import and export never save or invoke a model');
        await by('context-save').click();await by('context-notice').filter({hasText:'Политика контекста сохранена'}).waitFor();
        assert.deepEqual(layers['worker:["task-a","w2"]'].overrides,{trigger_percent:67,recent_groups:7});
        assert.equal(layers['worker:["task-a","w1"]'].overrides.trigger_percent,65);
        await page.evaluate(()=>fetch('/qa/observation'));await by('context-reload').click();
        await by('context-last-request').filter({hasText:'использовал эту сохранённую версию'}).waitFor();
        assert((await by('context-last-request').textContent()).includes('1500'));
        assert((await by('context-last-request').textContent()).includes('fixture-model — fixture-endpoint'));
        await by('context-save').click();await by('context-notice').filter({hasText:'Политика контекста сохранена'}).waitFor();
        await by('context-last-request').filter({hasText:'отличается от использованной'}).waitFor();
        assert((await by('context-last-request').textContent()).includes('оценочное'));
        await by('context-trigger_percent').fill('69');
        await page.evaluate(()=>fetch('/qa/observation'));await page.evaluate(()=>workspace.refreshContextObservation());
        await by('context-last-request').filter({hasText:'использовал эту сохранённую версию'}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),'69','background observation cannot replace unsaved settings');
        await page.evaluate(()=>fetch('/qa/fail-observation'));await page.evaluate(()=>workspace.refreshContextObservation());
        await by('context-observation-error').filter({hasText:'Не удалось обновить'}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),'69');
        await page.evaluate(()=>workspace.refreshContextObservation());
        assert.equal(await by('context-observation-error').textContent(),'','successful refresh clears stale warning');
        layers['worker:["task-a","w2"]'].revision++;
        await page.evaluate(()=>workspace.refreshContextObservation());
        await by('context-notice').filter({hasText:'Политика контекста изменена'}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),'69');
        assert.equal(await by('context-save').isDisabled(),true,'remote changes require explicit reload, never overwrite draft');
        await by('context-last-request').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-last-request-ru.png',animations:'disabled'});
        await by('context-scope').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-worker-ru-mobile.png',animations:'disabled'});
        await page.setViewportSize({width:1440,height:1000});await by('context-scope').scrollIntoViewIfNeeded();
        await page.screenshot({path:out+'/context-worker-ru-desktop.png',animations:'disabled'});
        const beforeDetached=calls.filter(call=>call.method==='POST').length;
        await page.evaluate(()=>workspace.setTaskContext(null));assert.equal(await by('context-save').isDisabled(),true);
        assert.equal(await by('context-worker').inputValue(),'');assert.equal(calls.filter(call=>call.method==='POST').length,beforeDetached);
        assert.equal(layers.owner.overrides.trigger_percent,60,'task changes preserve owner');
        await page.evaluate(()=>locale.applyUiLanguage('en'));
        await page.evaluate(()=>fetch('/qa/capabilities-error'));await by('refresh').click();await by('notice').filter({hasText:'Delete'}).waitFor();
        await page.evaluate(()=>locale.applyUiLanguage('ru'));assert.equal(await by('notice').textContent(),'Delete','raw diagnostic never inherits a previous localized status binding');
        await page.evaluate(()=>locale.applyUiLanguage('en'));assert.equal(await by('notice').textContent(),'Delete');
        await by('context-scope').selectOption('owner');
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-trigger_percent"]').value==='60');
        await by('context-trigger_percent').fill('59');
        const backgroundPosts=calls.filter(call=>call.method==='POST').length;
        layers.owner.revision++;layers.owner.overrides.trigger_percent=61;
        events.push({seq:events.length+1,scope:'owner',revision:layers.owner.revision,overrides:{trigger_percent:61},created_at:124});
        // No Team event or explicit refresh: the visible panel checks its policy.
        await by('context-notice').filter({hasText:'changed elsewhere'}).waitFor({timeout:12000});
        await by('context-events').getByText('61',{exact:true}).waitFor();
        assert.equal(await by('context-trigger_percent').inputValue(),'59');
        assert.equal(await by('context-save').isDisabled(),true);
        assert.equal(calls.filter(call=>call.method==='POST').length,backgroundPosts);
        await page.evaluate(()=>locale.applyUiLanguage('ru'));
        await by('context-notice').filter({hasText:'Политика контекста изменена'}).waitFor();
        await by('context-notice').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-owner-remote-change-ru.png',animations:'disabled'});
        await by('context-reload').click();
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-trigger_percent"]').value==='61');
        failHistory=true;await by('context-events-refresh').click();
        await by('context-events').getByText('Не удалось загрузить историю политики.',{exact:true}).waitFor();
        await by('context-events').getByText('61',{exact:true}).waitFor({timeout:12000});
        assert.equal(await by('context-events').getByText('61',{exact:true}).count(),1,'history retry restores rows without duplicates');
        await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});
        const whileHidden=calls.filter(call=>call.path.startsWith(api+'/context-policy')).length;
        layers.owner.revision++;
        await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,5500)));
        assert.equal(calls.filter(call=>call.path.startsWith(api+'/context-policy')).length,whileHidden,'hidden page pauses policy and history checks');
        await page.evaluate(()=>{delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));});
        await by('context-notice').filter({hasText:'Политика контекста изменена'}).waitFor();
        await by('context-validation').filter({hasText:'Показанная политика устарела'}).waitFor();
        await by('context-reload').click();await by('context-save').waitFor({state:'visible'});
        await page.waitForFunction(()=>!document.querySelector('[data-engineering="context-save"]').disabled);
        const policyBeforeLibrary=JSON.stringify(layers);
        await by('context-library-name').fill('Мой профиль');await by('context-library-copy').click();
        await by('context-notice').filter({hasText:'Профиль сохранён'}).waitFor();
        assert.equal(JSON.stringify(layers),policyBeforeLibrary);
        await by('context-library-refresh').click();
        await page.waitForFunction(()=>!document.querySelector('[data-engineering="context-library-select"]').disabled);
        await by('context-library-select').selectOption('preset-1');
        await by('context-library-preview').click();
        await by('context-notice').filter({hasText:'Загружен предпросмотр профиля'}).waitFor();
        assert((await by('context-import-preview').textContent()).includes('61'));
        await by('context-library-name').fill('Переименованный');await by('context-library-update').click();
        await by('context-notice').filter({hasText:'Профиль сохранён'}).waitFor();
        assert.equal(savedPreset.name,'Переименованный');assert.equal(savedPreset.revision,2);
        const presetBeforeRename=JSON.stringify(savedPreset.values);
        await by('context-output_reserve').fill('9999');
        await by('context-library-name').fill('Только название');await by('context-library-rename').click();
        await by('context-notice').filter({hasText:'Профиль переименован'}).waitFor();
        assert.equal(savedPreset.name,'Только название');assert.equal(JSON.stringify(savedPreset.values),presetBeforeRename);
        assert.equal(await by('context-output_reserve').inputValue(),'9999','rename preserves draft');
        await by('context-library-search').fill('нет совпадений');await by('context-library-search').press('Enter');
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-library-select"]').options.length===1);
        await by('context-library-search').fill('ТОЛЬКО');await by('context-library-search').press('Enter');
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-library-select"]').options.length===2);
        await by('context-library-select').selectOption('preset-1');
        assert.equal(await by('context-output_reserve').inputValue(),'9999','search preserves draft');
        await by('context-library-kind').selectOption('overrides');await by('context-library-update').click();
        await by('context-notice').filter({hasText:'Профиль сохранён'}).waitFor();
        assert.equal(savedPreset.kind,'overrides');assert.deepEqual(savedPreset.values,layers.owner.overrides);
        await by('context-library-preview').click();
        await by('context-notice').filter({hasText:'В предпросмотре только сохранённые переопределения'}).waitFor();
        assert.equal(await by('context-output_reserve').inputValue(),'9999','partial preview preserves draft');
        await by('context-library').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-library-ru.png',animations:'disabled'});
        page.once('dialog',dialog=>dialog.accept());await by('context-library-delete').click();
        await by('context-notice').filter({hasText:'Профиль удалён'}).waitFor();
        assert.equal(savedPreset,null);assert.equal(JSON.stringify(layers),policyBeforeLibrary);
        await page.evaluate(()=>workspace.setSessionId('ordinary-a'));await by('context-scope').selectOption('session');
        await page.waitForFunction(()=>!document.querySelector('[data-engineering="context-save"]').disabled);
        await by('context-override-output_reserve').check();await by('context-output_reserve').fill('1536');await by('context-save').click();
        await by('context-notice').filter({hasText:'Политика контекста сохранена'}).waitFor();
        assert.equal(layers['session:ordinary-a'].overrides.output_reserve,1536);
        assert.deepEqual(layers.owner,JSON.parse(policyBeforeLibrary).owner,'chat save leaves owner policy unchanged');
        await page.evaluate(()=>workspace.setSessionId('ordinary-b'));
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-output_reserve"]').value==='4096');
        assert.equal(await by('context-override-output_reserve').isChecked(),false);
        await page.evaluate(()=>workspace.setSessionId('ordinary-a'));
        await page.waitForFunction(()=>document.querySelector('[data-engineering="context-output_reserve"]').value==='1536');
        await by('context-scope').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/context-chat-ru.png',animations:'disabled'});
        await page.evaluate(()=>workspace());
        const afterDestroy=calls.filter(call=>call.path.startsWith(api+'/context-policy')).length;
        await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,5500)));
        assert.equal(calls.filter(call=>call.path.startsWith(api+'/context-policy')).length,afterDestroy,'destroy removes background checks');
        const beforeDialog=calls.length;
        capabilitiesError=false; // Earlier failure scenario must not poison a fresh dialog.
        await page.evaluate(async()=>{window.currentDialogChat='ordinary-a';window.contextSavedCount=0;const {openContextSettings}=await import('/static/js/context-settings-dialog.js');openContextSettings({getSessionId:()=>window.currentDialogChat,onSaved:()=>window.contextSavedCount++});});
        const dialog=page.getByRole('dialog');await dialog.waitFor();
        assert.equal(await dialog.getByRole('heading',{name:'Настройки контекста',exact:true}).count(),1);
        try { await page.waitForFunction(()=>document.querySelector('dialog [data-engineering="context-output_reserve"]')?.value==='1536'); }
        catch(error) { throw new Error(error.message+'\n'+await dialog.textContent()); }
        assert.equal(await dialog.locator('[data-engineering="context-scope"]').inputValue(),'session');
        assert.deepEqual(await dialog.locator('[data-engineering="context-scope"] option').evaluateAll(nodes=>nodes.map(node=>node.value)),['owner','session']);
        assert.equal(await dialog.locator('[data-engineering="context-worker"]').count(),0);
        await dialog.locator('[data-engineering="context-output_reserve"]').fill('1792');
        await dialog.locator('[data-engineering="context-save"]').click();
        await page.waitForFunction(()=>window.contextSavedCount===1);
        assert.equal(layers['session:ordinary-a'].overrides.output_reserve,1792);
        assert.equal(await dialog.locator('[data-engineering="probe-panel"]').count(),0);
        assert.equal(calls.slice(beforeDialog).some(call=>['/hosts','/projects','/tools','/model-probe'].some(path=>call.path===api+path)),false);
        const retentionHelp=dialog.getByText('Минимальное число групп сообщений важнее токенового бюджета истории.',{exact:false});
        assert.equal(await retentionHelp.count(),1);
        assert.equal(await retentionHelp.isVisible(),false,'explanation is collapsed initially');
        await dialog.getByText('Правила сохранения истории',{exact:true}).click();
        await page.setViewportSize({width:1280,height:900});await retentionHelp.scrollIntoViewIfNeeded();
        await page.screenshot({path:out+'/context-retention-ru-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await retentionHelp.scrollIntoViewIfNeeded();
        assert(await retentionHelp.isVisible());
        assert(await retentionHelp.evaluate(node=>node.scrollWidth<=node.clientWidth),'retention explanation must wrap on mobile');
        await page.screenshot({path:out+'/context-dialog-ru-mobile.png',animations:'disabled'});
        await dialog.getByText('Правила сохранения истории',{exact:true}).click();
        assert.equal(await retentionHelp.isVisible(),false);
        await dialog.getByRole('heading',{name:'Настройки контекста',exact:true}).scrollIntoViewIfNeeded();
        await page.screenshot({path:out+'/context-dialog-collapsed-ru-mobile.png',animations:'disabled'});
        await page.evaluate(()=>{window.currentDialogChat='ordinary-b';});await dialog.waitFor({state:'detached'});
        assert(calls.filter(call=>call.method==='POST').every(call=>[api+'/context-policy',api+'/context-presets'].includes(call.path)),'saving settings cannot invoke models or probes');assert.deepEqual(errors,[]);console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=85, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
