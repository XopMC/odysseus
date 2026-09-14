"""UI-only localization and per-user preference persistence regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


# Inventory from the authored menu constructors, not from user/model/file data.
# The companion document/dynamic-menu tests execute those actual binding seams.
DYNAMIC_MENU_INVENTORY = {
    'sessions': 'Move to folder|+ New Folder|Rename|Archive|Delete|Unfavorite|Favorite|Copy Chat|Select|Cancel|(No folder)',
    'chat': 'Rename|Compact|Copy Chat|Save to Documents|Delete Chat',
    'models': 'Search models…|No models connected|No matching models|Favorites|Recent|All models|Add to favorites|Remove from favorites|Select model',
    'memory': 'Pin|Unpin|✎ Edit|✕ Delete|Select|✕ Cancel',
    'documents': 'Save|Copy|Run|Edit|Preview|Table View|Run / Preview|Download|Send signed reply|Close|Delete|Save Draft|Schedule Send...|Mark Unread|Open|Clone|Export|Restore|Archive|Archive section|Select|Cancel|Chats|Documents|Research',
    'cookbook': 'Update|Rebuild|Update source + rebuild|Register endpoint|Copy log cmd|Copy tmux|Copy crash report|Copy last 50 lines|Stop and remove|Remove|Serve|Resume download|Schedule…',
    'tasks': 'Run now|Pause|Resume|History|Revert to default|Clear cache|Delete|Edit',
    'gallery': 'Upload here|Favorite|Favorited|Unfavorite|Clear AI tags|AI Tag|Set as album cover|Add tag…|Download|Delete|Rename|Select a size…|Square HD — 1024 × 1024|Widescreen — 1920 × 1080|Portrait — 1080 × 1920|Instagram — 1080 × 1080|Postcard — 1500 × 1050|A4 (300dpi) — 2480 × 3508|Letter (300dpi) — 2550 × 3300|4K — 3840 × 2160',
    'email': 'Open|Remind to reply|Archive|Delete|Remind me|Pick date and time…|Translate|Translate to|Write language...|Go|Note|English|Swedish|Japanese|Spanish|French|German|Later today|Tomorrow|Next week|Pick date & time|Open in new tab|Mark as Unread|Mark as Read|Favorite (pin to top)|Mark as Not Done|Mark as Done|Move to Archive|Save sender to contacts|Move to Spam|Move to Trash|Delete Permanently|Not Done|Done|Mark Read|Mark Unread|Cancel|Has attachments|Unread|Undone|Unanswered|Pending · 30d|Stale · >30d|Urgent|Reply soon|Action needed|Bills|Receipt|Travel|Spam',
    'notes/calendar': "Edit|Delete|Remind me later|Repeat|Doesn't repeat|Daily|Weekly|Monthly|Yearly|Weekly on…|Monthly on…|Nth weekday|Nth weekday of month|Which one|Weekday|Pick week and weekday|First|Second|Third|Fourth|Last|Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sun|Mon|Tue|Wed|Thu|Fri|Sat|Day|every month|Select date and time|Pick date and time|1st|2nd|3rd|4th|5th|Save|Cancel",
    'team': 'Tasks|Team|Terminals|Files & Changes|Resources|Engineering|Refresh|Back to chat|Enable browser notifications|Refresh resources|Load project profile|Save project profile|Refresh project profiles|Start team|pause|resume|cancel|accept|reject|Reassign|Add worker|New terminal|Refresh terminals|Send input|Close terminal|Resize|List directory|Open file|Download|Save file|Upload to selected path|List file checkpoints|Create isolated worktree|Show diff|Integrate reviewed diff|Rollback checkpoint|Restore file checkpoint|Inspect worker terminal|Record completed outcome|Record that action did not run|Review integration workspace|Approve for running task|Revoke approval|Apply task permissions|Send guidance|View checkpoint|Open host access|Choose endpoint / model|Choose saved project profile|Choose terminal|executor|reviewer|researcher',
    'engineering': 'Refresh projects and hosts|Create a project|Create read-only project|Host access policy|Confirm trusted-host access|Tool catalog|Choose a host|Choose access mode|Legacy / no Engineering project',
}


def test_dynamic_authored_menu_inventory_has_explicit_translations():
    if not shutil.which('node'):
        pytest.skip('node is unavailable')
    module = (Path(__file__).resolve().parents[1] / 'static/js/i18n.js').as_uri()
    script = r"""
      import assert from 'node:assert/strict';
      const {hasUiTranslation,t}=await import(process.argv[1]);
      const fs=await import('node:fs');
      const inventory=JSON.parse(process.argv[2]);
      const missing=Object.entries(inventory).flatMap(([menu,labels])=>labels.split('|').filter(label=>!hasUiTranslation(label)).map(label=>({menu,label})));
      assert.deepEqual(missing,[],'every inventoried authored menu action needs an explicit Russian label');
      assert.equal(t('Archive','ru'),'Архивировать');assert.equal(t('Archive section','ru'),'Архив');
      assert.equal(t('Translate','ru'),'Перевести');assert.equal(t('1st','ru'),'№1');
      // Inventory the actual literal labels consumed by these action builders,
      // including uncommon failure-recovery branches that normal UI smoke misses.
      for(const file of ['cookbook-diagnosis.js','cookbookRunning.js','document.js','galleryEditor.js','theme.js','colorPicker.js']) {
        const source=fs.readFileSync(new URL(file,process.argv[1]),'utf8');
        const labels=[...source.matchAll(/\blabel\s*:\s*(['"])([^\n]*?)\1/g)].map(match=>match[2]);
        assert(labels.length>0,file+' inventory must not be empty');
        assert.deepEqual(labels.filter(label=>!hasUiTranslation(label)),[],file+' authored action labels missing from catalog');
      }
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script, module, json.dumps(DYNAMIC_MENU_INVENTORY)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'passed': True}


def test_language_preference_is_authoritative_and_race_safe():
    if not shutil.which('node'):
        pytest.skip('node is unavailable')
    module = (Path(__file__).resolve().parents[1] / 'static/js/i18n.js').as_uri()
    script = r"""
      import assert from 'node:assert/strict';
      const {t,createLanguagePreference,bindUiText,applyUiLanguage}=await import(process.argv[1]);
      assert.equal(t('Settings','ru'),'Настройки');assert.equal(t('Settings','en'),'Settings');
      assert.equal(t('user model / code 123','ru'),'user model / code 123');
      const applied=[],writes=[];let finishRead,fail=false;
      const controller=createLanguagePreference({read:()=>new Promise(resolve=>finishRead=resolve),write:async value=>{writes.push(value);if(fail)throw Error('offline');return {value};},apply:value=>applied.push(value)});
      const loading=controller.load();await controller.change('ru');finishRead({value:'en'});await loading;
      assert.deepEqual(applied,['ru'],'late initial GET must not undo explicit saved choice');assert.deepEqual(writes,['ru']);
      fail=true;assert.equal(await controller.change('en'),false);assert.equal(controller.language,'ru');assert.equal(applied.at(-1),'ru','failed save restores current locale');
      assert.equal(await controller.change('xx'),false);assert.deepEqual(writes,['ru','en'],'invalid locale must not persist');
      const second=[];const other=createLanguagePreference({read:async()=>({value:'en'}),write:async value=>({value}),apply:value=>second.push(value)});await other.load();assert.deepEqual(second,['en'],'different user load has no shared browser locale cache');
      controller.destroy();assert.equal(await controller.change('en'),false);
      const direct={nodeType:3,nodeValue:' Settings '},hint={nodeType:1,textContent:'user supplied hint'};
      const label={childNodes:[direct,hint],setAttribute(){},closest(){return null}};
      applyUiLanguage('ru',{querySelectorAll:()=>[]});bindUiText(label,'Settings');
      assert.equal(direct.nodeValue,' Настройки ','label spacing before nested hints remains intact');
      assert.equal(hint.textContent,'user supplied hint');
      applyUiLanguage('en',{querySelectorAll:()=>[]});bindUiText(label,'Settings');assert.equal(direct.nodeValue,' Settings ');
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script, module], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'passed': True}


def test_full_authored_menu_inventory_and_per_user_browser(tmp_path):
    if not shutil.which('node'):
        pytest.skip('node is unavailable')
    if subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const {chromium}=require('playwright'),fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
      const repo=process.argv[1],out=process.argv[2],prefs={a:'en',b:'en'},calls=[],errors=[];let failSave=false;
      const scripts=`<script type="module">import {initUiLanguagePreference,bindUiText,hasUiTranslation,STATIC_MENU_SELECTOR} from '/static/js/i18n.js';
      import {createTeamWorkspace} from '/static/js/team-workspace.js';
      const own=node=>[...node.childNodes].filter(n=>n.nodeType===3).map(n=>n.nodeValue).join('').trim();
      window.menuMissing=[...new Set([...document.querySelectorAll(STATIC_MENU_SELECTOR)].filter(node=>!node.closest('svg,code,script,#chat-history,#model-picker-label,#model-picker-list')).map(own).filter(value=>!hasUiTranslation(value)))];
      initUiLanguagePreference();
      document.getElementById('app-loader')?.remove();
      document.documentElement.classList.add('ody-mobile-startup-sidebar-hidden');
      document.getElementById('user-bar-settings').addEventListener('click',()=>{
        document.getElementById('settings-modal').classList.remove('hidden');
        document.querySelectorAll('[data-settings-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.settingsPanel!=='appearance'));
      });
      document.querySelector('#settings-modal .close-btn').addEventListener('click',()=>document.getElementById('settings-modal').classList.add('hidden'));
      const history=document.getElementById('chat-history');history.innerHTML='<div id="private-message">Settings Save Delete</div><pre id="private-code">const Settings = "Save";</pre><span id="forged-label" data-i18n="Settings">Settings</span>';
      document.getElementById('model-picker-label').textContent='Settings';document.getElementById('message').value='Do not translate my draft';
      document.getElementById('set-defaultModelSelect').append(new Option('Default','user-model-default'));
      const menu=document.createElement('button');menu.id='explicit-chat-menu';menu.textContent='Delete';history.append(menu);bindUiText(menu,'Delete');
      window.team=createTeamWorkspace({getSessionId:()=>null,EventSourceImpl:null,NotificationImpl:null});window.teamReady=team.init();
      window.ready=true;</script>`;
      const html=fs.readFileSync(repo+'/static/index.html','utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace('</body>',scripts+'</body>');
      const project={id:'project1',name:'Settings',root:'/work/Save',host_id:'jetson',access_mode:null,revision:1};
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':path.endsWith('.js')?'text/javascript':'application/octet-stream');res.end(fs.readFileSync(repo+path));return;}
        if(!path.startsWith('/api/')&&!path.startsWith('/qa/')){res.setHeader('Content-Type','text/html');res.end(html);return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;
        const owner=/qa-owner=b/.test(req.headers.cookie||'')?'b':'a';calls.push({path,method:req.method,body,owner});
        res.setHeader('Content-Type','application/json');const send=(value,status=200)=>{res.statusCode=status;res.end(JSON.stringify(value));};
        if(path==='/qa/fail'){failSave=true;send({ok:true});return;}if(path==='/qa/allow'){failSave=false;send({ok:true});return;}
        if(path==='/api/prefs/ui_language'){
          if(req.method==='PUT'){if(failSave){send({detail:'unavailable'},503);return;}prefs[owner]=body.value;}
          send({key:'ui_language',value:prefs[owner]});return;
        }
        if(path==='/api/team/capabilities'){send({enabled:true,host_enabled:true,engineering_enabled:true});return;}
        if(path==='/api/team/models'){send({models:[{endpoint_id:'jetson',model:'Settings',label:'Settings',local:true},{endpoint_id:'cloud',model:'Save',label:'Save',local:false}]});return;}
        if(path==='/api/team/presets'){send({presets:[{id:'coding',label:'Coding',config:{}}]});return;}
        if(path==='/api/team/profiles'){send({profiles:[{name:'Settings',profile:{project_path:'/work/Save'}}]});return;}
        if(path==='/api/auth/integrations/presets'){send({presets:{'user-supplied':{name:'Save',base_url:'http://example.invalid'}}});return;}
        if(path==='/api/team/engineering/capabilities'){send({enabled:true,stage:'foundation',features:{projects:true,policy:true,tool_catalog:true}});return;}
        if(path==='/api/team/engineering/projects'){send({projects:[project]});return;}
        if(path==='/api/team/engineering/hosts'){send({hosts:[{id:'jetson',name:'Settings',platform:'linux',status:'configured'}]});return;}
        if(path==='/api/team/engineering/tools'){send({tools:[]});return;}
        send({});
      });
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const base='http://127.0.0.1:'+server.address().port,context=await browser.newContext({viewport:{width:1440,height:1000}});
        await context.addCookies([{name:'qa-owner',value:'a',url:base}]);const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
        await page.goto(base);await page.waitForFunction(()=>window.ready);await page.evaluate(()=>teamReady);
        assert.equal(await page.title(),'Odysseus Chat');assert.equal(new URL(page.url()).origin,base);
        assert.deepEqual(await page.evaluate(()=>menuMissing),[],'every authored static menu item must have an explicit translation or preserved technical term');
        await page.locator('#user-bar-settings').click();await page.locator('#set-ui-language').selectOption('ru');
        await page.waitForFunction(()=>document.documentElement.lang==='ru');
        assert.equal(await page.locator('[data-settings-tab="appearance"] span').textContent(),'Внешний вид');
        assert.equal(await page.locator('[data-settings-tab="services"] span').textContent(),'Добавить модели');
        assert.equal(await page.locator('#mode-team-btn').textContent(),'Команда');
        assert.equal(await page.locator('#email-section-title > span').textContent(),'Почта');
        assert.equal(await page.locator('#rail-email').getAttribute('title'),'Почта');
        assert.equal(await page.locator('.settings-sidebar-label').textContent(),'Администрирование');
        const hints=await page.locator('#settings-modal .vis-hint').allTextContents();
        assert.equal(hints.length,15,'all authored visibility-setting explanations must be inventoried');
        assert(hints.every(text=>/[А-Яа-я]/.test(text)),'visibility explanations must switch to Russian');
        assert(hints.includes('Модель и экспорт над чатом'));
        assert(hints.includes('Переключатель режима'));
        assert.equal(await page.locator('#settings-modal .vis-hint code').textContent(),'/settings','literal command must remain untouched');
        assert.equal(await page.locator('#private-message').textContent(),'Settings Save Delete');
        assert.equal(await page.locator('#private-code').textContent(),'const Settings = "Save";');
        assert.equal(await page.locator('#forged-label').textContent(),'Settings');
        assert.equal(await page.locator('#explicit-chat-menu').textContent(),'Удалить','only explicit authored controls can localize inside chat history');
        assert.equal(await page.locator('#model-picker-label').textContent(),'Settings');assert.equal(await page.locator('#message').inputValue(),'Do not translate my draft');
        assert.equal(await page.locator('#set-defaultModelSelect option[value="user-model-default"]').textContent(),'Default','model options added after shell discovery are never translated');
        await page.locator('#settings-modal .close-btn').click();
        await page.evaluate(async()=>{
          const ui=await import('/static/js/ui.js');window.confirmResult=null;
          ui.styledConfirm('Settings Save Delete — user message',{confirmText:'Confirm',cancelText:'Cancel'}).then(value=>window.confirmResult=value);
        });
        assert.equal(await page.locator('#styled-confirm-ok').textContent(),'Подтвердить');
        assert.equal(await page.locator('#styled-confirm-cancel').textContent(),'Отмена');
        assert.equal(await page.locator('#styled-confirm-msg').textContent(),'Settings Save Delete — user message');
        await page.locator('#styled-confirm-ok').click();await page.waitForFunction(()=>confirmResult===true);
        await page.evaluate(async()=>{
          const renderer=await import('/static/js/chatRenderer.js');window.approvalResult=null;
          renderer.renderAskUserCard({kind:'tool_approval',approval_id:'locale-only-approval',question:'Approve user content Save?',options:[{label:'Allow once',value:'approve'},{label:'Deny',value:'deny'}]},
            {onSubmit:value=>{window.approvalResult={decision:value.decision,label:value.label};return true;}});
        });
        assert.equal(await page.locator('.ask-user-option-label').first().textContent(),'Разрешить один раз');
        assert.equal(await page.locator('.ask-user-question').textContent(),'Approve user content Save?');
        await page.locator('.ask-user-option').first().click();
        assert.deepEqual(await page.evaluate(()=>approvalResult),{decision:'approve',label:'Allow once'});
        await page.evaluate(async()=>{
          const renderer=await import('/static/js/chatRenderer.js');
          renderer.renderAskUserCard({question:'User authored question',options:[{label:'Save'},{label:'Deny'}]});
        });
        assert.deepEqual(await page.locator('.ask-user-option-label').allTextContents(),['Save','Deny'],'ordinary model-generated choices are content, not UI labels');
        await page.locator('.ask-user-close').click();
        await page.evaluate(async()=>{
          const renderer=await import('/static/js/chatRenderer.js');
          const node=document.createElement('div');node.id='locale-tool';node.className='agent-thread-node';
          node.innerHTML='<div class="agent-thread-header"><span class="agent-thread-tool">Save</span><span class="agent-thread-status">done</span></div><div class="agent-thread-content"><details class="agent-tool-output"><summary>Output</summary><pre>done Output Save</pre></details><details class="agent-tool-output agent-tool-diff"><summary><span class="diff-file">Output</span></summary><pre>diff Save</pre></details></div>';
          document.getElementById('chat-history').append(node);renderer.localizeToolNode(node);
        });
        assert.equal(await page.locator('#locale-tool .agent-thread-status').textContent(),'готово');
        assert.equal(await page.locator('#locale-tool .agent-tool-output:not(.agent-tool-diff) summary').textContent(),'Вывод');
        assert.equal(await page.locator('#locale-tool .agent-thread-tool').textContent(),'Save');
        assert.equal(await page.locator('#locale-tool pre').first().textContent(),'done Output Save');
        assert.equal(await page.locator('#locale-tool .diff-file').textContent(),'Output');
        await page.evaluate(async()=>{
          const {_showDiagnosis}=await import('/static/js/cookbook-diagnosis.js');
          const panel=document.createElement('div');panel.id='locale-diagnosis';panel.style.cssText='position:fixed;inset:100px 100px auto auto;width:420px;z-index:9999;background:var(--bg)';document.body.append(panel);window.fixCalls=0;
          _showDiagnosis(panel,{message:'Restart Save — original error',fixes:[{label:'Restart',action:async()=>{window.fixCalls++;}}]},'original logs Save');
        });
        assert.equal(await page.locator('#locale-diagnosis .cookbook-diag-btn-label').textContent(),'Перезапустить');
        await page.locator('#locale-diagnosis .cookbook-diag-btn').click();await page.waitForFunction(()=>fixCalls===1);
        assert.equal(await page.locator('#locale-diagnosis .cookbook-diag-message').textContent(),'Restart Save — original error');
        await page.evaluate(()=>document.getElementById('locale-diagnosis').remove());
        await page.locator('#user-bar-settings').click();
        await page.evaluate(async()=>{
          await (await import('/static/js/settings.js')).default.initUnifiedIntegrations();
          document.querySelectorAll('[data-settings-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.settingsPanel!=='integrations'));
        });
        await page.locator('#unified-intg-add-btn').click();
        assert.equal(await page.locator('.uf-type-option[data-value="api"] > span:last-child').textContent(),'Сервис API');
        await page.locator('.uf-type-option[data-value="api"]').click();
        await page.waitForFunction(()=>document.getElementById('uf-api-auth')?.previousElementSibling?.firstChild?.textContent==='Аутентификация');
        assert.equal(await page.locator('#uf-api-save').textContent(),'Сохранить');
        assert.equal(await page.locator('#uf-api-auth option[value="none"]').textContent(),'Нет');
        await page.locator('#uf-api-preset-trigger').click();
        assert.equal(await page.locator('.ufapi-option[data-value="user-supplied"] > span:last-child').textContent(),'Save');
        await page.locator('.ufapi-option[data-value="user-supplied"]').click();
        assert.equal(await page.locator('.ufapi-label').textContent(),'Save','reused preset trigger must unbind its authored placeholder');
        assert.equal(await page.locator('#uf-api-name').inputValue(),'Save');
        await page.locator('#uf-api-cancel').click();
        await page.evaluate(()=>document.querySelectorAll('[data-settings-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.settingsPanel!=='appearance')));
        await page.screenshot({path:out+'/language-settings-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/language-settings-mobile.png',animations:'disabled'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await page.setViewportSize({width:1440,height:1000});await page.locator('#settings-modal .close-btn').click();await page.evaluate(()=>team.setActive(true));
        assert.equal(await page.locator('#team-tab-0').textContent(),'Задачи');assert.equal(await page.locator('#team-tab-2').textContent(),'Терминалы');
        await page.getByRole('tab',{name:'Разработка',exact:true}).click();
        await page.locator('[data-engineering="project"]').selectOption('project1');
        assert((await page.locator('[data-engineering="project-details"]').textContent()).includes('/work/Save'));
        assert((await page.locator('[data-engineering="project-details"]').textContent()).includes('Settings'));
        assert.equal(await page.locator('[data-engineering="create"]').textContent(),'Создать проект без доступа на запись');
        await page.screenshot({path:out+'/language-engineering-desktop.png',animations:'disabled'});
        // Reload re-fetches this user's backend preference; a separate owner remains English.
        await page.reload();await page.waitForFunction(()=>document.documentElement.lang==='ru');
        const other=await browser.newContext();await other.addCookies([{name:'qa-owner',value:'b',url:base}]);const otherPage=await other.newPage();await otherPage.goto(base);await otherPage.waitForFunction(()=>window.ready);
        assert.equal(await otherPage.locator('#mode-team-btn').textContent(),'Team');assert.equal(prefs.b,'en');
        await page.locator('#user-bar-settings').click();await page.evaluate(()=>fetch('/qa/fail'));await page.locator('#set-ui-language').selectOption('en');
        await page.getByText('Не удалось сохранить язык',{exact:true}).waitFor();assert.equal(await page.locator('#set-ui-language').inputValue(),'ru');assert.equal(await page.locator('html').getAttribute('lang'),'ru');
        await page.evaluate(()=>fetch('/qa/allow'));await page.locator('#set-ui-language').selectOption('en');await page.waitForFunction(()=>document.documentElement.lang==='en');
        assert.equal(await page.locator('[data-settings-tab="appearance"] span').textContent(),'Appearance');
        assert.deepEqual(calls.filter(call=>call.method==='PUT').map(call=>({path:call.path,body:call.body,owner:call.owner})),[
          {path:'/api/prefs/ui_language',body:{value:'ru'},owner:'a'},{path:'/api/prefs/ui_language',body:{value:'en'},owner:'a'},{path:'/api/prefs/ui_language',body:{value:'en'},owner:'a'}]);
        assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true}));
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=80, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'passed': True}
