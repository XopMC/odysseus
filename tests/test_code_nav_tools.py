"""Tests for the code-navigation tools (grep, glob, ls) + read_file line range."""
import os
import shutil
import asyncio
import tempfile
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test_code_nav.db")

from src.tool_execution import _direct_fallback


def _run(tool, content):
    return asyncio.run(_direct_fallback(tool, content))


@pytest.fixture
def repo():
    # Built under /tmp, which is on the default tool-path allowlist.
    root = tempfile.mkdtemp(dir="/tmp", prefix="codenav_")
    try:
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("import os\n# needle here\nprint('x')\n")
        os.mkdir(os.path.join(root, "sub"))
        with open(os.path.join(root, "sub", "b.txt"), "w") as f:
            f.write("nothing\nNEEDLE upper\n")
        os.mkdir(os.path.join(root, "sub", "deep"))
        with open(os.path.join(root, "sub", "deep", "c.py"), "w") as f:
            f.write("# deep python\n")
        os.mkdir(os.path.join(root, "node_modules"))
        with open(os.path.join(root, "node_modules", "dep.py"), "w") as f:
            f.write("needle in dep\n")
        g = os.path.join(root, ".git")
        os.mkdir(g)
        with open(os.path.join(g, "config"), "w") as f:
            f.write("needle in git\n")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── grep ──────────────────────────────────────────────────────────────────

def test_grep_finds_match(repo):
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]


def test_grep_skips_junk_dirs(repo):
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert "node_modules" not in r["output"]
    assert ".git/config" not in r["output"]


def test_grep_ignore_case(repo):
    r = _run("grep", f'{{"pattern": "needle", "ignore_case": true, "path": "{repo}"}}')
    assert "b.txt:2:" in r["output"]


def test_grep_glob_filter(repo):
    r = _run("grep", f'{{"pattern": "needle", "ignore_case": true, "glob": "*.py", "path": "{repo}"}}')
    assert "a.py" in r["output"]
    assert "b.txt" not in r["output"]


def test_grep_no_match(repo):
    r = _run("grep", f'{{"pattern": "zzzznotfound", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "No matches" in r["output"]


def test_search_files_defaults_to_paged_file_list(repo):
    import json
    first = _run("search_files", json.dumps({
        "pattern": "needle", "path": repo, "ignore_case": True, "page_size": 1,
    }))
    assert first["exit_code"] == 0
    assert len(first["files"]) == 1
    assert first["next_cursor"] == 1
    assert "needle here" not in first["output"]
    second = _run("search_files", json.dumps({
        "pattern": "needle", "path": repo, "ignore_case": True,
        "page_size": 1, "cursor": first["next_cursor"],
    }))
    assert second["exit_code"] == 0
    assert len(second["files"]) == 1
    assert first["files"] != second["files"]
    assert second["next_cursor"] is None
    assert all("node_modules" not in path and ".git" not in path
               for path in first["files"] + second["files"])


def test_search_files_exact_matches_and_scope(repo):
    import json
    selected = os.path.join(repo, "a.py")
    result = _run("search_files", json.dumps({
        "pattern": "needle", "path": selected, "mode": "matches",
        "page_size": 1,
    }))
    assert result["exit_code"] == 0
    assert "a.py:2:" in result["output"]
    assert "needle here" in result["output"]
    assert _run("search_files", json.dumps({
        "pattern": "needle", "path": "/etc", "mode": "files",
    }))["exit_code"] == 1
    assert _run("search_files", json.dumps({
        "pattern": "needle", "path": repo, "cursor": -1,
    }))["exit_code"] == 1


def test_search_files_native_and_python_fallback(repo, monkeypatch):
    import json
    from src.tool_schemas import function_call_to_tool_block
    block = function_call_to_tool_block("search_files", json.dumps({
        "pattern": "needle", "path": repo, "ignore_case": True,
    }))
    assert block is not None
    native_result = _run(block.tool_type, block.content)
    assert len(native_result["files"]) == 2
    monkeypatch.setattr(shutil, "which", lambda name: None)
    fallback_result = _run("search_files", json.dumps({
        "pattern": "needle", "path": repo, "ignore_case": True,
    }))
    assert fallback_result["files"] == native_result["files"]


def test_search_files_excludes_sensitive_file_names(repo):
    import json
    secret = os.path.join(repo, "ID_RSA")
    with open(secret, "w") as stream:
        stream.write("needle\n")
    result = _run("search_files", json.dumps({"pattern": "needle", "path": repo}))
    assert result["exit_code"] == 0
    assert secret not in result["files"]


def test_search_files_is_bound_to_active_workspace(repo):
    import json
    from src.tool_execution import _active_workspace
    token = _active_workspace.set(os.path.realpath(repo))
    try:
        local = _run("search_files", json.dumps({"pattern": "needle"}))
        assert local["exit_code"] == 0
        canonical_repo = os.path.realpath(repo)
        assert all(path.startswith(canonical_repo + os.sep) for path in local["files"])
        escaped = _run("search_files", json.dumps({
            "pattern": "needle", "path": "/tmp",
        }))
        assert escaped["exit_code"] == 1
    finally:
        _active_workspace.reset(token)


def test_search_files_paginates_distinct_files_not_lines(repo):
    import json
    for number in range(62):
        with open(os.path.join(repo, f"many-{number:03d}.txt"), "w") as stream:
            stream.write("many-hit\n" * 20)
    paths, cursor = [], 0
    while True:
        page = _run("search_files", json.dumps({
            "pattern": "many-hit", "path": repo, "page_size": 17,
            "cursor": cursor,
        }))
        assert page["exit_code"] == 0
        paths.extend(page["files"])
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert len(paths) == 62
    assert len(set(paths)) == 62
    assert paths == sorted(paths)


def test_list_tree_returns_bounded_hierarchy_without_file_body(repo):
    import json
    result = _run("list_tree", json.dumps({"path": repo, "max_depth": 3}))
    assert result["exit_code"] == 0
    paths = [entry["path"] for entry in result["entries"]]
    assert "a.py" in paths
    assert "sub/b.txt" in paths
    assert "sub/deep/c.py" in paths
    assert all("node_modules" not in path and ".git" not in path for path in paths)
    assert "needle here" not in result["output"]
    assert next(entry for entry in result["entries"] if entry["path"] == "a.py")["size_bytes"] > 0
    limited = _run("list_tree", json.dumps({"path": repo, "max_entries": 2}))
    assert len(limited["entries"]) == 2
    assert limited["truncated"] is True
    assert _run("list_tree", json.dumps({"path": "/etc"}))["exit_code"] == 1


def test_list_tree_honors_gitignore(repo):
    import json
    import subprocess
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="tree_ignore_") as root:
        subprocess.run(["git", "init", "-q", root], check=True)
        with open(os.path.join(root, ".gitignore"), "w") as stream:
            stream.write("ignored.txt\n")
        with open(os.path.join(root, "ignored.txt"), "w") as stream:
            stream.write("ignored")
        with open(os.path.join(root, "kept.txt"), "w") as stream:
            stream.write("kept")
        result = _run("list_tree", json.dumps({"path": root}))
        paths = {entry["path"] for entry in result["entries"]}
        assert "kept.txt" in paths
        assert "ignored.txt" not in paths


def test_file_outline_python_ast_and_explicit_unsupported(repo):
    import json
    source = os.path.join(repo, "symbols.py")
    with open(source, "w") as stream:
        stream.write("class Widget:\n    def run(self):\n        pass\n\nasync def fetch():\n    return 1\n")
    result = _run("file_outline", json.dumps({"path": source}))
    assert result["exit_code"] == 0
    assert [(item["kind"], item["name"], item["line"]) for item in result["symbols"]] == [
        ("class", "Widget", 1), ("method", "Widget.run", 2),
        ("async_function", "fetch", 5),
    ]
    assert "return 1" not in result["output"]
    assert _run("file_outline", json.dumps({"path": os.path.join(repo, "sub/b.txt")}))["exit_code"] == 1
    assert _run("file_outline", json.dumps({"path": "/etc/passwd"}))["exit_code"] == 1


def test_grep_requires_pattern(repo):
    r = _run("grep", "{}")
    assert r["exit_code"] == 1
    assert "pattern is required" in r["error"]


def test_grep_path_outside_roots_rejected(repo):
    r = _run("grep", '{"pattern": "x", "path": "/etc"}')
    assert r["exit_code"] == 1
    assert "outside the allowed roots" in r["error"]


def test_grep_python_fallback_when_no_rg(repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]
    assert "node_modules" not in r["output"]
    assert ".git/config" not in r["output"]


def test_grep_python_fallback_uses_relative_glob_paths(repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = _run(
        "grep",
        f'{{"pattern": "needle|python", "glob": "**/*.py", "path": "{repo}"}}',
    )
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]
    assert "sub/deep/c.py" in r["output"]


@pytest.mark.skipif(shutil.which("rg") is None, reason="targets the ripgrep fast-path")
def test_grep_skips_case_variant_sensitive_files_rg(repo):
    """The rg fast-path must exclude deny-listed key files case-insensitively.

    A file whose name is a case variant of a sensitive pattern (e.g. ID_RSA vs
    id_rsa, Known_Hosts vs known_hosts) points at the same secret on a
    case-insensitive filesystem, so grep must not return its contents. The
    Python fallback already folds case via _is_sensitive_path; a plain --glob
    exclusion is case-sensitive, so it would leak these — this pins the rg path.
    """
    token = "GREPSECRET_TOKEN_ZZZ"
    with open(os.path.join(repo, "notes.txt"), "w") as f:
        f.write(f"see {token}\n")
    with open(os.path.join(repo, "ID_RSA"), "w") as f:
        f.write(f"PRIVATE {token}\n")
    with open(os.path.join(repo, "Known_Hosts"), "w") as f:
        f.write(f"host {token}\n")
    r = _run("grep", f'{{"pattern": "{token}", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "notes.txt" in r["output"]        # ordinary matches still returned
    assert "ID_RSA" not in r["output"]        # case-variant key excluded
    assert "Known_Hosts" not in r["output"]


# ── glob ──────────────────────────────────────────────────────────────────

def test_glob_py(repo):
    r = _run("glob", f'{{"pattern": "*.py", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]


def test_glob_recursive_skips_junk(repo):
    r = _run("glob", f'{{"pattern": "**/*.py", "path": "{repo}"}}')
    assert "a.py" in r["output"]
    assert "node_modules" not in r["output"]


def test_glob_requires_pattern(repo):
    r = _run("glob", "{}")
    assert r["exit_code"] == 1


def test_glob_literal_in_subdir(repo):
    """Bare literal should match at any depth (like rglob), not only at root."""
    r = _run("glob", f'{{"pattern": "b.txt", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "b.txt" in r["output"]


def test_glob_multi_segment_single_star(repo):
    """sub/*.txt matches sub/b.txt but NOT sub/deep/c.py (single * stays in one segment)."""
    r = _run("glob", f'{{"pattern": "sub/*.txt", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "b.txt" in r["output"]
    assert "c.py" not in r["output"]


def test_glob_star_does_not_cross_slash(repo):
    """src/*.py must NOT match src/a/b/x.py — * is single-segment only."""
    r = _run("glob", f'{{"pattern": "sub/*.py", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    # sub/ has no .py directly, only sub/deep/c.py — should NOT match
    assert "No files matching" in r["output"]


def test_glob_double_star_matches_deep(repo):
    """**/*.py should match files at any depth."""
    r = _run("glob", f'{{"pattern": "**/*.py", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]
    assert "c.py" in r["output"]


# ── ls ────────────────────────────────────────────────────────────────────

def test_ls_lists_entries(repo):
    r = _run("ls", f'{{"path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]
    assert "sub/" in r["output"]
    assert ".git" not in r["output"]  # hidden skipped


def test_ls_path_outside_rejected(repo):
    r = _run("ls", '{"path": "/etc"}')
    assert r["exit_code"] == 1
    assert "outside the allowed roots" in r["error"]


# ── read_file line range ───────────────────────────────────────────────────

def test_read_file_offset_limit(repo):
    p = os.path.join(repo, "lines.txt")
    with open(p, "w") as f:
        f.write("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    r = _run("read_file", f'{{"path": "{p}", "offset": 3, "limit": 2}}')
    assert r["exit_code"] == 0
    assert r["output"] == "line3\nline4\n"


def test_read_file_plain_path_backcompat(repo):
    r = _run("read_file", os.path.join(repo, "a.py"))
    assert r["exit_code"] == 0
    assert "needle" in r["output"]


def test_read_file_v2_model_guidance_explains_bounded_ranges_and_artifacts():
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS

    for description in (TOOL_SECTIONS["read_file"], BUILTIN_TOOL_DESCRIPTIONS["read_file"]):
        assert "byte_offset" in description
        assert "line_numbers" in description
        assert "read_tool_artifact" in description
        assert "sha256" in description
        assert "megabytes" in description or "megabytes inline" in description


def test_search_files_v2_prompt_and_schema_explain_paging_contract():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    description = BUILTIN_TOOL_DESCRIPTIONS["search_files"]
    for term in ("mode=files", "mode=matches", "next_cursor", "page_size"):
        assert term in description
    schema = next(
        item["function"] for item in FUNCTION_TOOL_SCHEMAS
        if item["function"]["name"] == "search_files"
    )
    properties = schema["parameters"]["properties"]
    assert "next_cursor" in properties["cursor"]["description"]
    assert "default 25" in properties["page_size"]["description"]
    assert properties["page_size"]["maximum"] == 50


def test_list_tree_and_outline_prompts_state_scope_and_bounds():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    catalog = {
        item["function"]["name"]: item["function"]
        for item in FUNCTION_TOOL_SCHEMAS
    }
    tree = BUILTIN_TOOL_DESCRIPTIONS["list_tree"]
    outline = BUILTIN_TOOL_DESCRIPTIONS["file_outline"]
    assert all(term in tree for term in (".gitignore", "without opening file bodies", "depth 2/100", "200"))
    assert all(term in outline for term in ("Python-stub", "start/end line numbers", "2 MiB", "unavailable"))
    assert "default 2" in catalog["list_tree"]["parameters"]["properties"]["max_depth"]["description"]
    assert "default 100" in catalog["list_tree"]["parameters"]["properties"]["max_entries"]["description"]
    assert "default 100" in catalog["file_outline"]["parameters"]["properties"]["max_symbols"]["description"]


def test_read_file_v2_metadata_line_numbers_and_binary(repo):
    import hashlib
    import json
    path = os.path.join(repo, "read-v2.txt")
    raw = b"one\ntwo\nthree\n"
    with open(path, "wb") as stream:
        stream.write(raw)
    result = _run("read_file", json.dumps({"path": path, "offset": 2, "limit": 1,
                                            "line_numbers": True}))
    assert result["exit_code"] == 0
    assert result["output"] == "2: two\n"
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["size_bytes"] == len(raw)
    assert result["encoding"] == "utf-8"
    assert result["is_binary"] is False
    chunk = _run("read_file", json.dumps({"path": path, "byte_offset": 4,
                                           "byte_limit": 3}))
    assert chunk["output"] == "two"
    assert chunk["byte_range"] == [4, 7]
    beyond = _run("read_file", json.dumps({"path": path, "byte_offset": 100}))
    assert beyond["byte_range"] == [len(raw), len(raw)]
    assert beyond["output"] == ""
    invalid = _run("read_file", json.dumps({"path": path, "byte_offset": 1,
                                             "offset": 2}))
    assert invalid["exit_code"] == 1
    binary = os.path.join(repo, "read-v2.bin")
    with open(binary, "wb") as stream:
        stream.write(b"a\x00b")
    result = _run("read_file", json.dumps({"path": binary}))
    assert result["exit_code"] == 0
    assert result["is_binary"] is True
    assert "\x00" not in result["output"]


def test_read_file_large_text_has_owner_scoped_artifact(repo, monkeypatch):
    import json
    from src.agent_tools.filesystem_tools import ReadFileTool
    from src import observation_pack
    monkeypatch.setattr(observation_pack, "DATA_DIR", repo)
    path = os.path.join(repo, "large.txt")
    original = "line\n" * 6000
    with open(path, "w") as stream:
        stream.write(original)
    payload = json.dumps({"path": path})
    first = asyncio.run(ReadFileTool().execute(payload, {"owner": "alice", "session_id": "s1"}))
    assert first["exit_code"] == 0
    assert first["truncated"] is True
    assert len(first["output"]) < 3000
    assert "read_tool_artifact" in first["output"]
    assert first["artifact_id"].startswith("obs_")
    recalled = observation_pack.recall("alice", "s1", first["artifact_id"], 0)
    assert recalled["text"].startswith("line\n")
    with pytest.raises(FileNotFoundError):
        observation_pack.recall("bob", "s1", first["artifact_id"], 0)
    anonymous = asyncio.run(ReadFileTool().execute(payload, {}))
    assert "artifact_id" not in anonymous
    local = asyncio.run(ReadFileTool().execute(payload, {"session_id": "local-session"}))
    assert local["artifact_id"].startswith("obs_")
    assert observation_pack.recall(None, "local-session", local["artifact_id"], 0)["text"].startswith("line\n")


def test_model_read_file_dispatch_keeps_owner_and_session_for_artifact(repo, monkeypatch):
    import json
    from types import SimpleNamespace
    from src import observation_pack, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    monkeypatch.setattr(observation_pack, "DATA_DIR", repo)
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    path = os.path.join(repo, "large-dispatch.txt")
    with open(path, "w") as stream:
        stream.write("safe-line\n" * 4000)
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="read_file", content=json.dumps({"path": path})),
        owner="alice", session_id="smoke-session",
        security_context=ToolRunSecurityContext(),
    ))
    assert result["exit_code"] == 0
    assert result["artifact_id"].startswith("obs_")
    assert "read_tool_artifact" in result["output"]


def test_native_read_file_preserves_v2_range_arguments(repo):
    import json
    from src.tool_schemas import function_call_to_tool_block
    path = os.path.join(repo, "native.txt")
    with open(path, "w") as stream:
        stream.write("one\ntwo\nthree\n")
    block = function_call_to_tool_block("read_file", json.dumps({
        "path": path, "byte_offset": 4, "byte_limit": 3,
    }))
    assert block is not None
    assert _run(block.tool_type, block.content)["output"] == "two"
    numbered = function_call_to_tool_block("read_file", json.dumps({
        "path": path, "line_numbers": True,
    }))
    assert numbered is not None
    assert _run(numbered.tool_type, numbered.content)["output"].startswith("1: one\n")
