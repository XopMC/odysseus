"""Engineering foundation UI: explicit project policy and stale-response safety."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_engineering_project_policy_and_async_isolation():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    module = (Path(__file__).resolve().parents[1] / "static/js/engineering-workspace.js").as_uri()
    script = r"""
      import assert from 'node:assert/strict';
      class Element {
        constructor(tag,doc){this.tagName=tag.toUpperCase();this.ownerDocument=doc;this.children=[];this.dataset={};this.attributes={};this.listeners={};this.value='';this.checked=false;this.disabled=false;this.hidden=false;this._text='';}
        append(...items){for(const item of items){this.children.push(item);if(typeof item==='object')item.parentNode=this;}}
        replaceChildren(...items){this.children=[];this._text='';this.append(...items);}
        remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(item=>item!==this);}
        setAttribute(key,value){this.attributes[key]=String(value);}
        addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
        dispatch(type){for(const fn of this.listeners[type]||[])fn({preventDefault(){},target:this});}
        set textContent(value){this._text=String(value);this.children=[];}
        get textContent(){return this._text+this.children.map(item=>typeof item==='string'?item:item.textContent).join('');}
      }
      const doc={createElement(tag){return new Element(tag,doc);}};
      const find=(root,key)=>root.dataset?.engineering===key?root:root.children?.map(child=>find(child,key)).find(Boolean);
      const tick=async()=>{for(let n=0;n<12;n++)await Promise.resolve();};
      const {mountEngineeringWorkspace}=await import(process.argv[1]);
      const root=new Element('div',doc),sibling=new Element('p',doc);root.append(sibling);
      const p1={id:'p1',name:'One',root:'/work/one',host_id:'host1',access_mode:null,revision:1};
      const p2={id:'p2',name:'<img src=x onerror=alert(1)>',root:'/work/two',host_id:'host1',access_mode:null,revision:4};
      const requests=[],selections=[];let pendingTools,pendingPolicy,deferPolicy=false,failCreate=true,stalePolicy=true;
      const request=async(path,options={})=>{
        requests.push({path,...options});
        if(path.endsWith('/capabilities'))return {enabled:true,stage:'foundation',features:{projects:true,policy:true,tool_catalog:true}};
        if(path.endsWith('/hosts'))return {hosts:[{id:'host1',name:'Jetson',platform:'linux',status:'configured'}]};
        if(path.endsWith('/projects')&&options.method==='POST'){
          if(failCreate)throw new Error('Project unavailable');
          return {id:'p3',...options.body,access_mode:null,revision:1};
        }
        if(path.endsWith('/projects'))return {projects:[p1,p2]};
        if(path.includes('/policy')){
          if(stalePolicy){const error=new Error('Changed on another device');error.status=409;throw error;}
          if(deferPolicy)return new Promise(resolve=>pendingPolicy=resolve);
          return {...p1,access_mode:'trusted_host',revision:3};
        }
        if(path.includes('project_id=p1'))return new Promise(resolve=>pendingTools=resolve);
        if(path.includes('/tools?'))return {tools:[{id:'read',name:'Read file',available:true,effect:'read',reason:'Project read access'}]};
        throw Error('Unexpected request '+path);
      };
      const destroy=mountEngineeringWorkspace(root,{request,onProjectSelected:project=>selections.push(project)});assert.equal(typeof destroy,'function');await tick();
      assert.deepEqual(selections,[null],'initial load never implicitly binds an existing Engineering project');
      assert.equal(find(root,'project').value,'');
      find(root,'project').value='p1';find(root,'project').dispatch('change');await tick();
      assert.deepEqual(selections.at(-1),p1,'explicit loaded selection is reported without waiting on tool catalog');
      assert.notEqual(selections.at(-1),p1,'caller cannot mutate the internal project record');
      assert.equal(find(root,'host').value,'','host choice must be explicit');
      assert.equal(find(root,'consent').checked,false);
      assert.equal(find(root,'apply-policy').disabled,true);
      assert.match(find(root,'project-details').textContent,/read.only/i);
      assert.equal(find(root,'isolated-option').disabled,true);
      find(root,'name').value='Keep my draft';find(root,'root').value='relative/path';find(root,'host').value='host1';
      find(root,'create-form').dispatch('submit');await tick();
      assert.equal(requests.filter(item=>item.method==='POST').length,0,'absolute project folder is required');
      find(root,'root').value='/work/new';
      find(root,'create-form').dispatch('submit');await tick();
      assert.equal(find(root,'name').value,'Keep my draft');assert.match(find(root,'notice').textContent,/Project unavailable/);
      assert.deepEqual(requests.find(item=>item.method==='POST').body,{name:'Keep my draft',root:'/work/new',host_id:'host1'});
      find(root,'policy-mode').value='trusted_host';find(root,'policy-mode').dispatch('change');
      find(root,'policy-form').dispatch('submit');await tick();
      assert.equal(requests.filter(item=>item.path.includes('/policy')).length,0,'no mutation without explicit consent');
      find(root,'consent').checked=true;find(root,'consent').dispatch('change');
      find(root,'policy-form').dispatch('submit');await tick();
      assert.deepEqual(requests.find(item=>item.path.includes('/policy')).body,{expected_revision:1,access_mode:'trusted_host',confirmation:true});
      assert.equal(find(root,'consent').checked,false);assert.equal(find(root,'apply-policy').disabled,true);
      assert.match(find(root,'notice').textContent,/changed|conflict|stale/i);
      find(root,'policy-form').dispatch('submit');await tick();
      assert.equal(requests.filter(item=>item.path.includes('/policy')).length,1,'conflicts cannot silently retry with a new revision');
      find(root,'project').value='p2';find(root,'project').dispatch('change');await tick();
      assert.deepEqual(selections.at(-1),p2);
      const selectedCount=selections.length;
      assert.match(find(root,'tools').textContent,/Read file/);
      pendingTools({tools:[{id:'old',name:'STALE TOOL',available:true}]});await tick();
      assert.equal(selections.length,selectedCount,'stale tool response cannot publish a project selection');
      assert.equal(find(root,'tools').textContent.includes('STALE TOOL'),false,'project A response must not replace project B tools');
      assert.equal(find(root,'project-details').textContent.includes(p2.name),true,'untrusted names render literally');
      assert.equal(find(root,'project-details').children.some(child=>child.tagName==='IMG'),false);
      failCreate=false;find(root,'create-form').dispatch('submit');await tick();
      assert.equal(find(root,'project').value,'p3');assert.equal(find(root,'consent').checked,false);
      assert.equal(selections.at(-1).id,'p3');assert.equal(selections.at(-1).access_mode,null);
      assert.equal(requests.filter(item=>item.path.includes('/policy')).length,1,'create never grants execution');
      find(root,'policy-mode').value='isolated';find(root,'consent').checked=true;find(root,'policy-form').dispatch('submit');await tick();
      assert.equal(requests.filter(item=>item.path.includes('/policy')).length,1,'unavailable isolation cannot be enabled by a programmatic select');
      find(root,'refresh').dispatch('click');await tick();stalePolicy=false;
      assert.equal(selections.at(-1),null,'removed project selection does not silently pick a different project');
      find(root,'project').value='p1';find(root,'project').dispatch('change');await tick();
      find(root,'policy-mode').value='trusted_host';find(root,'consent').checked=true;find(root,'policy-form').dispatch('submit');await tick();
      assert.equal(selections.at(-1).id,'p1');assert.equal(selections.at(-1).access_mode,'trusted_host');assert.equal(selections.at(-1).revision,3);
      p1.access_mode=null;p1.revision=4;find(root,'refresh').dispatch('click');await tick();
      assert.equal(selections.at(-1).access_mode,null,'refresh reports revoked access, without authorizing a legacy task');assert.equal(selections.at(-1).revision,4);
      deferPolicy=true;find(root,'policy-mode').value='trusted_host';find(root,'consent').checked=true;find(root,'policy-form').dispatch('submit');await tick();
      find(root,'project').value='p2';find(root,'project').dispatch('change');await tick();
      const beforeLatePolicy=selections.length;
      pendingPolicy({...p1,access_mode:'trusted_host',revision:5});await tick();
      assert.equal(selections.length,beforeLatePolicy,'late policy response for A cannot replace selected B');assert.equal(selections.at(-1).id,'p2');
      find(root,'project').value='';find(root,'project').dispatch('change');await tick();
      assert.equal(selections.at(-1),null,'explicit Legacy selection clears the new-run Engineering binding');
      const disabledRoot=new Element('div',doc),disabledRequests=[],disabledSelections=[];
      const stopDisabled=mountEngineeringWorkspace(disabledRoot,{onProjectSelected:project=>disabledSelections.push(project),request:async path=>{disabledRequests.push(path);return {enabled:false};}});await tick();
      assert.deepEqual(disabledSelections,[null]);
      assert.equal(disabledRequests.length,1);assert.match(disabledRoot.textContent,/unavailable|disabled/i);stopDisabled();
      const lateRoot=new Element('div',doc);let finishCapabilities;
      const lateRequests=[],lateSelections=[];const stopLate=mountEngineeringWorkspace(lateRoot,{onProjectSelected:project=>lateSelections.push(project),request:path=>{lateRequests.push(path);return new Promise(resolve=>finishCapabilities=resolve);}});
      stopLate();finishCapabilities({enabled:true,features:{projects:true}});await tick();
      assert.equal(lateRoot.children.length,0);assert.equal(lateRequests.length,1,'destroy cannot start new discovery');
      assert.deepEqual(lateSelections,[],'destroyed mount cannot notify through a delayed request');
      destroy();assert.deepEqual(root.children,[sibling],'destroy preserves parent-owned nodes');
      console.log(JSON.stringify({passed:true}));
    """
    result = subprocess.run(["node", "--input-type=module", "-e", script, module], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}


def test_engineering_foundation_real_browser(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    if subprocess.run(["node", "-e", "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip("Playwright is unavailable")
    root = Path(__file__).resolve().parents[1]
    script = r"""
      const {chromium}=require('playwright'),fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
      const root=process.argv[1],out=process.argv[2],requests=[],errors=[],api='/api/team/engineering';
      const projects=[{id:'p1',name:'Existing project',root:'/work/existing',host_id:'legacy-jetson',access_mode:null,revision:1}];
      let failCreate=true;
      const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Odysseus Engineering QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/css/team-workspace.css"><link rel="stylesheet" href="/static/css/engineering-workspace.css"><style>html,body{height:auto;min-height:100%;overflow:visible}body{display:block}main{max-width:1000px;margin:auto;padding:12px}</style></head><body><main id="engineering"></main><script type="module">import {mountEngineeringWorkspace} from '/static/js/engineering-workspace.js';window.selectedProjects=[];window.stopEngineering=mountEngineeringWorkspace(document.getElementById('engineering'),{onProjectSelected:project=>window.selectedProjects.push(project),request:async(path,options={})=>{const response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},...(options.body?{body:JSON.stringify(options.body)}:{})});const data=await response.json();if(!response.ok){const error=new Error(data.detail);error.status=response.status;throw error;}return data;}});</script></body></html>`;
      const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://localhost'),pathname=url.pathname;
        if(pathname.startsWith('/static/')){res.setHeader('Content-Type',pathname.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(root+pathname));return;}
        if(pathname==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;
        requests.push({path:pathname,method:req.method,body});res.setHeader('Content-Type','application/json');
        const send=(data,status=200)=>{res.statusCode=status;res.end(JSON.stringify(data));};
        if(pathname==='/qa/allow-create'){failCreate=false;send({ok:true});return;}
        if(pathname==='/qa/stale'){projects.find(p=>p.id==='p2').revision=2;send({ok:true});return;}
        if(pathname===api+'/capabilities'){send({enabled:true,stage:'foundation',features:{projects:true,policy:true,tool_catalog:true}});return;}
        if(pathname===api+'/hosts'){send({hosts:[{id:'legacy-jetson',name:'Jetson',platform:'linux',status:'configured'}]});return;}
        if(pathname===api+'/projects'&&req.method==='GET'){send({projects});return;}
        if(pathname===api+'/projects'&&req.method==='POST'){
          if(failCreate){send({detail:'Project folder is not accessible'},400);return;}
          const project={id:'p2',...body,access_mode:null,revision:1};projects.push(project);send(project);return;
        }
        if(pathname===api+'/projects/p2/policy'){
          const project=projects.find(p=>p.id==='p2');
          if(body.expected_revision!==project.revision){send({detail:'Revision conflict'},409);return;}
          if(body.access_mode!=='trusted_host'||body.confirmation!==true){send({detail:'Explicit trusted-host confirmation required'},400);return;}
          project.access_mode=body.access_mode;project.revision++;send(project);return;
        }
        if(pathname===api+'/tools'){
          const project=projects.find(p=>p.id===url.searchParams.get('project_id'));
          send({tools:[{id:'read',name:'Read file',available:true,effect:'read',reason:'Read-only project access'},
            {id:'command',name:'Execute command',available:project?.access_mode==='trusted_host',effect:'host execution',reason:project?.access_mode==='trusted_host'?'Trusted host approved; host permissions still apply':'Trusted-host approval required'},
            {id:'isolated',name:'Isolated shell',available:false,effect:'execution',reason:'Verified isolated runtime is not available'}]});return;
        }
        send({detail:'Unknown fixture route'},404);
      });
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
        page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>{errors.push('Unexpected script dialog');dialog.dismiss();});
        const base='http://127.0.0.1:'+server.address().port;await page.goto(base);
        const by=key=>page.locator('[data-engineering="'+key+'"]');
        await page.getByText('Review a project or register a new read-only project.',{exact:true}).waitFor();
        assert.deepEqual(await page.evaluate(()=>selectedProjects),[null]);assert.equal(await by('project').inputValue(),'');
        assert.match(await by('binding-notice').textContent(),/NEW Team runs only/);
        await by('project').selectOption('p1');
        await page.getByText('Trusted-host approval required',{exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>selectedProjects.at(-1).id),'p1');
        assert.equal(await page.title(),'Odysseus Engineering QA');assert.equal(new URL(page.url()).origin,base);
        assert.equal(await by('host').inputValue(),'');assert.equal(await by('consent').isChecked(),false);assert.equal(await by('apply-policy').isDisabled(),true);
        assert.equal(await by('isolated-option').isDisabled(),true);assert.match(await by('workspace').textContent(),/full engineering workflow.*not available/i);
        await page.getByLabel('Project name',{exact:true}).fill('<img src=x onerror=alert(1)>');
        await page.getByLabel('Absolute project folder').fill('/work/browser-qa');await page.getByLabel('Host',{exact:true}).selectOption('legacy-jetson');
        await by('create').click();await page.getByText('Project folder is not accessible',{exact:true}).waitFor();
        assert.equal(await by('name').inputValue(),'<img src=x onerror=alert(1)>');assert.equal(await by('root').inputValue(),'/work/browser-qa');
        await page.evaluate(()=>fetch('/qa/allow-create'));await by('create').click();
        await page.getByText('Project registered. No trusted-host access has been granted.',{exact:true}).waitFor();
        assert.equal(await by('project').inputValue(),'p2');assert.equal(await page.locator('img').count(),0);assert.equal(await by('consent').isChecked(),false);
        assert.equal(await page.evaluate(()=>selectedProjects.at(-1).id),'p2');assert.equal(await page.evaluate(()=>selectedProjects.at(-1).access_mode),null);
        await page.getByLabel('Access mode',{exact:true}).selectOption('trusted_host');assert.equal(await by('apply-policy').isDisabled(),true);
        await by('consent').check();await page.evaluate(()=>fetch('/qa/stale'));await by('apply-policy').click();
        await page.getByText(/409 conflict/).waitFor();assert.equal(await by('apply-policy').isDisabled(),true);assert.equal(await by('consent').isChecked(),false);
        assert.equal(requests.filter(item=>item.path.endsWith('/policy')).length,1);
        await by('refresh').click();await page.getByText('Revision: 2',{exact:true}).waitFor();
        assert.equal(await by('name').inputValue(),'<img src=x onerror=alert(1)>','refresh preserves project draft');
        assert.equal(await by('consent').isChecked(),false);assert.equal(await by('policy-mode').inputValue(),'');
        await page.getByLabel('Access mode',{exact:true}).selectOption('trusted_host');await by('consent').check();await by('apply-policy').click();
        await page.getByText('Trusted host approved; host permissions still apply',{exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>selectedProjects.at(-1).revision),3);assert.equal(await page.evaluate(()=>selectedProjects.at(-1).access_mode),'trusted_host');
        assert.deepEqual(requests.filter(item=>item.path.endsWith('/policy')).map(item=>item.body),[
          {expected_revision:1,access_mode:'trusted_host',confirmation:true},{expected_revision:2,access_mode:'trusted_host',confirmation:true}]);
        assert.equal(await by('consent').isChecked(),false);assert.match(await by('project-details').textContent(),/not isolated/);
        await page.screenshot({path:out+'/engineering-desktop.png',fullPage:true,animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/engineering-mobile.png',fullPage:true,animations:'disabled'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'mobile controls must not overflow');
        await by('project').selectOption('p1');await page.getByText('Trusted-host approval required',{exact:true}).waitFor();
        assert.match(await by('project-details').textContent(),/read.only/i);assert.equal(await by('consent').isChecked(),false);
        await by('project').selectOption('');assert.equal(await page.evaluate(()=>selectedProjects.at(-1)),null);
        await page.evaluate(()=>stopEngineering());assert.equal(await by('workspace').count(),0);assert.deepEqual(errors,[]);
        console.log(JSON.stringify({passed:true}));
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(["node", "-e", "(async()=>{" + script + "})().catch(error=>{console.error(error);process.exit(1);});", str(root), str(tmp_path)], capture_output=True, text=True, timeout=60, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
