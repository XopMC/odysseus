"""Native tool-call text duplicates must not be re-fed to models."""
import src.agent_loop as agent_loop
from src.tool_parsing import strip_tool_blocks_streaming


def test_native_tool_markup_is_removed_from_followup_ledger(monkeypatch):
    # Focused agent-loop tests may stub agent_tools; use the real pure text
    # sanitizer to exercise this producer boundary.
    monkeypatch.setattr(agent_loop, "strip_tool_blocks_streaming", strip_tool_blocks_streaming)
    messages = []
    native = [{"id": "call-1", "name": "web_search", "arguments": '{"query":"fixture"}'}]
    assistant_text = 'Checking the result. <tool_call>{"name":"bash"}</tool_call>'

    agent_loop._append_tool_results(
        messages, assistant_text, native, [{}], ["safe result"],
        used_native=True, round_num=1,
    )

    assert messages[0]["content"] == "Checking the result."
    assert "<tool_call>" not in messages[0]["content"]
    assert '"name"' not in messages[0]["content"]
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call-1"


def test_non_native_markup_is_removed_from_followup_ledger(monkeypatch):
    monkeypatch.setattr(agent_loop, "strip_tool_blocks_streaming", strip_tool_blocks_streaming)
    messages = []
    assistant_text = 'Checking the result. <tool_call>{"name":"bash"}</tool_call>'

    agent_loop._append_tool_results(
        messages, assistant_text, [], ["safe result"], [],
        used_native=False, round_num=1,
    )

    assert messages[0]["content"] == "Checking the result."
    assert "<tool_call>" not in messages[0]["content"]
    assert "<tool_call>" not in messages[1]["content"]
