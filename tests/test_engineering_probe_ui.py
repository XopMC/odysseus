"""Browser contract for explicit, scoped model probes and persistent operations."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_model_probe_browser_confirmation_recovery_and_locale(tmp_path):
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Node/Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const {chromium}=require('playwright'),fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
      const repo=process.argv[1],out=process.argv[2],api='/api/team/engineering',calls=[],errors=[],operations=[];
      let releaseDescribe,holdFirst=true,conflict=true;
      const model='<b>same/model</b>',a='local-A-long-complete-endpoint',b='local-B-long-complete-endpoint',external='paid-provider';
      const desc=id=>({scope:{endpoint_id:id,model,config_digest:'digest-'+id},supported:id!==external,reason:id===external?'No paid requests allowed':'Synthetic only',max_requests:2,max_output_tokens_per_request:128,deadline_seconds:40});
      const html=`<!doctype html><html><head><title>Engineering probe QA</title><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>html,body{height:auto;overflow:visible}body{display:block}main{max-width:960px;margin:auto;padding:12px}</style></head><body><main id="root"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';import * as locale from '/static/js/i18n.js';window.locale=locale;window.mount=()=>window.destroy=mountEngineeringWorkspace(document.getElementById('root'),{request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},...(options.body?{body:JSON.stringify(options.body)}:{})});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});mount();</script></body></html>`;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(repo+path));return;}
        if(path==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({path,method:req.method,body,query:url.search});
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
        if(path===api+'/capabilities'){send({enabled:true,features:{projects:true,policy:true,tool_catalog:true,model_probe:true}});return;}
        if(path===api+'/hosts'){send({hosts:[]});return;}if(path===api+'/projects'){send({projects:[]});return;}
        if(path==='/api/team/models'){send({models:[a,b,external].map(endpoint_id=>({endpoint_id,model,label:'Same label',local:endpoint_id!==external}))});return;}
        if(path===api+'/model-probe'&&req.method==='GET'){
          const id=url.searchParams.get('endpoint_id');assert.equal(url.searchParams.get('model'),model);
          if(id===a&&holdFirst){holdFirst=false;releaseDescribe=()=>send(desc(a));return;}send(desc(id));return;
        }
        if(path==='/qa/release'){releaseDescribe();send({ok:true});return;}
        if(path===api+'/model-probe'&&req.method==='POST'){
          assert.deepEqual(body,{endpoint_id:b,model,confirmation:true,expected_config_digest:'digest-'+b});
          if(conflict){conflict=false;send({detail:'Configuration changed'},409);return;}
          const op={id:'op'+(operations.length+1),kind:'model_probe',status:'queued',scope:desc(b).scope};operations.unshift(op);send(op);return;
        }
        if(path===api+'/operations'){const activeOnly=url.searchParams.get('active_only')==='true';send({operations:activeOnly?operations.filter(item=>['queued','running','cancel_requested'].includes(item.status)).slice(-1):operations,next_cursor:null});return;}
        if(path==='/qa/complete'){const op=operations[0];op.status='completed';op.result={scope:desc(b).scope,status:'completed',capabilities:{streaming:true,native_tools:false,tool_roundtrip:null,usage:null},measurements:{requests:1,completed_requests:1,elapsed_seconds:0.2}};send({ok:true});return;}
        if(path==='/qa/stale-result'){operations[0].result.status='stale';operations[0].result.capabilities={streaming:null,native_tools:null,tool_roundtrip:null,usage:null};send({ok:true});return;}
        if(path.startsWith(api+'/operations/')){
          const id=path.slice((api+'/operations/').length).split('/')[0],op=operations.find(item=>item.id===id);
          if(path.endsWith('/cancel')){assert.deepEqual(body,{});op.status='cancel_requested';send({...op});return;}
          if(op.status==='queued')op.status='running';else if(op.status==='cancel_requested')op.status='cancelled';send({...op});return;
        }
        send({detail:'Unknown route'},404);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));
        await page.goto('http://127.0.0.1:'+server.address().port);const by=key=>page.locator('[data-engineering="'+key+'"]');
        await by('notice').filter({hasText:'No hosts are configured'}).waitFor();
        assert.equal(await by('probe-model').count(),1,'model probe controls must be rendered');
        await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-model"]').options.length===4);
        assert.equal(await page.title(),'Engineering probe QA');
        assert.equal(calls.some(call=>call.method==='POST'),false,'discovery cannot run inference');
        const labels=await by('probe-model').locator('option').allTextContents();assert(labels.some(label=>label.includes(a)));assert(labels.some(label=>label.includes(b)));
        await by('probe-model').selectOption(JSON.stringify([a,model]));
        await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-description"]').textContent.includes('Loading'));
        await by('probe-model').selectOption(JSON.stringify([b,model]));await by('probe-description').filter({hasText:'digest-'+b}).waitFor();
        const lateDescribe=page.waitForResponse(response=>response.url().includes('/model-probe?endpoint_id='+a));
        await page.evaluate(()=>fetch('/qa/release'));await lateDescribe;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal(await by('probe-description').textContent().then(text=>text.includes('digest-'+a)),false,'late describe cannot steal selected endpoint');
        assert.equal(await by('probe-start').isDisabled(),true);await by('probe-confirm').check();await by('probe-start').click();
        await by('probe-notice').filter({hasText:'Configuration changed'}).waitFor();assert.equal(await by('probe-start').isDisabled(),true);assert.equal(await by('probe-confirm').isChecked(),false);
        assert.equal(calls.filter(call=>call.path===api+'/model-probe'&&call.method==='POST').length,1,'409 must not retry automatically');
        await by('probe-refresh').click();await by('probe-description').filter({hasText:'digest-'+b}).waitFor();await by('probe-confirm').check();await by('probe-start').click();
        await by('probe-operation-status').filter({hasText:'Running'}).waitFor();await by('probe-cancel').click();
        await by('probe-operation-status').filter({hasText:'Cancelled'}).waitFor();assert.equal(await by('probe-cancel').isDisabled(),true);
        await by('probe-model').selectOption(JSON.stringify([external,model]));await by('probe-description').filter({hasText:'No paid requests allowed'}).waitFor();assert.equal(await by('probe-confirm').isDisabled(),true);
        await by('probe-model').selectOption(JSON.stringify([b,model]));await by('probe-description').filter({hasText:'digest-'+b}).waitFor();await by('probe-confirm').check();await by('probe-start').click();
        await by('probe-operation-status').filter({hasText:'Running'}).waitFor();const postsBefore=calls.filter(call=>call.method==='POST').length;
        await page.evaluate(()=>{destroy();mount();});await by('probe-operation-status').filter({hasText:'Running'}).waitFor();
        assert.equal(calls.filter(call=>call.method==='POST').length,postsBefore,'remount discovers the owner-scoped operation without cancelling/restarting');
        await page.evaluate(()=>fetch('/qa/complete'));await by('probe-operation-status').filter({hasText:'Completed'}).waitFor();
        assert.equal(await by('probe-capability-streaming').textContent(),'Observed');assert.equal(await by('probe-capability-native_tools').textContent(),'Not observed');assert.equal(await by('probe-capability-tool_roundtrip').textContent(),'Unverified');
        await page.evaluate(()=>locale.applyUiLanguage('ru'));
        assert.equal(await by('probe-start').textContent(),'Запустить проверку');assert.equal(await by('probe-operation-status').textContent(),'Завершено');
        assert.equal(await by('probe-capability-tool_roundtrip').textContent(),'Не проверено');assert((await by('probe-operation-scope').textContent()).includes(b));
        assert.equal(await page.locator('main b').count(),0,'model names are plain text, not HTML');
        await by('probe-operation-status').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/engineering-probe-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await by('probe-operation-status').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/engineering-probe-mobile.png',animations:'disabled'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await page.evaluate(()=>locale.applyUiLanguage('en'));assert.equal(await by('probe-start').textContent(),'Run model probe');assert.equal(await by('probe-capability-tool_roundtrip').textContent(),'Unverified');
        await page.evaluate(()=>fetch('/qa/stale-result'));await by('probe-refresh-operations').click();
        await by('probe-result').filter({hasText:'Stale or different configuration'}).waitFor();assert.equal(await by('probe-capability-streaming').textContent(),'Unverified');
        assert.deepEqual(errors,[]);console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=80, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
