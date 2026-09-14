"""Drive real session discovery/replay with independently controlled clients."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", ["idle_discovery", "idle_completion", "hidden_focus", "resume_lock", "late_headers", "return_to_same_chat", "late_chunk", "detach_reader", "replay_stall", "replay_canonical", "replay_activity"])
def test_cross_device_subscription_lifecycle(scenario):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1] / "static/js"
    script = r"""
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
      const scenario=process.argv[2],noop=()=>{},deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
      const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
      class Element {
        constructor(){this.children=[];this.dataset={};this.style={setProperty:noop};this.classList={add:noop,remove:noop,toggle:noop,contains:()=>false};this.parentNode=null;this.value='unsent draft';this.fields={};}
        set innerHTML(value){this.html=value;this.children=[];} get innerHTML(){return this.html||'';}
        appendChild(child){child.parentNode=this;this.children.push(child);return child;}
        remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(x=>x!==this);this.parentNode=null;}
        querySelector(key){return this.fields[key]||(this.fields[key]=new Element());}
        querySelectorAll(){return [];} addEventListener(){} removeEventListener(){} setAttribute(){} focus(){}
      }
      const box=new Element(),composer=new Element(),body=new Element(),listeners={},timers=[];
      const document={body,visibilityState:'visible',hidden:false,readyState:'loading',
        getElementById:id=>id==='chat-history'?box:id==='message'?composer:null,
        querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>new Element(),
        createComment:()=>new Element(),addEventListener:(key,fn)=>{(listeners[key]??=[]).push(fn);}};
      let selected='chat-a',viewToken=1,now=1000,requests=[],cancelled=0,appended=[],reloads=[],canonicalRefreshes=0;
      const pendingHeaders=deferred(),pendingRead=deferred();
      const sm={getCurrentSessionId:()=>selected,getSessionViewToken:()=>viewToken,getSessions:()=>[{id:'chat-a',model:'qwen'}],
        selectSession:async id=>reloads.push(id),loadSessions:noop};
      if(scenario==='replay_canonical'||scenario==='replay_activity')sm.refreshSessionHistory=async id=>{assert.equal(id,'chat-a');canonicalRefreshes++;};
      const proxy=extras=>new Proxy(extras||{},{get:(o,k)=>k in o?o[k]:noop});
      const ui=proxy({el:document.getElementById,esc:x=>x});
      const modules={'sessions.js':sm,'storage.js':proxy({get:(key,fallback)=>fallback,getJSON:(key,fallback)=>fallback}),
        'ui.js':ui,'chatRenderer.js':proxy({stripToolBlocks:text=>text,addMessage:(...args)=>appended.push(args)}),
        'markdown.js':proxy({normalizeThinkingMarkup:x=>x,mdToHtml:x=>x,squashOutsideCode:x=>x}),
        'spinner.js':proxy({create:()=>proxy({createElement:()=>new Element()})})};
      const window={location:{origin:'http://odysseus.test',hash:'',pathname:'/'},sessionModule:sm,innerWidth:1280,
        addEventListener:(key,fn)=>{(listeners[key]??=[]).push(fn);}};
      let remoteRunning=false,attached=0,remoteHistory=[];
      window.chatModule={hasActiveStream:()=>false,resumeStream:async id=>{assert.equal(id,'chat-a');attached++;return true;}};
      const reader={read:()=>pendingRead.promise,cancel:async()=>{cancelled++;pendingRead.resolve({done:true});}};
      const response={ok:true,headers:{get:()=> 'remote-run'},body:{getReader:()=>reader,cancel:reader.cancel}};
      const context=vm.createContext({console,document,window,navigator:{platform:'Linux'},history:{replaceState:noop},
        Date:class extends Date{static now(){return now;}},
        localStorage:{getItem:()=>null},sessionStorage:{getItem:()=>null},URL,TextDecoder,AbortController,
        MutationObserver:class{observe(){}},setTimeout:()=>0,clearTimeout:noop,
        setInterval:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearInterval:noop,
        fetch:async(url,options={})=>{requests.push({url:String(url),options});
          if(scenario.startsWith('idle_')||scenario==='hidden_focus'){
            if(String(url).includes('/api/history/')){
              const limit=new URL(url).searchParams.get('limit');
              return {ok:true,json:async()=>({history:limit==='1'?remoteHistory.slice(-1):remoteHistory,model:'qwen',total:remoteHistory.length})};
            }
            return {ok:remoteRunning,status:remoteRunning?200:404,json:async()=>({status:'streaming'})};
          }
          return pendingHeaders.promise;}});
      const file=scenario.startsWith('idle_')||scenario==='hidden_focus'?'sessions.js':'chat.js';
      const code=fs.readFileSync(process.argv[1]+'/'+file,'utf8'),mod=new vm.SourceTextModule(code,{context});
      const names=new Set(['default']);
      for(const match of code.matchAll(/import(?:\s+\w+\s*,)?\s*\{([\s\S]*?)\}\s+from/g))for(const name of match[1].split(',')){const key=name.trim().split(/\s+as\s+/)[0];if(key)names.add(key);}
      await mod.link(async spec=>new vm.SyntheticModule([...names],function(){const key=spec.split('/').pop().split('?')[0];for(const name of names)this.setExport(name,name==='default'?(modules[key]||proxy()):name==='stripToolBlocks'?(text=>text):noop);},{context}));
      await mod.evaluate();
      if(scenario.startsWith('idle_')||scenario==='hidden_focus'){
        mod.namespace.initDependencies();
        mod.namespace.setCurrentSessionId('chat-a');
        assert(timers.length,'already-open idle chat needs periodic discovery');
        await Promise.all(timers.map(x=>x.fn()));await flush();
        const previous=requests.length;remoteRunning=scenario!=='idle_completion';
        remoteHistory=[{role:'user',content:'Remote question'},{role:'assistant',content:'Remote answer'}];
        if(scenario==='hidden_focus'){
          document.visibilityState='hidden';
          await Promise.all(timers.map(x=>x.fn()));await flush();
          assert.equal(requests.length,previous,'hidden tab must not poll');
          document.visibilityState='visible';
          await Promise.all(listeners.focus.map(fn=>fn()));await flush();
        }else{
        await Promise.all(timers.map(x=>x.fn()));await flush();
        }
        assert(requests.length>previous,'must keep checking after no active run');
        assert.equal(attached,scenario==='idle_completion'?0:1,'second client must attach only to active run');
        assert.equal(appended.filter(x=>x[0]==='assistant').length,1,'canonical remote answer must render once');
        assert.equal(appended.filter(x=>x[0]==='user').length,1,'remote user turn must appear before replay');
        const historyFetches=requests.filter(x=>/\/api\/history\//.test(x.url)&&!x.url.endsWith('limit=1')).length;
        remoteRunning=false;
        await Promise.all(timers.map(x=>x.fn()));await flush();
        assert.equal(requests.filter(x=>/\/api\/history\//.test(x.url)&&!x.url.endsWith('limit=1')).length,historyFetches,'unchanged canonical history must not reload');
        assert.equal(composer.value,'unsent draft','sync must preserve unsent input');
      }else{
        const first=mod.namespace.resumeStream('chat-a');await flush();
        if(scenario==='resume_lock'){
          const second=mod.namespace.resumeStream('chat-a');await flush();
          assert.equal(requests.length,1,'duplicate attach requests must be locked before headers');
          selected='chat-b';pendingHeaders.resolve(response);await Promise.all([first,second]);
        }else if(scenario==='late_headers'||scenario==='return_to_same_chat'){
          selected=scenario==='late_headers'?'chat-b':'chat-a';viewToken++;
          pendingHeaders.resolve(response);await first;
          assert.equal(box.children.length,0,'old headers must not append into new chat');
          assert(cancelled>0,'discarded response subscription must close');
        }else if(scenario==='replay_stall'){
          pendingHeaders.resolve(response);await flush();
          now+=46000;
          await Promise.all(timers.map(x=>x.fn()));await flush();await first;
          assert(requests[0].options.signal.aborted,'silent replay must reconnect instead of hanging forever');
          assert(cancelled>0,'watchdog must release reader');
          assert.equal(box.children.length,0,'dead replay placeholder must not duplicate the retry');
          assert.equal(requests.filter(x=>/\/stop\//.test(x.url)).length,0,'stall recovery must not stop remote run');
        }else if(scenario==='replay_activity'){
          const events=[
            'id: 1\ndata: {"delta":"First round"}',
            'id: 2\ndata: {"type":"tool_start","tool":"bash","command":"echo example"}',
            'id: 2\ndata: {"type":"tool_start","tool":"bash","command":"echo example"}',
            'id: 3\ndata: {"type":"tool_output","tool":"bash","output":"example","exit_code":0}',
            'id: 4\ndata: {"type":"agent_step"}',
            'id: 5\ndata: {"delta":"Second round"}',
            'id: 6\ndata: {"type":"tool_start","tool":"web_search"}',
            'id: 7\ndata: {"type":"tool_progress","message":"Waiting for search"}'
          ].join('\n\n')+'\n\n';
          let firstChunk=true;
          reader.read=()=>firstChunk?(firstChunk=false,Promise.resolve({done:false,value:new TextEncoder().encode(events)})):pendingRead.promise;
          pendingHeaders.resolve(response);await flush();
          assert.equal(box.children.length,2,'each replayed agent round needs its own message bubble');
          const liveBody=box.children[0].querySelector('.body');
          const nextBody=box.children[1].querySelector('.body');
          const cards=[...liveBody.children,...nextBody.children].filter(x=>x.className==='agent-tool-output remote-tool-activity');
          assert.equal(cards.length,2,'live tools render before completion; duplicate SSE IDs ignored');
          assert.equal(cards[0].dataset.status,'done');assert.equal(cards[0].children[1].textContent,'example');
          assert.equal(cards[1].dataset.status,'running');assert.equal(cards[1].children[1].textContent,'Waiting for search');
          assert.equal(box.children[0].querySelector('.stream-content').innerHTML,'First round');
          assert.equal(box.children[1].querySelector('.stream-content').innerHTML,'Second round');
          assert.equal(canonicalRefreshes,0,'still-live rendering must not wait for canonical completion');
          assert.equal(composer.value,'unsent draft');
          pendingRead.resolve({done:true});await first;
          assert.equal(canonicalRefreshes,1);
          assert.equal(box.children.length,0,'canonical refresh removes every temporary replay bubble');
        }else if(scenario==='replay_canonical'){
          pendingHeaders.resolve(response);await flush();
          pendingRead.resolve({done:false,value:new TextEncoder().encode('data: {"delta":"Remote answer"}\n\ndata: [DONE]\n\n')});
          await first;
          assert.equal(canonicalRefreshes,1,'completed replay reconciles canonical history once');
          assert.equal(appended.length,0,'no extra local assistant beside canonical record');
          assert.equal(reloads.length,0,'replay must not navigate/reset composer');
        }else{
          pendingHeaders.resolve(response);await flush();
          assert.equal(box.children.length,1,'one replay holder before switching');
          selected='chat-b';
          if(scenario==='detach_reader'){
            mod.namespace.detachCurrentStream('chat-a');await flush();
            assert(cancelled>0||requests[0].options.signal?.aborted,'navigation closes local replay reader immediately');
            pendingRead.resolve({done:true});
          }else{
            pendingRead.resolve({done:false,value:new TextEncoder().encode('data: {"delta":"STALE"}\n\ndata: [DONE]\n\n')});
          }
          await first;
          assert.equal(appended.length,0,'stale replay must not finalize into new chat');
          assert.equal(box.children.length,0,'stale replay holder must be removed');
          assert.equal(requests.filter(x=>/\/stop\//.test(x.url)).length,0,'navigation must not stop remote run');
        }
      }
      console.log(JSON.stringify({passed:scenario}));
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e", "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1);});", str(root), scenario],
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": scenario}
