"""A thinking/tool-only failed turn must not hide its terminal explanation."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_failed_reasoning_only_turn_keeps_safe_terminal_note():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    source = Path(__file__).resolve().parents[1] / "static/js/chatRenderer.js"
    script = r"""
      const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
      const code=fs.readFileSync(process.argv[1],'utf8');
      const names=new Set(['default']);
      for(const match of code.matchAll(/import(?:\s+\w+\s*,)?\s*\{([\s\S]*?)\}\s+from/g))
        for(const name of match[1].split(',')){
          const clean=name.trim().split(/\s+as\s+/)[0];if(clean)names.add(clean);
        }
      const context=vm.createContext({console,window:{},document:{addEventListener:()=>{}}});
      const mod=new vm.SourceTextModule(code,{context});
      await mod.link(async()=>new vm.SyntheticModule([...names],function(){
        for(const name of names)this.setExport(name,name==='getTools'?async()=>({tools:[]}):()=>{});
      },{context}));
      await mod.evaluate();
      const fallback=mod.namespace.terminalFallbackText;
      assert.equal(fallback({failed:true},false,'[Agent stopped: Model request failed (HTTP 400)]'),
        '[Agent stopped: Model request failed (HTTP 400)]');
      assert.equal(fallback({failed:true},true,'duplicate'), '');
      assert.equal(fallback({failed:false},false,'not terminal'), '');
    """
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e",
         "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1)})",
         str(source)], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
