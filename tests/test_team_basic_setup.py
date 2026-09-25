"""Basic setup selects an approved pool, never grants tools or cloud consent."""
import subprocess
import unittest
from pathlib import Path


class TeamBasicSetupTest(unittest.TestCase):
    def test_newly_materialized_chat_hash_is_a_safe_team_session_fallback(self):
        module = (Path(__file__).resolve().parents[1] / 'static/js/team-workspace.js').as_uri()
        script = r'''
import assert from 'node:assert/strict';
const {resolveTeamSessionId}=await import(process.argv[1]);
const id='0a3727a6-a86b-4b57-9073-71da3b5410da';
assert.equal(resolveTeamSessionId(null, '#'+id), id);
assert.equal(resolveTeamSessionId('', '#'+id), id);
assert.equal(resolveTeamSessionId('selected', '#'+id), 'selected');
assert.equal(resolveTeamSessionId(null, '#not-a-session'), null);
assert.equal(resolveTeamSessionId(null, '#'+id+'/other'), null);
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, module], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_team_can_start_after_same_tab_chat_materialization(self):
        if subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
            self.skipTest('Playwright unavailable')
        repo = str(Path(__file__).resolve().parents[1])
        script = r'''
const {chromium}=require('playwright'),http=require('http'),fs=require('fs'),assert=require('node:assert/strict');
(async()=>{
const repo=process.argv[1],id='0a3727a6-a86b-4b57-9073-71da3b5410da',starts=[];
const html=`<section id="team-workspace"></section><button id="mode-agent-btn">Agent</button><button id="mode-chat-btn">Chat</button><button id="mode-team-btn">Team</button><script type="module">
import {createTeamWorkspace} from '/static/js/team-workspace.js';
window.controller=createTeamWorkspace({getSessionId:()=>null,EventSourceImpl:class {close(){}},NotificationImpl:null});window.ready=controller.init();</script>`;
const server=http.createServer((req,res)=>{const path=new URL(req.url,'http://localhost').pathname;if(path.startsWith('/static/')){res.setHeader('Content-Type','application/javascript');res.end(fs.readFileSync(repo+path));}else{res.setHeader('Content-Type','text/html');res.end(html);}});
await new Promise(done=>server.listen(0,'127.0.0.1',done));
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
try {
const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/team/**',async route=>{const u=new URL(route.request().url());let data={};
if(u.pathname.endsWith('/capabilities'))data={enabled:true,host_enabled:true};
else if(u.pathname.endsWith('/models'))data={models:[{endpoint_id:'local',model:'kat',label:'KAT',local:true}]};
else if(u.pathname.endsWith('/presets'))data={presets:[]};
else if(u.pathname.endsWith('/profiles'))data={profiles:[]};
else if(u.pathname.endsWith('/start')){starts.push({path:u.pathname,body:route.request().postDataJSON()});data={};}
else if(u.pathname.includes('/session/'))data={team_id:null,workers:[],last_seq:0};
await route.fulfill({json:data});});
await page.goto('http://127.0.0.1:'+server.address().port);await page.evaluate(()=>ready);
await page.locator('#mode-team-btn').click();await page.getByText('Select an existing chat before starting a team.').waitFor();
await page.locator('#mode-chat-btn').click();await page.evaluate(id=>history.replaceState(null,'','#'+id),id);
await page.locator('#mode-team-btn').click();
await page.getByLabel('Goal and completion criteria').fill('QA arithmetic 19 x 7 = 133, no tools.');
await page.getByLabel('Absolute project directory on the host').fill('/tmp/odysseus-team-qa-20260925');
await page.getByLabel('Endpoint and model').first().selectOption(JSON.stringify(['local','kat']));
await page.getByRole('checkbox',{name:'KAT',exact:true}).check();
await page.getByRole('button',{name:'Start team',exact:true}).click();
await page.waitForFunction(()=>document.querySelector('.team-notice[role="status"]')?.textContent==='Saved.');
assert.equal(starts.length,1);assert.equal(starts[0].path,'/api/team/session/'+id+'/start');assert.deepEqual(errors,[]);
}finally{await browser.close();await new Promise(done=>server.close(done));}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, repo], capture_output=True, text=True, timeout=50)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_team_resolves_fetch_after_csrf_wrapper_install(self):
        module = (Path(__file__).resolve().parents[1] / 'static/js/team-workspace.js').as_uri()
        script = r'''
import assert from 'node:assert/strict';
globalThis.document={getElementById(){return null;}};
globalThis.sessionStorage={};
const {createTeamWorkspace}=await import(process.argv[1]);
const original=globalThis.fetch;
globalThis.fetch=async()=>{throw new Error('captured too early');};
const controller=createTeamWorkspace({getSessionId:()=>null,root:null,modeButton:null});
let calls=0;
globalThis.fetch=async()=>{calls++;return {ok:true,status:200,json:async()=>({})};};
assert.equal(await controller.init(),false);
assert.equal(calls,0,'null UI exits before any request');
// Source contract: the default fetch must be resolved inside request(), not
// captured in createTeamWorkspace's default parameter.
assert.equal(createTeamWorkspace.toString().includes('fetchImpl = null'),true);
assert.equal(createTeamWorkspace.toString().includes('fetchImpl || globalThis.fetch.bind(globalThis)'),true);
assert.equal(createTeamWorkspace.toString().includes("headers.set('X-Odysseus-CSRF'"),true);
globalThis.fetch=original;
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, module], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_basic_controls_in_browser(self):
        probe = subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True)
        if probe.returncode:
            self.skipTest('Playwright unavailable')
        repo = str(Path(__file__).resolve().parents[1])
        script = r'''
const {chromium}=require('playwright'),http=require('http'),fs=require('fs'),assert=require('node:assert/strict');
(async()=>{
const repo=process.argv[1],calls=[],models=Array.from({length:6},(_,i)=>({endpoint_id:`e${i}`,model:'same-name',label:`Host ${i} / same-name`,local:true}));
const html=`<section id="team-workspace"></section><button id="mode-team-btn">Team</button><script type="module">
import {createTeamWorkspace} from '/static/js/team-workspace.js';
window.controller=createTeamWorkspace({getSessionId:()=> 'chat-1',EventSourceImpl:class {close(){}},NotificationImpl:null});window.ready=controller.init();</script>`;
const server=http.createServer((req,res)=>{if(req.url.startsWith('/static/')){res.setHeader('Content-Type','application/javascript');res.end(fs.readFileSync(repo+req.url));}else {res.setHeader('Content-Type','text/html');res.end(html);}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
try {
const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/team/**',async route=>{const url=new URL(route.request().url());let result={};
if(url.pathname.endsWith('/capabilities'))result={enabled:true,host_enabled:true};
else if(url.pathname.endsWith('/models'))result={models};
else if(url.pathname.endsWith('/presets'))result={presets:[]};
else if(url.pathname.endsWith('/profiles'))result={profiles:[]};
else if(url.pathname.endsWith('/start')){calls.push(route.request().postDataJSON());result={};}
else if(url.pathname.includes('/session/'))result={team_id:null,workers:[],last_seq:0};
await route.fulfill({json:result});});
await page.goto('http://127.0.0.1:'+server.address().port);await page.evaluate(()=>ready);await page.locator('#mode-team-btn').click();
const mode=page.getByLabel('Team setup',{exact:true});await mode.waitFor();assert.equal(await mode.inputValue(),'basic');
assert.equal(await page.getByLabel('Install command (optional)',{exact:true}).isVisible(),false);
await page.getByLabel('Goal and completion criteria',{exact:true}).fill('Build and test');
await page.getByLabel('Absolute project directory on the host',{exact:true}).fill('/work/project');
await page.getByLabel('Endpoint and model',{exact:true}).first().selectOption(JSON.stringify(['e0','same-name']));
for(let i=0;i<6;i++)await page.getByRole('checkbox',{name:`Host ${i} / same-name`,exact:true}).check();
await mode.selectOption('advanced');await page.getByLabel('Install command (optional)',{exact:true}).fill('npm ci');
await mode.selectOption('basic');await mode.selectOption('advanced');assert.equal(await page.getByLabel('Install command (optional)',{exact:true}).inputValue(),'npm ci');
await mode.selectOption('basic');await page.getByRole('button',{name:'Start team',exact:true}).click();
await page.waitForFunction(()=>document.querySelector('.team-notice[role="status"]')?.textContent==='Saved.');
assert.equal(calls.length,1);assert.equal(calls[0].workers.length,6);assert.equal(calls[0].config.auto_dispatch,true);assert.equal(calls[0].config.trusted_host,false);assert.equal(calls[0].config.external,false);
assert.deepEqual(errors,[]);
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, repo], capture_output=True, text=True, timeout=50)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_basic_pool_and_advanced_contract(self):
        module = (Path(__file__).resolve().parents[1] / 'static/js/team-workspace.js').as_uri()
        script = r'''
import assert from 'node:assert/strict';
const {normalizeTeamStart} = await import(process.argv[1]);
const models = Array.from({length: 70}, (_, i) => ({endpoint_id: `endpoint-${i}`, model: 'same-name', local: true}));
const base = {goal: 'Implement and verify the project', project_path: '/work/project', leader: models[0],
  setup_mode: 'basic', worker_pool: models, config: {auto_dispatch:false, auto_continue:false, trusted_host:false, web:false, external:false}};
const value = normalizeTeamStart(base, models);
assert.equal(value.workers.length, 70, 'page size is not a team limit; identical model names at different endpoints remain distinct');
assert.equal(value.config.auto_dispatch, true);
assert.equal(value.config.auto_continue, true);
for (const permission of ['trusted_host','web','external']) assert.equal(value.config[permission],false);
assert.equal(value.workers.every(w => !w.objective && w.role === 'executor'),true,'leader assigns objectives, not fabricated worker tasks');
assert.equal(normalizeTeamStart({...base, worker_pool:[models[1],models[1]]},models).workers.length,1);
assert.throws(()=>normalizeTeamStart({...base,worker_pool:[]},models),/worker model/);
assert.throws(()=>normalizeTeamStart({...base,worker_pool:[{endpoint_id:'missing',model:'same-name'}]},models),/known endpoint/);
const paid = {endpoint_id:'paid',model:'same-name',local:false};
assert.throws(()=>normalizeTeamStart({...base,worker_pool:[paid]},[...models,paid]),/External models are disabled/);
assert.throws(()=>normalizeTeamStart({...base,worker_pool:[paid],config:{external:true}},[...models,paid]),/approval|budget/);
const advanced = normalizeTeamStart({...base,setup_mode:'advanced',workers:[{...models[2],objective:'Run tests',role:'reviewer',write_scope:[]}]},models);
assert.equal(advanced.config.auto_dispatch,false);
assert.equal(advanced.config.auto_continue,false);
assert.equal(advanced.workers.length,1);
assert.equal(advanced.workers[0].objective,'Run tests');
assert.equal(advanced.workers[0].role,'reviewer');
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, module], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
