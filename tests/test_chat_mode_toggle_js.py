"""Mode changes must not schedule stale permission changes half a second later."""
import shutil
import subprocess
from pathlib import Path
import pytest

def test_mode_applies_tool_choices_synchronously_without_stale_timer():
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    source=Path(__file__).resolve().parents[1]/'static/app.js'
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8');
const start=code.indexOf('    function setMode(mode) {');
const end=code.indexOf('    window.__odysseusSetChatMode',start);
assert(start>=0&&end>start);
const applied=[],timers=[];
const button=()=>({classList:{toggle(){}},setAttribute(){},closest:()=>null});
const env={currentMode:'chat',loadToggleState:()=>({}),saveToggleState(){},agentBtn:button(),chatBtn:button(),workspaceModule:{applyMode(){}},applyModeToToggles:m=>applied.push(m),setTimeout:f=>timers.push(f)};
vm.createContext(env);vm.runInContext(code.slice(start,end),env);
env.setMode('chat');env.setMode('agent');
assert.deepEqual(applied,['chat','agent'],'visible mode and tool permission must change atomically');
assert.equal(timers.length,0,'old mode must not override a later user tool choice');
'''
    result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
