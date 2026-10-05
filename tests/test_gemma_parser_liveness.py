"""Gemma marker floods must never monopolize the agent server's GIL."""
import subprocess
import sys
from pathlib import Path

import pytest
import src.agent_tools  # noqa: F401  (tool registration/import-cycle boundary)
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks, strip_tool_blocks_streaming


@pytest.mark.parametrize("marker", ["<|tool_call|>", "<|tool_call>", "<tool_call|>"])
def test_gemma_valid_calls_and_partial_streams_remain_usable(marker):
    raw = marker + 'call:read-file{"path":"README.md"}' + marker
    blocks = parse_tool_blocks(raw)
    assert [(b.tool_type, b.content) for b in blocks] == [("read_file", "README.md")]
    assert strip_tool_blocks("Before " + raw + " After") == "Before  After"
    for length in range(1, len(raw) + 1):
        assert strip_tool_blocks_streaming("Visible reasoning. " + raw[:length]).strip() == "Visible reasoning."


def test_gemma_plain_argument_repair_keeps_multiple_calls():
    raw = ('<|tool_call|>call:read_file{path: README.md}<tool_call|>\n'
           '<|tool_call|>call:web_search{query: hello world}<|tool_call|>')
    assert [(b.tool_type, b.content) for b in parse_tool_blocks(raw)] == [
        ("read_file", "README.md"), ("web_search", "hello world"),
    ]
    assert strip_tool_blocks(raw) == ""


@pytest.mark.parametrize("prefix", ["", "}<|tool_call|>"])
def test_unclosed_gemma_flood_has_bounded_parse_and_display_cost(prefix):
    # A subprocess bounds the old regex's CPU hang without hanging pytest.
    # Only synthetic protocol data is used; no production transcript is read.
    code = f'''
import src.agent_tools
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks, strip_tool_blocks_streaming
text = {prefix!r} + "<|tool_call|>call:read_file{{" * 8000
assert parse_tool_blocks(text) == []
assert strip_tool_blocks(text, skip_fenced=True) == text
assert strip_tool_blocks_streaming("Visible reasoning. " + text, skip_fenced=True).strip() == "Visible reasoning." + (" }}" if {prefix!r} else "")
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stderr


def test_closed_gemma_with_unparsable_long_argument_fails_closed_promptly():
    code = '''
import src.agent_tools
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks
text = "<|tool_call|>call:read_file{" + "x" * 200000 + "}<|tool_call|>"
assert parse_tool_blocks(text) == []
assert strip_tool_blocks(text) == ""
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stderr
