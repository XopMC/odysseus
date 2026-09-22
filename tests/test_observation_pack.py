import os

from src import observation_pack as pack


def test_large_observation_is_full_twice_then_replaced_and_recalled(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    body = "head line\n" + ("middle evidence\n" * 900) + "tail line\n"
    tool = {
        "role": "tool", "tool_call_id": "call-1", "content": body,
        "_observation_source": {"tool_name": "bash", "tool_call_id": "call-1"},
    }
    first, info = pack.project_messages([tool], owner="alice", session_id="s1")
    assert first[0]["content"] == body and info["packed"] == 0
    second, _ = pack.project_messages([tool, {"role": "assistant", "content": "one"}], owner="alice", session_id="s1")
    assert second[0]["content"] == body
    third, info = pack.project_messages([
        tool, {"role": "assistant", "content": "one"}, {"role": "assistant", "content": "two"},
    ], owner="alice", session_id="s1")
    assert info["packed"] == 1 and info["removed_bytes"] > 10_000
    placeholder = third[0]["content"]
    oid = next(line.split(": ", 1)[1] for line in placeholder.splitlines() if line.startswith("id: "))
    parts = []
    offset = 0
    while True:
        chunk = pack.recall("alice", "s1", oid, offset)
        parts.append(chunk["text"])
        if chunk["eof"]:
            break
        offset = chunk["next_offset"]
    assert "".join(parts) == body


def test_observation_scope_and_symlink_recall_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    body = "x\n" * 7000
    meta = pack.archive("alice", "s1", tool_name="bash", tool_call_id="c", text=body)
    assert meta
    try:
        pack.recall("bob", "s1", meta["id"])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("cross-owner observation read succeeded")
    path = pack._path("alice", "s1", meta["id"])
    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    path.unlink()
    os.symlink(target, path)
    try:
        pack.recall("alice", "s1", meta["id"])
    except OSError:
        pass
    else:
        raise AssertionError("symlink observation read succeeded")


def test_projection_fails_open_when_store_is_unavailable(monkeypatch):
    monkeypatch.setattr(pack, "archive", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    body = "large\n" * 3000
    projected, info = pack.project_messages([
        {"role": "tool", "content": body,
         "_observation_source": {"tool_name": "bash", "tool_call_id": "x"}},
        {"role": "assistant", "content": "one"},
        {"role": "assistant", "content": "two"},
    ], owner="alice", session_id="s1")
    assert projected[0]["content"] == body
    assert info == {"packed": 0, "removed_bytes": 0}


def test_owner_quota_and_session_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pack, "get_setting", lambda key, default=None: {
        "observation_pack_object_max_bytes": 20_000,
        "observation_pack_owner_max_bytes": 22_000,
    }.get(key, default))
    one = pack.archive("alice", "s1", tool_name="bash", tool_call_id="1", text="a" * 12_000)
    assert one and pack.owner_usage("alice") == 12_000
    try:
        pack.archive("alice", "s2", tool_name="bash", tool_call_id="2", text="b" * 12_000)
    except OSError as exc:
        assert "quota" in str(exc).lower()
    else:
        raise AssertionError("owner quota was not enforced")
    assert pack.archive("bob", "s2", tool_name="bash", tool_call_id="2", text="b" * 12_000)
    assert pack.delete_session("alice", "s1") is True
    assert pack.owner_usage("alice") == 0
    assert pack.owner_usage("bob") == 12_000


def test_run_scoped_artifact_search_is_bounded_and_owner_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    run_a, run_b = "a" * 32, "b" * 32
    first = pack.archive("alice", "chat", tool_name="bash", tool_call_id="a",
                         text="top\nneedle on line two\n", force=True, run_id=run_a)
    second = pack.archive("alice", "chat", tool_name="read_file", tool_call_id="b",
                          text="another needle\n", force=True, run_id=run_a)
    pack.archive("alice", "chat", tool_name="bash", tool_call_id="c",
                 text="needle from other run\n", force=True, run_id=run_b)
    pack.archive("bob", "chat", tool_name="bash", tool_call_id="d",
                 text="needle from other owner\n", force=True, run_id=run_a)
    page = pack.search("alice", "chat", run_a, "needle", limit=1)
    assert page["run_id"] == run_a
    assert len(page["matches"]) == 1 and page["next_cursor"]
    other = pack.search("alice", "chat", run_a, "needle", limit=1,
                        cursor=page["next_cursor"])
    assert len(other["matches"]) == 1 and not other["next_cursor"]
    assert {page["matches"][0]["id"], other["matches"][0]["id"]} == {first["id"], second["id"]}
    assert pack.search("bob", "chat", run_b, "needle")["matches"] == []
    assert pack.search("alice", "other-chat", run_a, "needle")["matches"] == []
    assert all("from other" not in item["snippet"] for item in page["matches"] + other["matches"])


def test_artifact_search_rejects_bad_run_cursor_and_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    run_id = "a" * 32
    meta = pack.archive("alice", "chat", tool_name="bash", tool_call_id="a",
                        text="needle\n", force=True, run_id=run_id)
    for kwargs in ({"run_id": "../escape"}, {"run_id": run_id, "cursor": "../escape"},
                   {"run_id": run_id, "query": ""}):
        options = {"run_id": run_id, "query": "needle", **kwargs}
        try:
            pack.search("alice", "chat", options.pop("run_id"), options.pop("query"), **options)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid artifact search argument accepted")
    target = tmp_path / "outside.txt"
    target.write_text("needle secret", encoding="utf-8")
    path = pack._path("alice", "chat", meta["id"])
    path.unlink()
    os.symlink(target, path)
    assert pack.search("alice", "chat", run_id, "needle")["matches"] == []


def test_artifact_search_scan_budget_resumes_without_skipping(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pack, "SEARCH_MAX_BYTES", 40)
    run_id = "a" * 32
    expected = set()
    for index in range(3):
        meta = pack.archive("alice", "chat", tool_name="bash", tool_call_id=str(index),
                            text=f"needle {index}\n" + "x" * 20, force=True, run_id=run_id)
        expected.add(meta["id"])
    found = set()
    cursor = None
    for _ in range(3):
        page = pack.search("alice", "chat", run_id, "needle", cursor=cursor)
        found.update(item["id"] for item in page["matches"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert found == expected and cursor is None


def test_observation_parent_symlinks_cannot_escape_owner_session(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    run_id = "a" * 32
    meta = pack.archive("alice", "chat", tool_name="bash", tool_call_id="call",
                        text="needle\n", force=True, run_id=run_id)
    scope = pack._scope("alice", "chat")
    runs = scope / "runs"
    moved_runs = tmp_path / "moved-runs"
    runs.rename(moved_runs)
    os.symlink(moved_runs, runs)
    try:
        pack.search("alice", "chat", run_id, "needle")
    except OSError:
        pass
    else:
        raise AssertionError("Search followed symlinked run parent")
    runs.unlink()
    moved_runs.rename(runs)
    objects = scope / "objects"
    moved_objects = tmp_path / "moved-objects"
    objects.rename(moved_objects)
    os.symlink(moved_objects, objects)
    try:
        pack.recall("alice", "chat", meta["id"])
    except OSError:
        pass
    else:
        raise AssertionError("Recall followed symlinked object parent")


def test_indexed_observation_recall_requires_matching_run_and_legacy_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    run_a, run_b = "a" * 32, "b" * 32
    indexed = pack.archive("alice", "chat", tool_name="bash", tool_call_id="indexed",
                           text="private result\n", force=True, run_id=run_a)
    assert pack.recall("alice", "chat", indexed["id"], run_id=run_a)["text"] == "private result\n"
    for wrong_run in (run_b, None):
        try:
            pack.recall("alice", "chat", indexed["id"], run_id=wrong_run)
        except (PermissionError, FileNotFoundError):
            pass
        else:
            raise AssertionError("Run-bound observation escaped its run")
    legacy = pack.archive("alice", "chat", tool_name="bash", tool_call_id="legacy",
                          text="old result\n", force=True)
    assert pack.recall("alice", "chat", legacy["id"], run_id=run_b)["text"] == "old result\n"


def test_observation_owner_directory_symlink_is_not_followed(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    run_id = "a" * 32
    meta = pack.archive("alice", "chat", tool_name="bash", tool_call_id="call",
                        text="needle\n", force=True, run_id=run_id)
    owner_dir = pack._scope("alice", "chat").parent
    moved = tmp_path / "moved-owner"
    owner_dir.rename(moved)
    os.symlink(moved, owner_dir)
    for operation in (lambda: pack.recall("alice", "chat", meta["id"], run_id=run_id),
                      lambda: pack.search("alice", "chat", run_id, "needle")):
        try:
            operation()
        except OSError:
            pass
        else:
            raise AssertionError("Observation followed a symlinked owner directory")


def test_append_only_ledger_records_full_placeholder_and_recall(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "DATA_DIR", str(tmp_path))
    body = "line\n" * 3000
    tool = {"role": "tool", "content": body,
            "_observation_source": {"tool_name": "bash", "tool_call_id": "call-ledger"}}
    pack.project_messages([tool], owner="alice", session_id="s1")
    projected, _ = pack.project_messages([
        tool, {"role": "assistant", "content": "one"}, {"role": "assistant", "content": "two"},
    ], owner="alice", session_id="s1")
    oid = next(line.split(": ", 1)[1] for line in projected[0]["content"].splitlines() if line.startswith("id: "))
    pack.recall("alice", "s1", oid, 0)
    ledger = (pack._scope("alice", "s1") / "ledger.jsonl").read_text()
    assert '"event": "full"' in ledger
    assert '"event": "placeholder"' in ledger
    assert '"event": "recall"' in ledger
