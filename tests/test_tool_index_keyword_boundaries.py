"""Keyword-hint force-include must match on word boundaries, not substrings.

`get_tools_for_query` force-includes whole tool families when a query mentions
an intent keyword. The match used a raw substring test (`kw in ql`), so short
hints fired inside unrelated words: "fix" in "prefix", "line" in "deadline"/
"online", "serve" in "observe"/"reserve", "reply" in "replying", "unread" in
"unreadable". That bloated the tool set with irrelevant email/document/serve
tools for queries that have nothing to do with them. Same substring-vs-word
pitfall already fixed in topic_analyzer.py.

`retrieve` (which needs a chroma collection) is stubbed out so these tests
exercise only the keyword-hint loop.
"""
from src.tool_index import ToolIndex


def _index():
    ti = ToolIndex.__new__(ToolIndex)
    ti.retrieve = lambda query, k=8: []  # no chroma; isolate the keyword loop
    return ti


def test_substring_inside_word_does_not_force_email_tools():
    ti = _index()
    # "replying" contains "reply"; "unreadable" contains "unread".
    for q in ("i am replying to your github comment", "this document is unreadable"):
        tools = ti.get_tools_for_query(q)
        assert "send_email" not in tools, q
        assert "reply_to_email" not in tools, q


def test_substring_inside_word_does_not_force_document_tools():
    ti = _index()
    # "prefix" contains "fix"; "deadline"/"online" contain "line".
    for q in ("prefix the output with a label", "the deadline is online already"):
        tools = ti.get_tools_for_query(q)
        assert "edit_document" not in tools, q
        assert "update_document" not in tools, q


def test_substring_inside_word_does_not_force_serve_tools():
    ti = _index()
    # "observe"/"reserve" contain "serve". serve_model/serve_preset are also in
    # ALWAYS_AVAILABLE, so pass a non-serve base to isolate the keyword loop (an
    # empty set falls back to ALWAYS_AVAILABLE). The "serve" hint must NOT fire.
    tools = ti.get_tools_for_query(
        "please observe the reserve levels", always_include={"__base__"}
    )
    assert "serve_model" not in tools
    assert "serve_preset" not in tools


def test_genuine_keywords_still_force_include():
    ti = _index()
    assert "reply_to_email" in ti.get_tools_for_query("reply to this email")
    assert "edit_document" in ti.get_tools_for_query("edit the document")
    assert "serve_model" in ti.get_tools_for_query("serve the model")


def test_find_info_online_forces_web_search_tools():
    ti = _index()
    tools = ti.get_tools_for_query("find info online about crow box designs")
    assert "web_search" in tools
    assert "web_fetch" in tools


def test_explicit_tree_and_outline_requests_load_specialized_schemas():
    ti = _index()
    assert "list_tree" in ti.get_tools_for_query("show the directory tree")
    assert "file_outline" in ti.get_tools_for_query("show the file outline")
    assert "list_tree" in ti.get_tools_for_query("call list_tree now")
    assert "file_outline" in ti.get_tools_for_query("call file_outline now")
    assert "file_outline" not in ti.get_tools_for_query(
        "Use bash if needed.", always_include={"bash"})


def test_explicit_git_status_request_loads_typed_tool():
    ti = _index()
    assert "git_status" in ti.get_tools_for_query("show git status")
    assert "git_status" not in ti.get_tools_for_query(
        "check the deadline", always_include={"bash"})


def test_verification_profiles_are_deferred_until_requested():
    ti = _index()
    assert "run_tests" in ti.get_tools_for_query("run_tests now")
    assert "run_lint" in ti.get_tools_for_query("run_lint now")
    assert "run_tests" not in ti.get_tools_for_query(
        "Use bash if needed.", always_include={"bash"})
    assert "run_lint" not in ti.get_tools_for_query(
        "Use bash if needed.", always_include={"bash"})


def test_explicit_artifact_search_is_selected_without_vector_index():
    ti = _index()
    assert "search_artifacts" in ti.get_tools_for_query(
        "call search_artifacts to find the earlier tool output")
    assert "search_artifacts" not in ti.get_tools_for_query(
        "search files in the repository", always_include={"read_file"})
