"""Real JS regression for child thinking/answer replay ordering."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_subagent_detail_keeps_thinking_tools_and_answer_in_separate_sections():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    module = (Path(__file__).resolve().parents[1] / "static/js/chat-subagents.js").as_uri()
    script = f"""
      globalThis.window = {{ location: {{ origin: 'http://qa.invalid' }} }};
      const {{ foldSubagentDetailEvents, formatSubagentDetailChannels }} = await import({json.dumps(module)});
      let state = {{ cursor: 0, thinking: '', tools: '', answer: '' }};
      state = foldSubagentDetailEvents(state, [
        {{ seq: 1, kind: 'delta', payload: {{ text: 'Answer first.' }} }},
        {{ seq: 2, kind: 'thinking', payload: {{ text: 'Reason one. ' }} }},
        {{ seq: 3, kind: 'tool_start', payload: {{ tool: 'python', command: 'print(42)' }} }},
      ]);
      state = foldSubagentDetailEvents(state, [
        {{ seq: 4, kind: 'thinking', payload: {{ text: 'Reason two.' }} }},
        {{ seq: 5, kind: 'delta', payload: {{ text: ' Done.' }} }},
      ]);
      const display = formatSubagentDetailChannels(state, {{
        thinking: 'Thinking', tools: 'Tools', answer: 'Answer',
      }});
      console.log(JSON.stringify({{ state, display }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["state"]["cursor"] == 5
    assert data["state"]["thinking"] == "Reason one. Reason two."
    assert data["state"]["answer"] == "Answer first. Done."
    assert data["display"] == (
        "Thinking\nReason one. Reason two.\n\n"
        "Tools\n▶ python print(42)\n\n"
        "Answer\nAnswer first. Done."
    )
