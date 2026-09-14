"""Real submit handler must preserve visible tool choices across async preflight."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_submit_preserves_clicked_permissions_and_never_grants_shell_from_api_text():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module = Path(__file__).resolve().parents[1] / "static/js/chat.js"
    script = r"""
      const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
      const code = fs.readFileSync(process.argv[1], 'utf8');
      const noop = () => {};
      const cases = [
        {mode: 'agent', web: true, bash: false, message: 'Read the GitHub API, do not run commands.', expected: {mode:'agent', allow_web_search:'true', allow_bash:'false'}},
        {mode: 'agent', web: false, bash: false, message: 'Review this API project without shell or web.', expected: {mode:'agent', allow_web_search:'false', allow_bash:'false'}},
        {mode: 'chat', web: true, bash: false, message: 'Hello there.', expected: {mode:'chat', use_web:'true', allow_web_search:null, allow_bash:'false'}},
        {mode: 'chat', web: true, bash: false, plan: true, message: 'Propose a plan.', expected: {mode:'agent', plan_mode:'true', allow_web_search:'true', allow_bash:'false', use_research:null}},
        {mode: 'chat', web: false, bash: false, research: true, message: 'Investigate public sources.', expected: {mode:'chat', plan_mode:'false', use_research:'true', allow_bash:'false'}},
      ];
      const scenario = cases[Number(process.argv[2])];
      assert(scenario, 'unknown submit scenario');
      {
        class Element {
          constructor() {this.value='';this.checked=false;this.children=[];this.style={setProperty:noop};this.dataset={};this.parentNode=null;this.innerHTML='';this.active=false;
            this.classList={add:noop,remove:noop,toggle:noop,contains:key=>key==='active'&&this.active};}
          querySelector() {return new Element();} querySelectorAll() {return [];}
          appendChild(child) {this.children.push(child);return child;} addEventListener() {} removeEventListener() {}
          dispatchEvent() {} setAttribute() {} removeAttribute() {} focus() {} blur() {} remove() {} closest() {return null;}
        }
        const ids = Object.fromEntries(['message','web-toggle','bash-toggle','research-toggle','rag-toggle','plan-toggle','incognito-toggle','mode-agent-btn','mode-chat-btn','chat-history','research-toggle-btn'].map(id=>[id,new Element()]));
        ids.message.value=scenario.message;
        for(const key of ['web','bash','research','plan']) ids[key+'-toggle'].checked=!!scenario[key];
        ids['rag-toggle'].checked=true;
        ids['mode-agent-btn'].active=scenario.mode==='agent';
        ids['mode-chat-btn'].active=scenario.mode==='chat';
        const button=new Element(), root=new Element();
        const document={body:root, addEventListener:noop, removeEventListener:noop,
          getElementById:id=>ids[id]||null, querySelector:s=>s==='.send-btn'?button:null,
          querySelectorAll:()=>[], createElement:()=>new Element(), createTextNode:t=>({textContent:t})};
        const stored={mode:scenario.mode,plan_mode:!!scenario.plan};
        const stub = extras => new Proxy(extras||{}, {get:(o,k)=>k in o?o[k]:noop});
        const sessions=stub({getCurrentSessionId:()=> 'chat', getSessions:()=>[{id:'chat',model:'qwen'}],getCurrentModel:()=> 'qwen',hasPendingChat:()=>false,getPendingChat:()=>null});
        const ui=stub({el:id=>ids[id]||null,esc:x=>x});
        const renderer=stub({addMessage:()=>new Element(),modelRouteLabel:()=> 'qwen'});
        let preflightChanged=false, sent=null;
        const file=stub({getPendingCount:()=>0,uploadPending:async()=>{
          await Promise.resolve();
          preflightChanged=true;
          ids['web-toggle'].checked=!scenario.web;
          ids['bash-toggle'].checked=true;
          ids['research-toggle'].checked=!scenario.research;
          ids['plan-toggle'].checked=!scenario.plan;
          stored.mode=scenario.mode==='agent'?'chat':'agent';stored.plan_mode=!scenario.plan;
          return [];
        }});
        const spinner=stub({create:()=>({element:new Element(),createElement:()=>new Element(),start:noop,destroy:noop,updateMessage:noop})});
        const storage=stub({loadToggleState:()=>stored,KEYS:{WORKSPACE:'workspace'},get:()=>'',getJSON:()=>({})});
        const modules={'storage.js':storage,'sessions.js':sessions,'ui.js':ui,'chatRenderer.js':renderer,'fileHandler.js':file,'spinner.js':spinner,'slashCommands.js':stub({getSetupMode:()=>false}),'presets.js':stub({getInject:()=>({prefix:'',suffix:''})})};
        const window={sessionModule:sessions,innerWidth:1440,dispatchEvent:noop,location:{hash:''},_updateSendBtnIcon:noop};
        const errors=[];
        const context=vm.createContext({document,window,navigator:{},performance:{now:()=>0},console:{log:noop,info:noop,warn:noop,error:(...args)=>errors.push(args.map(String).join(' '))},
          MutationObserver:class{observe(){}},CustomEvent:class{},Event:class{},FormData,AbortController,
          setTimeout:()=>0,clearTimeout:noop,setInterval:()=>0,clearInterval:noop,
          localStorage:{getItem:()=>null,setItem:noop},requestAnimationFrame:()=>0,cancelAnimationFrame:noop,
          fetch:async(url,options)=>{
            if(String(url).endsWith('/api/chat_stream')){sent=options.body;return {ok:false,status:401,text:async()=> 'Test request captured'};}
            return {ok:false,json:async()=>({}),text:async()=>''};
          }});
        const mod=new vm.SourceTextModule(code,{context});
        const names=new Set(['default']);
        for(const match of code.matchAll(/import(?:\s+\w+\s*,)?\s*\{([\s\S]*?)\}\s+from/g))
          for(const name of match[1].split(',')){const key=name.trim().split(/\s+as\s+/)[0];if(key)names.add(key);}
        await mod.link(async spec=>new vm.SyntheticModule([...names],function(){
          const key=spec.split('/').pop().split('?')[0];
          for(const name of names)this.setExport(name,name==='default'?(modules[key]||stub()):noop);
        },{context}));
        await mod.evaluate();
        await mod.namespace.handleChatSubmit({preventDefault:noop});
        assert.equal(preflightChanged,true,'test must cross actual awaited preflight');
        assert(sent,'actual submit POST missing: '+errors.join('\n'));
        for(const [key,value] of Object.entries(scenario.expected)) assert.equal(sent.get(key),value,`${scenario.message}: ${key}`);
      }
      console.log(JSON.stringify({passed:1}));
    """
    # Node 20 ARM64 can crash while collecting multiple experimental VM module
    # contexts. Keep each scenario's real submit handler in a fresh process.
    for scenario_index in range(5):
        result = subprocess.run(
            ["node", "--experimental-vm-modules", "-e", "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1);});", str(module), str(scenario_index)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"submit scenario {scenario_index}: {result.stderr}"
        assert json.loads(result.stdout) == {"passed": 1}
