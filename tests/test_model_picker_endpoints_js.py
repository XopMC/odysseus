"""Exercise actual picker rows and PATCHes for equal names on distinct routes."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_model_picker_preserves_endpoint_identity():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    root = Path(__file__).resolve().parents[1]
    script = r"""
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
      const noop=()=>{},store=new Map(),requests=[],docListeners={};
      class Element {
        constructor(){this.children=[];this.listeners={};this.dataset={};this.style={};this._classes=new Set();this.value='';this._text='';
          this.classList={add:(...xs)=>xs.forEach(x=>this._classes.add(x)),remove:(...xs)=>xs.forEach(x=>this._classes.delete(x)),contains:x=>this._classes.has(x),toggle:(x,on)=>{on=on??!this._classes.has(x);on?this._classes.add(x):this._classes.delete(x);return on;}};}
        set className(x){this._classes=new Set(x.split(/\s+/));}get className(){return [...this._classes].join(' ');}
        set innerHTML(x){this.children=[];this._text=x;}get innerHTML(){return this._text;}
        set textContent(x){this.children=[];this._text=String(x);}get textContent(){return this._text+this.children.map(x=>x.textContent||'').join('');}
        appendChild(x){if(x.parentNode)x.parentNode.children=x.parentNode.children.filter(y=>y!==x);this.children.push(x);x.parentNode=this;return x;}
        append(...xs){for(const x of xs)this.appendChild(typeof x==='string'?{textContent:x}:x);}
        get lastElementChild(){return this.children.at(-1);}setAttribute(k,v){this[k]=String(v);}getAttribute(k){return this[k]??null;}
        addEventListener(k,fn){(this.listeners[k]??=[]).push(fn);}removeEventListener(){}focus(){}blur(){}scrollIntoView(){}
        async emit(k){for(const fn of this.listeners[k]||[])await fn({stopPropagation:noop,preventDefault:noop,target:this});}
        querySelector(s){return this.querySelectorAll(s)[0]||null;}
        querySelectorAll(s){const cls=s.split('.').filter(Boolean);return this.children.flatMap(x=>[(cls.every(c=>x.classList?.contains(c))?x:null),...(x.querySelectorAll?.(s)||[])]).filter(Boolean);}
        contains(x){return this===x||this.children.some(c=>c.contains?.(x));}
      }
      const ids=Object.fromEntries(['model-picker-wrap','model-picker-btn','model-picker-menu','model-picker-search','model-picker-list','model-picker-label','message'].map(x=>[x,new Element()]));
      ids['model-picker-menu'].classList.add('hidden');
      const shared='qwen/shared-model',items=[
        {endpoint_id:'jetson',endpoint_name:'Local Qwen',url:'http://jetson:11434/v1',category:'local',models:[shared],models_extra:[shared]},
        {endpoint_id:'mac',endpoint_name:'Local Qwen',url:'http://mac:1234/v1',category:'local',models:[shared]},
        {endpoint_id:'cloud-a',endpoint_name:'Cloud',url:'https://a.example/v1',category:'api',models:[shared]},
        {endpoint_id:'cloud-b',endpoint_name:'Cloud',url:'https://b.example/v1',category:'api',models:[shared]},
      ];
      let pending=null,currentId='chat';const session={id:'chat',model:shared,endpoint_id:'mac',endpoint_url:items[1].url};
      const document={getElementById:id=>ids[id]||null,createElement:()=>new Element(),createTextNode:text=>({textContent:text}),addEventListener:(key,fn)=>{(docListeners[key]??=[]).push(fn);},dispatchEvent:noop};
      const window={location:{origin:'http://odysseus.test'},innerWidth:1280,modelsModule:{getCachedItems:()=>items}};
      const context=vm.createContext({console,window,document,URL,FormData,CustomEvent:class{},localStorage:{getItem:key=>store.get(key)||null,setItem:(key,value)=>store.set(key,value)},
        setTimeout:()=>0,clearTimeout:noop,fetch:async(url,options)=>{requests.push({url,model:options.body.get('model'),endpoint:options.body.get('endpoint_id'),address:options.body.get('endpoint_url')});return{ok:true};}});
      const stubs={'ui.js':{default:{showToast:noop,showError:message=>{throw Error(message);}}},'settings.js':{default:{}},'providers.js':{providerLogo:()=>''},'spinner.js':{default:{}}};
      const cache=new Map();async function load(filename){
        if(cache.has(filename))return cache.get(filename);
        let mod;const stub=stubs[path.basename(filename)];
        if(stub)mod=new vm.SyntheticModule(Object.keys(stub),function(){for(const[k,v]of Object.entries(stub))this.setExport(k,v);},{context});
        else mod=new vm.SourceTextModule(fs.readFileSync(filename,'utf8'),{context,identifier:filename});
        cache.set(filename,mod);if(!stub)await mod.link(spec=>load(path.resolve(path.dirname(filename),spec.split('?')[0])));return mod;
      }
      const mod=await load(process.argv[1]+'/static/js/modelPicker.js');await mod.evaluate();
      mod.namespace.initModelPicker({getCurrentSessionId:()=>currentId,getSessions:()=>[session],getPendingChat:()=>pending,setPendingChat:x=>{pending=x;},createDirectChat:()=>{throw Error('unexpected new chat');}});
      mod.namespace.updateModelPicker();
      const open=async()=>{ids['model-picker-menu'].classList.add('hidden');await ids['model-picker-btn'].emit('click');};
      const rows=()=>ids['model-picker-list'].querySelectorAll('.model-switch-item');
      await open();assert.equal(rows().length,4,'same model must appear once for each local AND API endpoint');
      const sourceLabels=rows().map(row=>row.querySelector('.model-switch-ep').textContent);
      assert.equal(new Set(sourceLabels).size,4,'identical endpoint names need source disambiguation');
      assert(ids['model-picker-label'].textContent.includes('mac'),'restored current model label must identify its endpoint');
      for(let index=0;index<4;index++){
        ids['model-picker-search'].value=['jetson','mac','a.example','b.example'][index];await ids['model-picker-search'].emit('input');
        assert.equal(rows().length,1,'search must retain a specific route');await rows()[0].emit('click');
        await window.__odysseusModelSwitchPromise;
        assert.equal(requests.at(-1).endpoint,items[index].endpoint_id);assert.equal(requests.at(-1).address,items[index].url);assert.equal(requests.at(-1).model,shared);
        assert.equal(session.endpoint_id,items[index].endpoint_id);
        mod.namespace.updateModelPicker();assert(ids['model-picker-label'].textContent.includes(sourceLabels[index]),'restored label must match selected source');
        await open();
      }
      assert.equal(JSON.parse(store.get('odysseus-model-recent')).length,4,'recent selections must retain each route');
      // Mark only Mac as favorite; a sibling serving the same model stays separate.
      ids['model-picker-search'].value='mac';await ids['model-picker-search'].emit('input');
      await rows()[0].querySelector('.mp-fav-dot').emit('click');
      assert.deepEqual(JSON.parse(store.get('odysseus-model-favorites')),['mac::'+shared]);
      await open();assert.equal(rows().length,4);assert.equal(rows().filter(row=>row.querySelector('.mp-fav-dot').classList.contains('active')).length,1);
      // Old bare-ID favorites preserve all endpoints, never pick an arbitrary first.
      store.set('odysseus-model-favorites',JSON.stringify([shared]));await open();assert.equal(rows().length,4);
      assert.equal(rows().filter(row=>row.querySelector('.mp-fav-dot').classList.contains('active')).length,4);
      currentId=null;pending={url:items[1].url,modelId:shared,endpointId:'mac',source:'manual'};
      mod.namespace.updateModelPicker();assert(ids['model-picker-label'].textContent.includes('mac'));
      items.reverse();mod.namespace.updateModelPicker();assert.equal(pending.endpointId,'mac','catalog order must not change route');
      assert(ids['model-picker-label'].textContent.includes('mac'));
      pending={};await docListeners['odysseus:auto-select-model'][0]({detail:{modelId:shared,url:'http://mac:1234/v1'}});
      assert.equal(pending.endpointId,'mac','URL-only discovery must not select another equal-named model');
      const identity=(await load(process.argv[1]+'/static/js/model/routeIdentity.js')).namespace;
      const catalog=items.map(item=>({mid:shared,url:item.url,endpointId:item.endpoint_id,epName:item.endpoint_name}));
      assert.equal(identity.resolveSavedModelRoutes([shared],catalog).length,0,'ambiguous legacy recent entry must not invent a route');
      assert.equal(identity.resolveSavedModelRoutes(['mac::'+shared],catalog)[0].endpointId,'mac');
      const mac=catalog.find(item=>item.endpointId==='mac');
      const migrated=identity.toggleRouteFavorite([shared],mac,catalog);
      assert.equal(migrated.length,3);assert(!migrated.includes('mac::'+shared),'unfavoriting one legacy route preserves siblings');
      const equalHosts=[{endpointId:'account-a',epName:'Same account label',url:'https://shared.example/v1'},{endpointId:'account-b',epName:'Same account label',url:'https://shared.example/v1'}];
      assert.notEqual(identity.modelEndpointLabel(equalHosts[0],equalHosts),identity.modelEndpointLabel(equalHosts[1],equalHosts),'IDs distinguish equal names at an equal address');
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(["node", "--experimental-vm-modules", "-e", "(async()=>{" + script + "})().catch(error=>{console.error(error);process.exit(1);});", str(root)], capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
