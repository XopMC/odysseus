"""Saved baseline comparison UI: command outcomes only, read-only and scoped."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_baseline_browser_saved_runs_scope_and_stale_responses(tmp_path):
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Node/Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs');
      const {chromium}=require('playwright'),repo=process.argv[1],out=process.argv[2],api='/api/team/engineering',calls=[],errors=[];
      const projects=[{id:'A',name:'Project <b>Save</b>',root:'/work/A',host_id:'host-A',revision:4,access_mode:null},{id:'B',name:'Other project',root:'/work/B',host_id:'host-B',revision:2,access_mode:null}];
      const common={profile_id:'Profile <b>same</b>',profile_revision:2,command_hash:'a'.repeat(64),project_revision:4,host_id:'host-A',workspace_hash:'b'.repeat(64),finished_at:150,status:'failed',evidence:{exit_code:1,toolchain:{compiler:'clang <b>custom</b>'}}};
      const runs=Array.from({length:55},(_,i)=>({...common,id:'check-'+String(i).padStart(3,'0'),kind:'check',started_at:100-i}));
      const baseline={...common,id:'old-baseline',kind:'baseline',started_at:1,finished_at:2};runs.push(baseline);
      let enabled=false,classification='failure_persists',failPage=false,repeatCursor=false,failCompare=false,holdCompare=false,pendingCompare,onHeld;
      const held=()=>new Promise(resolve=>onHeld=resolve);
      const view=run=>{const {evidence,...rest}=run;return {...rest,exit_code:evidence?.exit_code??null,reported_toolchain:evidence?.toolchain??null};};
      const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Baseline QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>html,body{height:auto;overflow:visible}body{display:block}main{max-width:1000px;margin:auto;padding:12px}</style></head><body><main id="root"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';import * as locale from '/static/js/i18n.js';window.locale=locale;window.mount=()=>window.destroy=mountEngineeringWorkspace(document.getElementById('root'),{request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET'});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});mount();</script></body></html>`;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),path=url.pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(repo+path));return;}
        if(path==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}if(path==='/favicon.ico'){res.end();return;}
        calls.push({path,method:req.method,query:url.search});assert.equal(req.method,'GET','comparison must never execute or mutate');
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');res.end(JSON.stringify(data));};
        if(path===api+'/capabilities'){send({enabled:true,features:{projects:true,baseline_comparison:enabled}});return;}
        if(path===api+'/hosts'){send({hosts:[]});return;}if(path===api+'/projects'){send({projects});return;}
        if(path.endsWith('/check-runs')){
          assert.equal(url.searchParams.get('limit'),'50');const after=url.searchParams.get('after_id'),source=path.includes('/A/')?runs:[];
          if(failPage&&after){failPage=false;send({detail:'History unavailable'},503);return;}
          if(after&&!source.some(row=>row.id===after)){send({detail:'Foreign cursor'},404);return;}
          const start=after?source.findIndex(row=>row.id===after)+1:0,page=source.slice(start,start+50);
          send({runs:page,next_cursor:repeatCursor&&after?after:start+50<source.length?page.at(-1).id:null});return;
        }
        if(path.endsWith('/check-comparison')){
          assert.equal(path,api+'/projects/A/check-comparison');assert.equal(url.searchParams.get('baseline_run_id'),'old-baseline');assert.equal(url.searchParams.get('check_run_id'),'check-054');
          if(failCompare){failCompare=false;send({detail:'Comparison unavailable'},503);return;}
          const after={...runs[54],workspace_hash:'d'.repeat(64)};
          const pair={remained_passing:['passed','passed'],became_failing:['passed','failed'],became_passing:['failed','passed'],failure_persists:['failed','failed']};
          const before=view({...baseline,status:pair[classification]?.[0]||'failed'}),afterView=view({...after,status:pair[classification]?.[1]||'failed'});
          before.exit_code=before.status==='passed'?0:1;afterView.exit_code=afterView.status==='passed'?0:2;
          const result={classification,comparable:classification!=='not_comparable',reasons:classification==='not_comparable'?['profile_revision_changed','reported_environment_changed']:[],scope:'command_outcome_only',individual_failures_compared:false,before,after:afterView};
          if(holdCompare){holdCompare=false;pendingCompare=()=>send(result);onHeld();onHeld=null;return;}send(result);return;
        }
        send({detail:'Unexpected route'},404);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));page.setDefaultTimeout(10000);const origin='http://127.0.0.1:'+server.address().port,by=key=>page.locator('[data-engineering="'+key+'"]');
        await page.goto(origin);await by('notice').filter({hasText:'No hosts are configured'}).waitFor();assert.equal(await by('baseline-comparison').count(),1);assert.equal(await by('baseline-comparison').isHidden(),true);assert.equal(calls.some(call=>call.path.endsWith('/check-runs')),false);
        enabled=true;await page.evaluate(()=>{destroy();mount();});await by('notice').filter({hasText:'No hosts are configured'}).waitFor();await by('project').selectOption('A');await by('baseline-more').waitFor();assert.equal(await by('baseline-before').locator('option').count(),1);assert.equal(await by('baseline-compare').isDisabled(),true);
        failPage=true;await by('baseline-more').click();await by('baseline-history-notice').filter({hasText:'Unable to load saved runs'}).waitFor();await by('baseline-more').click();await by('baseline-before').locator('option[value="old-baseline"]').waitFor({state:'attached'});
        assert.equal(await by('baseline-after').locator('option').count(),56);await by('baseline-before').selectOption('old-baseline');await by('baseline-after').selectOption('check-054');assert.equal(calls.some(call=>call.path.endsWith('/check-comparison')),false,'selection never compares automatically');
        await by('baseline-compare').click();await by('baseline-result').filter({hasText:'Both commands failed'}).waitFor();assert((await by('baseline-result').textContent()).includes('does not mean the errors are identical'));assert((await by('baseline-result').textContent()).includes('clang <b>custom</b>'));assert.equal(await page.locator('main b').count(),0);
        for(const [value,label] of [['remained_passing','Both commands passed'],['became_failing','Command changed from passing to failing'],['became_passing','Command changed from failing to passing']]){classification=value;await by('baseline-compare').click();await by('baseline-result').filter({hasText:label}).waitFor();}
        classification='not_comparable';await by('baseline-compare').click();await by('baseline-result').filter({hasText:'Outcomes are not comparable'}).waitFor();assert((await by('baseline-result').textContent()).includes('Approved profile revision changed'));assert((await by('baseline-result').textContent()).includes('Reported environment changed'));
        failCompare=true;await by('baseline-compare').click();await by('baseline-notice').filter({hasText:'Unable to compare'}).waitFor();assert.equal(await by('baseline-result').textContent(),'');await by('baseline-compare').click();await by('baseline-result').filter({hasText:'Outcomes are not comparable'}).waitFor();
        await page.evaluate(()=>locale.applyUiLanguage('ru'));assert.equal(await by('baseline-compare').textContent(),'Сравнить сохранённые результаты');assert((await by('baseline-result').textContent()).includes('Результаты несопоставимы'));assert((await by('baseline-result').textContent()).includes('clang <b>custom</b>'));
        await by('baseline-result').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/baseline-desktop.png',animations:'disabled'});await page.setViewportSize({width:390,height:844});await by('baseline-result').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/baseline-mobile.png',animations:'disabled'});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        await page.evaluate(()=>locale.applyUiLanguage('en'));assert.equal(await by('baseline-compare').textContent(),'Compare saved outcomes');await by('baseline-refresh').click();await by('baseline-history-notice').filter({hasText:'Saved runs loaded'}).waitFor();assert.equal(await by('baseline-before').inputValue(),'old-baseline');assert.equal(await by('baseline-after').inputValue(),'check-054','refresh preserves selected saved rows beyond first page');assert.equal(await by('baseline-result').textContent(),'');
        repeatCursor=true;await by('baseline-more').click();await by('baseline-history-notice').filter({hasText:'cursor did not advance'}).waitFor();assert.equal(await by('baseline-more').isDisabled(),true);repeatCursor=false;await by('baseline-refresh').click();await by('baseline-history-notice').filter({hasText:'Saved runs loaded'}).waitFor();
        holdCompare=true;const waitHeld=held();await by('baseline-compare').click();await waitHeld;await by('project').selectOption('B');const late=page.waitForResponse(response=>response.url().includes('/A/check-comparison'));pendingCompare();await late;await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));assert.equal(await by('baseline-result').textContent(),'');assert.equal(await by('baseline-before').inputValue(),'');
        assert(calls.every(call=>call.method==='GET'));assert.equal(await page.title(),'Baseline QA');assert.equal(page.url(),origin+'/');assert.deepEqual(errors,[]);console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=120, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
