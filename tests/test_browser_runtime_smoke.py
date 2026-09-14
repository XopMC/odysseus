"""Opt-in real pinned MCP/browser smoke, serving only an owned loopback fixture.

This proves two separate MCP processes, not Team's per-task routing or sandboxing.
Run with ODYSSEUS_REAL_BROWSER_QA=1 and ODYSSEUS_BROWSER_EXECUTABLE set.
The pinned package must already be cached: test startup is npm-offline.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from src.browser_runtime import PLAYWRIGHT_MCP_PACKAGE


@unittest.skipUnless(os.environ.get('ODYSSEUS_REAL_BROWSER_QA') == '1', 'explicit real-browser QA opt-in required')
class BrowserRuntimeSmoke(unittest.TestCase):
    def test_pinned_mcp_navigation_and_separate_process_storage(self):
        executable = os.environ.get('ODYSSEUS_BROWSER_EXECUTABLE', '')
        self.assertTrue(Path(executable).is_file(), 'a real browser executable is required')
        script = r'''
const {spawn}=require('node:child_process'),http=require('node:http'),readline=require('node:readline'),assert=require('node:assert/strict');
const [pkg,executable]=process.argv.slice(1),children=[];
const server=http.createServer((req,res)=>{
  res.setHeader('Content-Type','text/html');res.setHeader('Cache-Control','no-store');
  res.end('<!doctype html><title>Owned MCP fixture</title><h1 id="value">loading</h1><script>'+
    (req.url==='/set'?'localStorage.setItem("qa","private-A");document.cookie="qa=private-A; SameSite=Strict";':'')+
    'document.getElementById("value").textContent=(localStorage.getItem("qa")||"empty")+" / "+(document.cookie||"no-cookie");</script>');
});
function client(){
  const child=spawn('npx',['--no-install',pkg,'--headless','--isolated','--executable-path',executable],{env:{...process.env,npm_config_offline:'true'},stdio:['pipe','pipe','pipe']});
  children.push(child);let seq=0,stderr='';const pending=new Map();
  child.stderr.on('data',data=>stderr=(stderr+data).slice(-4000));
  readline.createInterface({input:child.stdout}).on('line',line=>{
    let message;try{message=JSON.parse(line)}catch{return}
    const item=pending.get(message.id);if(item){pending.delete(message.id);clearTimeout(item.timer);message.error?item.reject(Error(JSON.stringify(message.error))):item.resolve(message.result)}
  });
  child.on('exit',()=>{for(const item of pending.values()){clearTimeout(item.timer);item.reject(Error('MCP exited: '+stderr))}pending.clear()});
  const send=(method,params)=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(Error('MCP timeout: '+method+' '+stderr))},25000);pending.set(id,{resolve,reject,timer});child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n')});
  return {send,async init(){const reply=await send('initialize',{protocolVersion:'2024-11-05',capabilities:{},clientInfo:{name:'odysseus-owned-qa',version:'1'}});assert(reply.serverInfo);child.stdin.write(JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'})+'\n')},async tool(name,args){const result=await send('tools/call',{name,arguments:args});assert.notEqual(result.isError,true,JSON.stringify(result));return JSON.stringify(result)}};
}
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const url='http://127.0.0.1:'+server.address().port,a=client(),b=client();
  try{
    await Promise.all([a.init(),b.init()]);
    const tools=await a.send('tools/list',{});assert(tools.tools.some(tool=>tool.name==='browser_navigate'));assert(tools.tools.some(tool=>tool.name==='browser_snapshot'));
    const first=await a.tool('browser_navigate',{url:url+'/set'});assert(first.includes('Owned MCP fixture'),first);
    assert((await a.tool('browser_snapshot',{})).includes('private-A'));
    await b.tool('browser_navigate',{url:url+'/view'});
    const other=await b.tool('browser_snapshot',{});assert(other.includes('empty / no-cookie'));assert(!other.includes('private-A'));
    await a.tool('browser_navigate',{url:url+'/view'});const retained=await a.tool('browser_snapshot',{});assert(retained.includes('private-A'));
    const snapshot=await b.tool('browser_snapshot',{});assert(snapshot.includes('empty / no-cookie'));
    const invalid=await b.send('tools/call',{name:'browser_navigate',arguments:{}});assert.equal(invalid.isError,true);
    await Promise.all([a.tool('browser_close',{}),b.tool('browser_close',{})]);console.log('PASS: pinned MCP handshake/navigation/storage separation/error');
  }finally{for(const child of children){child.stdin.end();child.kill('SIGTERM')}server.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        with tempfile.TemporaryDirectory(prefix='odysseus-mcp-qa-') as workspace:
            result = subprocess.run(['node', '-e', script, PLAYWRIGHT_MCP_PACKAGE, executable],
                                    cwd=workspace, capture_output=True, text=True, timeout=100)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS:', result.stdout)
