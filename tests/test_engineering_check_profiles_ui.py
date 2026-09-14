"""Approved command profiles: real browser, scoped drafts, no execution."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_check_profiles_browser_approval_paging_conflict_and_project_isolation(tmp_path):
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Node/Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs'),crypto=require('node:crypto');
      const {chromium}=require('playwright'),repo=process.argv[1],out=process.argv[2],api='/api/team/engineering',calls=[],errors=[];
      const hash=command=>crypto.createHash('sha256').update(command).digest('hex');
      const record=(id,project_id,name,command)=>({id,project_id,name,command,revision:1,command_hash:hash(command)});
      const profiles={p1:Array.from({length:55},(_,n)=>record('p'+String(n).padStart(3,'0'),'p1','Profile '+n,"printf 'record "+n+"'")),p2:[record('q001','p2','Other profile','printf other')]};
      const projects=[{id:'p1',name:'Save <b>Project</b>',root:'/work/Save',host_id:'jetson',access_mode:null,revision:7},{id:'p2',name:'Second project',root:'/work/second',host_id:'mac',access_mode:null,revision:2}];
      let enabled=false,failPage=false,repeatCursor=false,holdList=false,holdSave=false,pendingList,pendingSave,onHeld;
      const held=()=>new Promise(resolve=>onHeld=resolve);
      const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Check profiles QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>html,body{height:auto;overflow:visible}body{display:block}main{max-width:1000px;margin:auto;padding:12px}</style></head><body><main id="root"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';import * as locale from '/static/js/i18n.js';window.locale=locale;window.mount=()=>window.destroy=mountEngineeringWorkspace(document.getElementById('root'),{request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},...(options.body?{body:JSON.stringify(options.body)}:{})});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});mount();</script></body></html>`;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(repo+path));return;}
        if(path==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}if(path==='/favicon.ico'){res.end();return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({path,method:req.method,body,query:url.search});
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
        if(path===api+'/capabilities'){send({enabled:true,features:{projects:true,check_profiles:enabled}});return;}
        if(path===api+'/hosts'){send({hosts:[]});return;}if(path===api+'/projects'){send({projects});return;}
        const match=path.match(/\/projects\/(p1|p2)\/check-profiles$/);
        if(match){const project=match[1];
          if(req.method==='GET'){
            assert.equal(url.searchParams.get('limit'),'50');const after=url.searchParams.get('after_id')||'';
            if(after&&failPage){failPage=false;send({detail:'Temporary page failure'},503);return;}
            const list=profiles[project].filter(item=>item.id>after),data={profiles:list.slice(0,50),next_cursor:list.length>50?list[49].id:null};
            if(after&&repeatCursor){data.next_cursor=after;data.profiles=[];}
            if(project==='p1'&&holdList){holdList=false;pendingList=()=>send(data);onHeld();onHeld=null;return;}send(data);return;
          }
          assert.equal(req.method,'POST');assert.deepEqual(Object.keys(body).sort(),['command','confirmation','expected_revision','name','profile_id']);assert.equal(body.confirmation,true);
          const old=profiles[project].find(item=>item.id===body.profile_id);
          if(body.profile_id!==null&&(!old||body.expected_revision!==old.revision)){send({detail:'Check profile changed; approve current command explicitly'},409);return;}
          if(body.profile_id===null)assert.equal(body.expected_revision,null);
          const value=old?{...old,name:body.name,command:body.command,command_hash:hash(body.command),revision:old.revision+1}:record('z-new',project,body.name,body.command);
          profiles[project]=profiles[project].filter(item=>item.id!==value.id).concat(value).sort((a,b)=>a.id.localeCompare(b.id));
          if(holdSave){holdSave=false;pendingSave=()=>send(value);onHeld();onHeld=null;return;}send(value);return;
        }
        send({detail:'Unexpected route'},404);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));page.setDefaultTimeout(7000);
        const origin='http://127.0.0.1:'+server.address().port;await page.goto(origin);const by=key=>page.locator('[data-engineering="'+key+'"]');
        await by('notice').filter({hasText:'No hosts are configured'}).waitFor();await by('project').selectOption('p1');
        assert.equal(await by('check-profiles').count(),1,'check profile panel must be mounted');assert.equal(await by('check-profiles').isHidden(),true);assert.equal(calls.some(call=>call.path.endsWith('/check-profiles')),false);
        enabled=true;await page.evaluate(()=>{destroy();mount();});await by('notice').filter({hasText:'No hosts are configured'}).waitFor();
        assert.equal(await by('check-save').isDisabled(),true);assert.equal(calls.some(call=>call.path.endsWith('/check-profiles')),false,'no project means no discovery request');
        await by('project').selectOption('p1');await page.waitForFunction(()=>document.querySelector('[data-engineering="check-profile"]').options.length===51);
        assert.equal(calls.filter(call=>call.path.endsWith('/check-profiles')).length,1);assert.equal(calls.some(call=>call.method==='POST'),false);
        assert.equal(await by('check-confirm').isChecked(),false);assert.equal(await by('check-save').isDisabled(),true);
        const command="  printf '%s\\n' '<b>Save</b>'\n# Keep command whitespace and English words unchanged\n";
        await by('check-name').fill('Save <b>profile</b>');await by('check-command').fill(command);assert.equal(await by('check-preview').textContent(),command);
        await by('check-confirm').check();await by('check-command').fill(command+'# revised\n');assert.equal(await by('check-confirm').isChecked(),false,'changing exact command revokes approval');
        await by('check-command').fill('');await by('check-confirm').check();await by('check-save').click();await by('check-notice').filter({hasText:'Enter a profile name and command'}).waitFor();assert.equal(calls.some(call=>call.method==='POST'),false);
        await by('check-command').fill(command);await by('check-confirm').check();await by('check-save').click();await by('check-notice').filter({hasText:'Command profile saved'}).waitFor();
        assert.deepEqual(calls.find(call=>call.method==='POST').body,{name:'Save <b>profile</b>',command,confirmation:true,profile_id:null,expected_revision:null});
        assert.equal(await by('check-preview').textContent(),command);assert.equal(await by('check-confirm').isChecked(),false);assert((await by('check-saved-details').textContent()).includes(hash(command)));
        assert.equal(await page.locator('main b').count(),0,'commands and profile names are not HTML');
        failPage=true;await by('check-more').click();await by('check-notice').filter({hasText:'Temporary page failure'}).waitFor();assert.equal(await by('check-more').isEnabled(),true);
        repeatCursor=true;await by('check-more').click();await by('check-notice').filter({hasText:'did not advance'}).waitFor();assert.equal(await by('check-more').isDisabled(),true);repeatCursor=false;
        await by('check-refresh').click();await by('check-notice').filter({hasText:'Check profiles loaded'}).waitFor();await by('check-more').click();await page.waitForFunction(()=>document.querySelector('[data-engineering="check-profile"]').options.length===57);
        assert.equal(await by('check-profile').inputValue(),'z-new','pagination and refresh preserve selected draft');assert.equal(await by('check-preview').textContent(),command);
        const ids=await by('check-profile').locator('option').evaluateAll(options=>options.map(item=>item.value));assert.equal(new Set(ids).size,57);assert.equal(await by('check-more').isHidden(),true);
        await by('check-profile').selectOption('p000');await by('check-command').fill('printf local-draft');
        Object.assign(profiles.p1.find(item=>item.id==='p000'),{revision:2,command:'printf other-device',command_hash:hash('printf other-device')});
        await by('check-confirm').check();await by('check-save').click();await by('check-notice').filter({hasText:'changed elsewhere'}).waitFor();
        assert.equal(await by('check-command').inputValue(),'printf local-draft');assert.equal(await by('check-confirm').isChecked(),false);assert.equal(await by('check-save').isDisabled(),true);
        assert.equal(await by('check-use-revision').isDisabled(),true,'a conflict cannot accept the stale cached revision before reloading');
        const afterConflict=calls.filter(call=>call.method==='POST').length;await by('check-refresh').click();await by('check-saved-command').filter({hasText:'printf other-device'}).waitFor();
        assert.equal(await by('check-command').inputValue(),'printf local-draft');assert.equal(calls.filter(call=>call.method==='POST').length,afterConflict);
        await by('check-use-revision').click();assert.equal(await by('check-confirm').isChecked(),false);await by('check-confirm').check();await by('check-save').click();await by('check-notice').filter({hasText:'Command profile saved'}).waitFor();
        assert.equal(calls.filter(call=>call.method==='POST').at(-1).body.expected_revision,2);assert.equal(profiles.p1.find(item=>item.id==='p000').revision,3);
        await by('check-more').click();await by('check-profile').selectOption('p054');await by('check-command').fill('printf beyond-first-page');
        Object.assign(profiles.p1.find(item=>item.id==='p054'),{revision:2,command:'printf remote-last-page',command_hash:hash('printf remote-last-page')});
        await by('check-confirm').check();await by('check-save').click();await by('check-notice').filter({hasText:'changed elsewhere'}).waitFor();
        await by('check-refresh').click();await by('check-notice').filter({hasText:'Check profile changed'}).waitFor();
        assert.equal(await by('check-profile').inputValue(),'p054');assert.equal(await by('check-command').inputValue(),'printf beyond-first-page');assert.equal(await by('check-use-revision').isDisabled(),true,'cached profile outside the fresh page cannot clear a conflict');
        await by('check-more').click();await by('check-saved-command').filter({hasText:'printf remote-last-page'}).waitFor();
        assert.equal(await by('check-command').inputValue(),'printf beyond-first-page');await by('check-use-revision').click();await by('check-confirm').check();await by('check-save').click();await by('check-notice').filter({hasText:'Command profile saved'}).waitFor();
        assert.equal(profiles.p1.find(item=>item.id==='p054').revision,3);assert.equal(calls.filter(call=>call.method==='POST').at(-1).body.expected_revision,2);
        await by('check-profile').selectOption('p000');
        holdList=true;let ready=held();await by('check-refresh').click();await ready;await by('project').selectOption('p2');await page.waitForFunction(()=>document.querySelector('[data-engineering="check-profile"]').options.length===2);
        const oldList=page.waitForResponse(response=>response.url().includes('/p1/check-profiles'));pendingList();await oldList;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal(await by('check-command').inputValue(),'');assert.equal(await by('check-profile').locator('option[value="p000"]').count(),0,'late response cannot populate another project');
        await by('project').selectOption('p1');await by('check-notice').filter({hasText:'Check profiles loaded'}).waitFor();assert.equal(await by('check-command').inputValue(),'printf local-draft','project switch keeps that project draft');
        await by('check-command').fill('printf pending-p1');await by('check-confirm').check();holdSave=true;ready=held();await by('check-save').click();await ready;
        await by('project').selectOption('p2');await by('check-profile').selectOption('q001');const oldSave=page.waitForResponse(response=>response.url().includes('/p1/check-profiles')&&response.request().method()==='POST');pendingSave();await oldSave;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal(await by('check-command').inputValue(),'printf other');assert.equal(await by('check-profile').inputValue(),'q001');assert.equal(await by('check-confirm').isChecked(),false);
        await page.evaluate(()=>locale.applyUiLanguage('ru'));assert.equal(await by('check-save').textContent(),'Сохранить подтверждённую команду');
        assert.equal(await by('check-preview').textContent(),'printf other');assert((await by('check-profiles').textContent()).includes('Команда не выполняется'));assert((await by('check-project').textContent()).includes('/work/second'));
        await by('check-profiles').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/check-profiles-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await by('check-form').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/check-profiles-mobile.png',animations:'disabled'});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await page.evaluate(()=>locale.applyUiLanguage('en'));assert.equal(await by('check-save').textContent(),'Save approved command');assert.equal(await by('check-preview').textContent(),'printf other');
        assert.equal(await page.title(),'Check profiles QA');assert.equal(page.url(),origin+'/');
        assert(calls.filter(call=>call.method==='POST').every(call=>/\/projects\/p1\/check-profiles$/.test(call.path)),'no host execution, permission, or inference endpoint may be called');assert.deepEqual(projects.map(project=>[project.access_mode,project.revision]),[[null,7],[null,2]]);
        assert.deepEqual(errors,[]);console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=100, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
