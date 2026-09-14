"""Actual picker UI: equal model names remain selectable by endpoint on reload."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_duplicate_models_select_and_restore_distinct_endpoints(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    if subprocess.run(["node", "-e", "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip("Playwright is unavailable")
    root = Path(__file__).resolve().parents[1]
    script = r"""
      const {chromium}=require('playwright'),fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
      const root=process.argv[1],out=process.argv[2],requests=[],errors=[];
      const shared='qwen3.6-35b-a3b',items=[
        {endpoint_id:'jetson',endpoint_name:'Local Qwen',url:'http://jetson:11434/v1',category:'local',models:[shared]},
        {endpoint_id:'mac',endpoint_name:'Local Qwen',url:'http://mac:1234/v1',category:'local',models:[shared]},
        {endpoint_id:'cloud-a',endpoint_name:'Cloud',url:'https://a.example/v1',category:'api',models:[shared]},
        {endpoint_id:'cloud-b',endpoint_name:'Cloud',url:'https://b.example/a-very-long-routing-path/that-must-remain-visible/without-ellipsis/v1',category:'api',models:[shared]},
      ];
      const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Odysseus model endpoint QA</title><link rel="stylesheet" href="/static/style.css"></head><body><main class="chat-container" style="height:100dvh"><div class="chat-history" id="chat-history">Model endpoint selection</div><div class="chat-input-bar"><div class="chat-input-top"><textarea id="message">Keep my unsent message</textarea><div class="model-picker-wrap" id="model-picker-wrap"><button type="button" class="model-picker-btn" id="model-picker-btn"><span id="model-picker-label"></span></button><div class="model-picker-menu hidden" id="model-picker-menu"><div class="model-picker-search-row"><input type="text" id="model-picker-search" aria-label="Search models"></div><div class="model-picker-list" id="model-picker-list"></div></div></div></div></div></main>
      <script type="module">import {initModelPicker,updateModelPicker} from '/static/js/modelPicker.js';
      window.catalog=${JSON.stringify(items)};window.current=JSON.parse(localStorage.getItem('qa-current')||'null')||{id:'chat',model:${JSON.stringify(shared)},endpoint_id:'jetson',endpoint_url:${JSON.stringify(items[0].url)}};
      window.modelsModule={getCachedItems:()=>catalog};
      const realFetch=window.fetch;window.fetch=async(url,options)=>{if(options?.method==='PATCH'){window.captured=Object.fromEntries(options.body);localStorage.setItem('qa-current',JSON.stringify(current));return new Response('{}',{status:200});}return realFetch(url,options);};
      initModelPicker({getCurrentSessionId:()=>current.id,getSessions:()=>[current],getPendingChat:()=>null,setPendingChat:()=>{},createDirectChat:()=>{throw Error('Unexpected new chat');}});updateModelPicker();window.ready=true;</script></body></html>`;
      const stubs={'/static/js/providers.js':"export const providerLogo=()=>'';",'/static/js/ui.js':"export default {showToast(){},showError(message){throw Error(message);}};",'/static/js/settings.js':"export default {};",'/static/js/spinner.js':"export default {};"};
      const server=http.createServer((req,res)=>{const pathname=new URL(req.url,'http://localhost').pathname;
        if(stubs[pathname]){res.setHeader('Content-Type','text/javascript');res.end(stubs[pathname]);}
        else if(pathname.startsWith('/static/')){res.setHeader('Content-Type',pathname.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(root+pathname));}
        else{res.setHeader('Content-Type','text/html');res.end(html);}});
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
        const base='http://127.0.0.1:'+server.address().port;await page.goto(base);await page.waitForFunction(()=>window.ready);
        assert.equal(await page.title(),'Odysseus model endpoint QA');assert.equal(new URL(page.url()).origin,base);
        await page.locator('#model-picker-btn').click();
        assert.equal(await page.locator('.model-switch-item').count(),4);
        assert.equal(new Set(await page.locator('.model-switch-ep').allTextContents()).size,4);
        for (const item of items) assert((await page.locator('.model-switch-item[data-endpoint-id="'+item.endpoint_id+'"] .model-switch-ep').textContent()).includes(item.url));
        await page.locator('.model-switch-item').first().hover();
        await page.screenshot({path:out+'/model-endpoints-desktop.png',animations:'disabled'});
        await page.locator('.model-switch-item[data-endpoint-id="mac"]').click();await page.evaluate(()=>window.__odysseusModelSwitchPromise);
        assert.deepEqual(await page.evaluate(()=>captured),{model:shared,endpoint_url:items[1].url,endpoint_id:'mac'});
        assert((await page.locator('#model-picker-label').textContent()).includes('mac:1234'));assert.equal(await page.locator('#message').inputValue(),'Keep my unsent message');
        await page.reload();await page.waitForFunction(()=>window.ready);
        assert.equal(await page.evaluate(()=>current.endpoint_id),'mac');assert((await page.locator('#model-picker-label').textContent()).includes('mac:1234'));
        await page.locator('#model-picker-btn').click();await page.getByLabel('Search models').fill('b.example');
        assert.equal(await page.locator('.model-switch-item').count(),1);await page.locator('.model-switch-item').click();await page.evaluate(()=>window.__odysseusModelSwitchPromise);
        assert.equal(await page.evaluate(()=>captured.endpoint_id),'cloud-b');assert.equal(await page.evaluate(()=>captured.endpoint_url),items[3].url);
        await page.reload();await page.waitForFunction(()=>window.ready);assert.equal(await page.evaluate(()=>current.endpoint_id),'cloud-b');
        await page.setViewportSize({width:390,height:844});await page.locator('#model-picker-btn').click();
        assert.equal(await page.locator('.model-switch-item').count(),4);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        assert(await page.locator('.model-switch-ep').evaluateAll(nodes=>nodes.every(node=>getComputedStyle(node).textOverflow!=='ellipsis' && node.scrollWidth<=node.clientWidth+1)));
        await page.locator('.model-switch-item').first().hover();
        await page.screenshot({path:out+'/model-endpoints-mobile.png',animations:'disabled'});
        assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true}));
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(["node", "-e", "(async()=>{" + script + "})().catch(error=>{console.error(error);process.exit(1);});", str(root), str(tmp_path)], capture_output=True, text=True, timeout=60, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
