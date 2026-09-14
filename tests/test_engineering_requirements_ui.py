"""Acceptance criteria and snapshot readiness through real Chrome/HTTP boundaries."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_requirements_browser_confirmation_revision_paging_and_snapshot(tmp_path):
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Node/Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs');
      const {chromium}=require('playwright'),repo=process.argv[1],out=process.argv[2],api='/api/team/engineering',calls=[],errors=[];
      const projects=[{id:'A',name:'Project <b>Save</b>',root:'/work/A',host_id:'host-A',revision:4,access_mode:null},{id:'B',name:'Other project',root:'/work/B',host_id:'host-B',revision:2,access_mode:null}];
      const profiles=Array.from({length:56},(_,i)=>({id:'p'+String(i).padStart(3,'0'),project_id:'A',name:'Profile '+i,command:'printf CHECK_'+i,revision:1,command_hash:'a'.repeat(64)}));
      const rows=profiles.map((profile,i)=>({id:'r'+String(i).padStart(3,'0'),project_id:'A',title:'Criterion '+i,profile_ids:[profile.id],mandatory:true,revision:1}));
      let enabled=false,conflict=true,readinessError=false,readinessReady=false,holdReadiness=false,pendingReadiness,onHeld;
      const held=()=>new Promise(resolve=>onHeld=resolve);
      const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Requirements QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>html,body{height:auto;overflow:visible}body{display:block}main{max-width:1000px;margin:auto;padding:12px}</style></head><body><main id="root"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';import * as locale from '/static/js/i18n.js';window.locale=locale;window.mount=()=>window.destroy=mountEngineeringWorkspace(document.getElementById('root'),{request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},...(options.body?{body:JSON.stringify(options.body)}:{})});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});mount();</script></body></html>`;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(repo+path));return;}
        if(path==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}if(path==='/favicon.ico'){res.end();return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({path,method:req.method,body,query:url.search});
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');res.end(JSON.stringify(data));};
        if(path===api+'/capabilities'){send({enabled:true,features:{projects:true,requirements:enabled}});return;}
        if(path===api+'/hosts'){send({hosts:[]});return;}if(path===api+'/projects'){send({projects});return;}
        if(path.endsWith('/requirements')&&req.method==='POST'){
          assert.equal(path,api+'/projects/A/requirements');assert.deepEqual(Object.keys(body).sort(),['confirmation','expected_revision','mandatory','profile_ids','requirement_id','title']);assert.equal(body.confirmation,true);
          assert(body.profile_ids.length>0&&body.profile_ids.every(id=>profiles.some(profile=>profile.id===id)));
          if(body.requirement_id){const row=rows.find(item=>item.id===body.requirement_id);assert(row);
            if(conflict){conflict=false;row.revision++;row.title='Other saved revision';send({detail:'Requirement changed elsewhere'},409);return;}
            assert.equal(body.expected_revision,row.revision);Object.assign(row,{title:body.title,profile_ids:body.profile_ids,mandatory:body.mandatory,revision:row.revision+1});send({id:row.id,revision:row.revision});return;
          }
          assert.equal(body.expected_revision,null);const row={id:'r-new',project_id:'A',title:body.title,profile_ids:body.profile_ids,mandatory:body.mandatory,revision:1};rows.push(row);send({id:row.id,revision:1});return;
        }
        if(path.endsWith('/requirements')||path.endsWith('/check-profiles')){
          assert.equal(req.method,'GET');assert.equal(url.searchParams.get('limit'),'50');const isProfiles=path.endsWith('/check-profiles'),key=isProfiles?'profiles':'requirements',source=path.includes('/A/')?(isProfiles?profiles:rows):[];
          const after=url.searchParams.get('after_id');if(after&&!source.some(item=>item.id===after)){send({detail:'Foreign cursor'},404);return;}
          const start=after?source.findIndex(item=>item.id===after)+1:0,page=source.slice(start,start+50);send({[key]:page,next_cursor:start+50<source.length?page.at(-1).id:null});return;
        }
        if(path.endsWith('/check-readiness')){
          assert.equal(req.method,'GET');if(readinessError){readinessError=false;send({detail:'Host unavailable'},503);return;}
          const result={ready:readinessReady,workspace_hash:'b'.repeat(64),project_revision:4,snapshot_id:'c'.repeat(64),observed_at:'2026-09-14T12:00:00Z',requirements:[{id:'r-evidence',revision:3,title:'Evidence <b>Save</b>',mandatory:true,passed:readinessReady,checks:[{profile_id:'p000',profile_revision:1,run_id:readinessReady?'saved-check':null,passed:readinessReady,status:readinessReady?'passed':'missing_or_stale'}]}]};
          if(holdReadiness){holdReadiness=false;pendingReadiness=()=>send(result);onHeld();onHeld=null;return;}send(result);return;
        }
        send({detail:'Unexpected route'},404);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));page.setDefaultTimeout(10000);const origin='http://127.0.0.1:'+server.address().port,by=key=>page.locator('[data-engineering="'+key+'"]');
        await page.goto(origin);await by('notice').filter({hasText:'No hosts are configured'}).waitFor();assert.equal(await by('requirements').count(),1);assert.equal(await by('requirements').isHidden(),true);assert.equal(calls.some(call=>call.path.endsWith('/requirements')),false);
        enabled=true;await page.evaluate(()=>{destroy();mount();});await by('notice').filter({hasText:'No hosts are configured'}).waitFor();await by('project').selectOption('A');await by('requirement-more').waitFor();await by('requirement-profiles-more').waitFor();
        assert.equal(await by('requirement-select').locator('option').count(),51);assert.equal(calls.some(call=>call.path.endsWith('/check-readiness')||call.method==='POST'),false);
        await by('requirement-more').click();await by('requirement-select').locator('option[value="r055"]').waitFor({state:'attached'});await by('requirement-profiles-more').click();await page.locator('[data-requirement-profile="p055"]').waitFor();
        await by('requirement-select').selectOption('r055');await by('requirement-title').fill('Draft <b>criterion</b>');await by('requirement-confirm').check();await by('requirement-mandatory').uncheck();assert.equal(await by('requirement-confirm').isChecked(),false,'weakening mandatory requires renewed confirmation');
        assert.equal(calls.filter(call=>call.method==='POST').length,0);await by('requirement-confirm').check();await by('requirement-save').click();await by('requirement-notice').filter({hasText:'changed elsewhere'}).waitFor();
        assert.equal(await by('requirement-title').inputValue(),'Draft <b>criterion</b>');assert.equal(await by('requirement-mandatory').isChecked(),false);assert.equal(await by('requirement-confirm').isDisabled(),true);assert.equal(calls.filter(call=>call.method==='POST').length,1);
        await by('requirement-refresh').click();await by('requirement-notice').filter({hasText:'Criteria loaded'}).waitFor();assert.equal(await by('requirement-use-revision').isDisabled(),true,'pinned old row is not a freshly reviewed revision');
        await by('requirement-more').click();await by('requirement-saved').filter({hasText:'Other saved revision'}).waitFor();await by('requirement-use-revision').click();assert.equal(await by('requirement-title').inputValue(),'Draft <b>criterion</b>');
        await by('requirement-confirm').check();await by('requirement-save').click();await by('requirement-notice').filter({hasText:'Criterion saved'}).waitFor();const edit=calls.filter(call=>call.method==='POST').at(-1).body;assert.equal(edit.expected_revision,2);assert.equal(edit.mandatory,false);assert.deepEqual(edit.profile_ids,['p055']);
        await by('requirement-select').selectOption('');await by('requirement-confirm').check();await by('requirement-save').click();await by('requirement-notice').filter({hasText:'Enter a title'}).waitFor();assert.equal(calls.filter(call=>call.method==='POST').length,2);
        await by('requirement-title').fill('New criterion');await page.locator('[data-requirement-profile="p000"]').check();await by('requirement-confirm').check();await by('requirement-save').click();await by('requirement-notice').filter({hasText:'Criterion saved'}).waitFor();assert.equal(calls.filter(call=>call.method==='POST').length,3);assert.equal(await by('requirement-select').inputValue(),'r-new');
        assert.equal(calls.some(call=>call.path.endsWith('/check-readiness')),false,'saving a criterion does not compute or execute readiness');
        await by('readiness-refresh').click();await by('readiness-result').filter({hasText:'Evidence <b>Save</b>'}).waitFor();assert.equal(await by('readiness-hash').textContent(),'b'.repeat(64));assert((await by('readiness-notice').textContent()).includes('returned workspace snapshot only'));
        readinessError=true;await by('readiness-refresh').click();await by('readiness-notice').filter({hasText:'Unable to read readiness'}).waitFor();assert.equal(await by('readiness-result').textContent(),'','a failed refresh cannot present old green evidence as current');
        readinessReady=true;await by('readiness-refresh').click();await by('readiness-result').filter({hasText:'Ready for the returned snapshot'}).waitFor();assert((await by('readiness-result').textContent()).includes('c'.repeat(64)));
        await page.evaluate(()=>locale.applyUiLanguage('ru'));assert.equal(await by('requirement-save').textContent(),'Сохранить критерий');assert.equal(await by('readiness-refresh').textContent(),'Обновить снимок готовности');assert((await by('readiness-notice').textContent()).includes('только к полученному снимку'));
        assert.equal(await page.locator('main b').count(),0);assert((await by('readiness-result').textContent()).includes('Evidence <b>Save</b>'));await by('readiness-result').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/requirements-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await by('readiness-result').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/requirements-mobile.png',animations:'disabled'});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await page.evaluate(()=>locale.applyUiLanguage('en'));assert.equal(await by('requirement-save').textContent(),'Save criterion');
        await by('requirement-title').fill('Edited after readiness');assert.equal(await by('readiness-result').textContent(),'','local edits invalidate previously passed readiness evidence');await by('requirement-title').fill('New criterion');
        holdReadiness=true;const waitHeld=held();await by('readiness-refresh').click();await waitHeld;await by('project').selectOption('B');const late=page.waitForResponse(response=>response.url().endsWith('/A/check-readiness'));pendingReadiness();await late;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));assert.equal(await by('readiness-result').textContent(),'');assert.equal(await by('requirement-title').inputValue(),'');
        await by('project').selectOption('A');await by('requirement-notice').filter({hasText:'Criteria loaded'}).waitFor();assert.equal(await by('requirement-title').inputValue(),'New criterion','project-local drafts survive selection changes');
        assert(calls.filter(call=>call.method==='POST').every(call=>call.path===api+'/projects/A/requirements'),'no execution, policy or completion mutation is performed');assert.equal(await page.title(),'Requirements QA');assert.equal(page.url(),origin+'/');assert.deepEqual(errors,[]);console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=120, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
