"""Ordinary long model output must not stall the server's sanitizer."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import src.agent_tools  # noqa: F401
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks, strip_tool_blocks_streaming


@pytest.mark.parametrize("padding,suffix", [
    (" " * 80000, "ordinary prose"),
    ("\n" * 12000, "ordinary prose"),
    ("\n" * 12000, "ui_contx"),
], ids=["long-spaces", "long-newlines", "newlines-near-command"])
def test_interior_whitespace_has_bounded_parse_and_stream_cost(padding, suffix):
    text = "Visible reasoning.\n" + padding + suffix
    expected = "Visible reasoning.\n\n" + suffix if "\n" in padding else text
    code = '''
import json
import sys
import src.agent_tools
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks, strip_tool_blocks_streaming
text, expected = json.load(sys.stdin)
assert parse_tool_blocks(text, skip_fenced=True) == []
assert strip_tool_blocks(text, skip_fenced=True) == expected
assert strip_tool_blocks_streaming(text, skip_fenced=True) == expected
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        input=json.dumps([text, expected]), capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("text,target", [
    (" \t`ui_control open_panel notes`\t ", "notes"),
    ("\n\n\tui_control\nopen_panel\nlibrary\n\n", "library"),
    ("``ui_control open_panel cookbook```", "cookbook"),
    ("\u00a0ui_control open_panel models\u00a0", "models"),
])
def test_supported_plain_ui_commands_still_dispatch_and_strip(text, target):
    blocks = parse_tool_blocks(text, skip_fenced=True)
    assert [(b.tool_type, b.content) for b in blocks] == [("ui_control", "open_panel " + target)]
    assert strip_tool_blocks(text, skip_fenced=True) == ""


def test_native_examples_and_partial_tool_markup_stay_inert():
    example = 'Example:\n```python\nprint("ui_control open_panel notes")\n```'
    assert strip_tool_blocks_streaming(example, skip_fenced=True) == example
    assert strip_tool_blocks_streaming("Visible reasoning.<tool_ca", skip_fenced=True) == "Visible reasoning."
