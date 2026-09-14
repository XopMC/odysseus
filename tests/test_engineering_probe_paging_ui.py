"""Real browser probe history: bounded discovery, paging, and request isolation."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_probe_history_paging_restores_old_active_without_mutations(tmp_path):
    if not shutil.which('node'):
        pytest.skip('node is unavailable')
    if subprocess.run(['node', '-e', "require('playwright')"], capture_output=True).returncode:
        pytest.skip('Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs');
      const {chromium}=require('playwright'),repo=process.argv[1],out=process.argv[2],api='/api/team/engineering';
      const calls=[],errors=[],consoleErrors=[],scope={endpoint_id:'host-A',model:'same <b>model</b>',config_digest:'digest-A'};
      const ops=Array.from({length:105},(_,i)=>({id:'op'+String(i).padStart(3,'0'),kind:'model_probe',status:'completed',scope}));
      const old={id:'old-active',kind:'model_probe',status:'running',scope};ops.push(old);
      let failPage=false,repeatCursor=false,holdPage=false,pendingPage=null,heldResolve=null;
      const nextHeld=()=>new Promise(resolve=>{heldResolve=resolve;});
      const html=`<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Probe history QA</title><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>body{margin:0;background:#252930;color:#a2ddeb;font-family:monospace}main{max-width:980px;margin:auto;padding:12px}</style></head><body><main id="root"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';import * as locale from '/static/js/i18n.js';window.locale=locale;window.mount=()=>window.destroy=mountEngineeringWorkspace(document.getElementById('root'),{request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},...(options.body?{body:JSON.stringify(options.body)}:{})});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});mount();</script></body></html>`;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(repo+path));return;}
        if(path==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}
        if(path==='/favicon.ico'){res.end();return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;
        calls.push({path,method:req.method,body,after:url.searchParams.get('after_id'),active:url.searchParams.get('active_only'),limit:url.searchParams.get('limit')});
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
        if(path===api+'/capabilities'){send({enabled:true,features:{model_probe:true}});return;}
        if(path===api+'/hosts'){send({hosts:[]});return;}if(path===api+'/projects'){send({projects:[]});return;}
        if(path==='/api/team/models'){send({models:[]});return;}
        if(path===api+'/operations'){
          assert.equal(url.searchParams.get('kind'),'model_probe');
          if(url.searchParams.get('active_only')==='true'){assert.equal(url.searchParams.get('limit'),'1');send({operations:['running','queued','cancel_requested'].includes(old.status)?[old]:[],next_cursor:null});return;}
          assert.equal(url.searchParams.get('limit'),'50');const after=url.searchParams.get('after_id');
          if(!after){send({operations:ops.slice(0,50),next_cursor:'op049'});return;}
          if(failPage){failPage=false;send({detail:'Temporary history failure'},503);return;}
          let data=after==='op049'?{operations:ops.slice(49,99),next_cursor:'op098'}:{operations:ops.slice(99),next_cursor:null};
          if(repeatCursor)data={operations:[{id:'bad-page',status:'completed',scope}],next_cursor:after};
          if(holdPage){holdPage=false;pendingPage=(stale=true)=>send({...data,operations:[...data.operations,...(stale?[{id:'stale-page',status:'completed',scope}]:[])]});heldResolve?.();heldResolve=null;return;}
          send(data);return;
        }
        if(path.startsWith(api+'/operations/')){
          const id=path.slice((api+'/operations/').length).split('/')[0],op=ops.find(item=>item.id===id);assert(op);
          if(path.endsWith('/cancel')){assert.equal(req.method,'POST');assert.deepEqual(body,{});op.status='cancel_requested';send({...op});return;}
          if(op.status==='cancel_requested')op.status='cancelled';send({...op});return;
        }
        send({detail:'Unknown route'},404);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));page.on('console',msg=>{if(msg.type()==='error')consoleErrors.push(msg.text());});page.setDefaultTimeout(7000);
        const origin='http://127.0.0.1:'+server.address().port;await page.goto(origin);
        const by=key=>page.locator('[data-engineering="'+key+'"]'),ids=()=>by('probe-history').locator('option').evaluateAll(options=>options.map(item=>item.value).filter(Boolean));
        await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-history"]').options.length>1);
        assert.equal(await by('probe-history').inputValue(),'old-active','active operation older than the newest 50 must be restored');
        await by('probe-operation-status').filter({hasText:'Running'}).waitFor();assert.equal((await ids()).length,51);
        assert.deepEqual((await ids()).slice(0,3),['op000','op001','op002'],'recent results keep server chronological order');
        assert.equal(calls.filter(call=>call.path===api+'/operations').length,2,'mount only fetches recent 50 plus one oldest active');
        assert.equal(calls.some(call=>call.method==='POST'),false,'restoration must not restart or cancel a probe');
        await by('probe-history').selectOption('op003');await by('probe-refresh-operations').click();await by('probe-more-history').waitFor();
        assert.equal(await by('probe-history').inputValue(),'op003','refresh preserves explicit selection');
        failPage=true;await by('probe-more-history').click();await by('probe-history-notice').filter({hasText:'Temporary history failure'}).waitFor();
        assert.equal(await by('probe-more-history').isEnabled(),true,'transient error leaves explicit retry available');assert.equal((await ids()).length,51);
        await by('probe-more-history').click();await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-history"]').options.length===101);
        assert.equal(await by('probe-history').inputValue(),'op003');assert.equal(new Set(await ids()).size,100,'overlapping page IDs are deduplicated');
        repeatCursor=true;await by('probe-more-history').click();await by('probe-history-notice').filter({hasText:'did not advance'}).waitFor();assert.equal(await by('probe-more-history').isDisabled(),true);
        assert.equal((await ids()).includes('bad-page'),false,'repeated cursor fails closed before merging');
        repeatCursor=false;await by('probe-refresh-operations').click();await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-history"]').options.length===52);
        // A superseded page cannot merge into refreshed state, even if its reply arrives later.
        holdPage=true;let held=nextHeld();await by('probe-more-history').click();await held;
        await by('probe-refresh-operations').click();await page.waitForFunction(()=>!document.querySelector('[data-engineering="probe-more-history"]').disabled);
        const late=page.waitForResponse(response=>response.url().includes('after_id=op049'));assert(pendingPage);pendingPage();pendingPage=null;await late;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal((await ids()).includes('stale-page'),false);assert.equal((await ids()).length,51);assert.equal(await by('probe-history').inputValue(),'op003');
        // A normal in-flight page also preserves a selection changed after its request.
        holdPage=true;held=nextHeld();await by('probe-more-history').click();await held;await by('probe-history').selectOption('op004');
        pendingPage(false);pendingPage=null;await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-history"]').options.length===101);
        assert.equal(await by('probe-history').inputValue(),'op004');await by('probe-history').selectOption('op003');
        await page.evaluate(()=>locale.applyUiLanguage('ru'));assert.equal(await by('probe-more-history').textContent(),'Загрузить ещё историю');
        assert.equal(await page.title(),'Probe history QA');assert.equal(page.url(),origin+'/');assert.equal(await page.locator('main b').count(),0);
        await by('probe-more-history').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/probe-history-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await by('probe-more-history').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/probe-history-mobile.png',animations:'disabled'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await by('probe-more-history').click();await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-history"]').options.length===107);
        assert.equal(new Set(await ids()).size,106);assert.equal(await by('probe-history').inputValue(),'op003');assert.equal(await by('probe-more-history').isHidden(),true,'end cursor hides pagination');
        await page.evaluate(()=>locale.applyUiLanguage('en'));await by('probe-refresh-operations').click();await page.waitForFunction(()=>document.querySelector('[data-engineering="probe-history"]').options.length===52);
        holdPage=true;held=nextHeld();await by('probe-more-history').click();await held;
        await page.evaluate(()=>{destroy();mount();});await by('probe-operation-status').filter({hasText:'Running'}).waitFor();
        assert(pendingPage);const destroyedReply=page.waitForResponse(response=>response.url().includes('after_id=op049'));pendingPage();pendingPage=null;await destroyedReply;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
        assert.equal((await ids()).includes('stale-page'),false);assert.equal(await by('probe-history').inputValue(),'old-active');
        assert.equal(calls.some(call=>call.method==='POST'),false,'refresh, paging, disposal, and remount never mutate an operation');
        await by('probe-cancel').click();await by('probe-operation-status').filter({hasText:'Cancelled'}).waitFor();
        assert.deepEqual(calls.filter(call=>call.method==='POST').map(call=>({path:call.path,body:call.body})),[{path:api+'/operations/old-active/cancel',body:{}}]);
        assert.deepEqual(errors,[]);assert(consoleErrors.every(text=>text.includes('503')),'only the deliberate recoverable HTTP error is allowed');console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=100, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
