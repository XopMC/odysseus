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
