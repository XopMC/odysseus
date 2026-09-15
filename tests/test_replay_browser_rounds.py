"""Real DOM replay rendering; transport/dependencies are controlled fixtures."""
import os
from pathlib import Path
import subprocess
import unittest


class ReplayBrowserTests(unittest.TestCase):
    def test_two_clients_keep_rounds_and_tools_separate(self):
        if subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
            self.skipTest('Playwright unavailable')
        root = Path(__file__).resolve().parents[1]
        script = r'''
const {chromium}=require('playwright'),fs=require('fs'),assert=require('node:assert/strict');
(async()=>{
const raw=fs.readFileSync(process.argv[1]+'/static/js/chat.js','utf8');
const source=raw.slice(raw.indexOf('export async function resumeStream'),raw.indexOf('// This only disconnects',raw.indexOf('export async function resumeStream'))).replace('export ','');
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
try {
 const pages=[];
 for(const viewport of [{width:1280,height:900},{width:390,height:844}]) {
  const page=await browser.newPage({viewport});pages.push(page);const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<meta charset="utf-8"><title>Replay round QA</title><main id="chat-history"></main><textarea id="draft">unsent draft</textarea>');
  await page.addStyleTag({path:process.argv[1]+'/static/style.css'});
  await page.evaluate(({source})=>{
   const noop=()=>{},API_BASE='',_resumingStreams=new Map(),_streamRunIds=new Map(),_streamGenerations=new Map([['s1',1]]);
   const sessionModule={getCurrentSessionId:()=> 's1',getSessionViewToken:()=>1,getSessions:()=>[{id:'s1',model:'qwen'}],refreshSessionHistory:async()=>{window.refreshed=(window.refreshed||0)+1;}};
   const uiModule={esc:v=>v,scrollHistory:noop},markdownModule={normalizeThinkingMarkup:v=>v,mdToHtml:v=>v,squashOutsideCode:v=>v};
   const spinnerModule={create:()=>({createElement:()=>document.createElement('span'),start:noop,destroy:noop})};
   const chatRenderer={recordSessionMetricsCost:noop,localizeToolNode:noop,addMessage:noop};
   const updateSubmitButton=noop,applyStreamContextUsage=noop,displayMetrics=noop,bindUiText=noop;
   const createTerminalStreamError=v=>v,documentModule=null;
   const _shortModel=v=>v,_applyModelColor=noop,_setRoleModelLabel=noop,_streamDisplayText=v=>v;
   const inheritModelRouteState=(previous,unused,next)=>{next._actualModel=previous._actualModel;};
   const refreshChatContextHeader=noop,hasActiveStream=()=>false,cancelResumedStream=noop;
   let controller;const body=new ReadableStream({start:c=>controller=c});
   const fetch=async()=>({ok:true,headers:new Headers({'X-Odysseus-Run-Id':'r1'}),body});
   const resume=eval('('+source+')');window.replay=resume('s1');
   window.pushReplay=events=>controller.enqueue(new TextEncoder().encode(events.map((e,i)=>'id: '+e.id+'\ndata: '+JSON.stringify(e.data)).join('\n\n')+'\n\n'));
   window.finishReplay=()=>controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));
  },{source});
  await page.evaluate(()=>pushReplay([{id:1,data:{delta:'Первый шаг'}},{id:2,data:{type:'tool_start',tool:'bash',command:'echo ok'}},{id:3,data:{type:'tool_output',tool:'bash',output:'ok',exit_code:0}},{id:4,data:{type:'agent_step'}},{id:5,data:{delta:'Второй шаг'}}]));
  await page.waitForFunction(()=>document.querySelectorAll('#chat-history > .msg').length===2);
  assert.equal(await page.locator('#chat-history > .msg').nth(0).locator('.stream-content').textContent(),'Первый шаг');
  assert.equal(await page.locator('#chat-history > .msg').nth(1).locator('.stream-content').textContent(),'Второй шаг');
  assert.equal(await page.locator('#chat-history > .agent-thread').count(),1);
  assert.equal(await page.locator('#chat-history > .agent-thread .agent-thread-node').count(),1);
  assert.equal(await page.locator('#chat-history > .agent-thread .agent-tool-output pre').textContent(),'ok');
  assert.equal(await page.locator('#chat-history > .msg details.agent-tool-output').count(),0);
  assert.equal(await page.locator('#draft').inputValue(),'unsent draft');
  assert.deepEqual(errors,[]);
 }
 for(const page of pages) {
  await page.evaluate(()=>finishReplay());await page.evaluate(()=>replay);
  assert.equal(await page.evaluate(()=>refreshed),1);
  assert.equal(await page.locator('#chat-history > .msg').count(),0);
  assert.equal(await page.locator('#draft').inputValue(),'unsent draft');
 }
} finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(root)], capture_output=True,
                                text=True, timeout=45, env=os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stderr)
