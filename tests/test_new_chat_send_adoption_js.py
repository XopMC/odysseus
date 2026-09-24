"""The New Chat composer must never send into the previously viewed session."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "static/js/chat.js"


def test_fresh_composer_is_fenced_before_goal_guidance_shortcut():
    source = SRC.read_text(encoding="utf-8")
    submit = source[source.index("export async function handleChatSubmit(e) {"):]
    assert submit.index("const freshComposerAtSubmit") < submit.index("const sessionId =")
    assert submit.index("&& !freshComposerAtSubmit") < submit.index("await window.chatWork.addGuidance")
    assert submit.index("const freshComposerAtSubmit") < submit.index("_adoptOpenedSessionBeforeAutoCreate()")


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js unavailable")
def test_fresh_composer_fences_stale_session_adoption():
    source = SRC.read_text(encoding="utf-8")
    start = source.index("  async function _adoptOpenedSessionBeforeAutoCreate() {")
    end = source.index("  // ── Auto-recovery", start)
    function = source[start:end].strip()
    script = f"""
const adopt = new Function('sessionModule', '_hashSessionCandidate', 'document', 'window',
  'return (' + {json.dumps(function)} + ')')();
async function check(title, hash, initial) {{
  const calls = [];
  let id = initial;
  const sessionModule = {{
    getCurrentSessionId: () => id,
    setCurrentSessionId: value => {{ calls.push(['reset', value]); id = value; }},
    hasPendingChat: () => false,
    selectSession: async value => {{ calls.push(['select', value]); id = value; }},
  }};
  const document = {{
    getElementById: key => key === 'current-meta' ? {{textContent:title}} : null,
    querySelector: () => null,
  }};
  const window = {{__odysseusLastSelectedSessionId:'old-session'}};
  const _hashSessionCandidate = () => hash;
  const instance = new Function('sessionModule', '_hashSessionCandidate', 'document', 'window',
    'return (' + {json.dumps(function)} + ')')(
      sessionModule, _hashSessionCandidate, document, window);
  return {{result:await instance(), id, calls}};
}}
Promise.all([
  check('New Chat', '', 'old-session'),
  check('Existing chat', 'old-session', 'old-session'),
  check('New Chat', '', null),
]).then(result => console.log(JSON.stringify(result)));
"""
    output = subprocess.check_output(["node", "-e", script], text=True)
    fresh_stale, existing, fresh_empty = json.loads(output)
    assert fresh_stale == {"result": False, "id": None, "calls": [["reset", None]]}
    assert existing == {"result": True, "id": "old-session", "calls": []}
    assert fresh_empty == {"result": False, "id": None, "calls": []}
