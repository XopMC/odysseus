"""An executing durable Plan needs an explicit, run-safe Continue affordance."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "static/js/chat-work.js"


def test_executing_plan_keeps_continue_button_visible():
    source = SRC.read_text(encoding="utf-8")
    assert "['draft', 'approved', 'executing'].includes(plan.status)" in source
    assert "plan.status === 'executing' ? t('Continue')" in source


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js unavailable")
def test_plan_continue_checks_remote_run_before_submitting():
    source = SRC.read_text(encoding="utf-8")
    start = source.index("async function mutate(kind, action) {")
    end = source.index("async function continueGoal()", start)
    function = source[start:end].strip()
    script = f"""
const fn = {json.dumps(function)};
async function run(statusCode) {{
  let submissions = 0, posts = 0, notices = [];
  const input = {{value:'', dispatchEvent:() => {{}}}};
  const form = {{requestSubmit:() => submissions++}};
  const snapshot = {{plan:{{status:'executing',revision:3}}}};
  const window = {{chatModule:{{hasActiveStream:() => false}}}};
  const fetch = async () => ({{ok:statusCode === 200,status:statusCode}});
  const el = id => id === 'message' ? input : id === 'chat-form' ? form : null;
  const t = value => value;
  const toast = value => notices.push(value);
  const post = async () => {{ posts++; return {{}}; }};
  const mutate = new Function('snapshot','sessionId','api','window','fetch','el','t','toast','post','Event',
    'return (' + fn + ')')(
      snapshot,'safe-session','',window,fetch,el,t,toast,post,
      class Event {{ constructor(type) {{ this.type=type; }} }});
  await mutate('plan','execute');
  return {{submissions,posts,notices,input:input.value}};
}}
Promise.all([run(404),run(200)]).then(result => console.log(JSON.stringify(result)));
"""
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert result[0]["submissions"] == 1
    assert result[0]["posts"] == 0
    assert "latest durable steps" in result[0]["input"]
    assert result[1]["submissions"] == 0
    assert result[1]["posts"] == 0
    assert "already active" in result[1]["notices"][0]
