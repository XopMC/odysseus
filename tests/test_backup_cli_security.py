import io
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.cli_loader import load_script


def _load_backup_cli():
    return load_script("odysseus-backup")


def _patch_repo(module, monkeypatch, root: Path):
    monkeypatch.setattr(module, "_REPO_ROOT", root)
    monkeypatch.setattr(module, "_DATA_DIR", root / "data")
    monkeypatch.setattr(module, "_BACKUP_DIR", root / "backups")


def _restore_args(path: Path):
    return SimpleNamespace(path=str(path), yes=True, pretty=False)


def _verify_args(path: Path):
    return SimpleNamespace(path=str(path), pretty=False)


def test_backup_entry_skips_files_that_disappear():
    backup = _load_backup_cli()

    class Vanished:
        name = "gone.tar.gz"

        def is_file(self):
            return True

        def stat(self):
            raise FileNotFoundError("gone")

        def __str__(self):
            return "backups/gone.tar.gz"

    assert backup._backup_entry(Vanished()) is None


def test_backup_list_sorts_by_captured_mtime(monkeypatch):
    backup = _load_backup_cli()
    first = SimpleNamespace(name="older.tar.gz")
    second = SimpleNamespace(name="newer.tar.gz")
    monkeypatch.setattr(backup, "_BACKUP_DIR", SimpleNamespace(
        is_dir=lambda: True,
        iterdir=lambda: [first, second],
    ))
    monkeypatch.setattr(backup, "_backup_entry", lambda p: {
        "name": p.name,
        "modified": "2026-10-25T01:45:00" if p is first else "2026-10-25T01:15:00",
        "_mtime": 100 if p is first else 200,
    })
    seen = []
    monkeypatch.setattr(backup, "emit", lambda payload, args: seen.append(payload))

    backup.cmd_list(SimpleNamespace(pretty=False))

    assert [entry["name"] for entry in seen[0]] == ["newer.tar.gz", "older.tar.gz"]
    assert all("_mtime" not in entry for entry in seen[0])


def test_snapshot_rejects_output_inside_data_dir(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)

    with pytest.raises(SystemExit):
        backup._reject_output_inside_data(data / "self.tar.gz")


def test_wal_snapshot_restore_excludes_post_snapshot_commit(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "snapshot.tar.gz"
    db_path = data / "app.db"
    (data / "notes-wal").write_text("ordinary user file", encoding="utf-8")
    original_copy = backup._sqlite_safe_copy
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE evidence (value TEXT)")
        writer.execute("INSERT INTO evidence VALUES ('at_snapshot')")
        writer.commit()

        def copy_then_commit(src, dst):
            original_copy(src, dst)
            writer.execute("INSERT INTO evidence VALUES ('after_snapshot')")
            writer.commit()
            # The directory walk must exclude every staged DB sidecar,
            # including one appearing after the backup API call finishes.
            Path(str(src) + "-journal").write_bytes(b"stale journal")

        monkeypatch.setattr(backup, "_sqlite_safe_copy", copy_then_commit)
        backup.cmd_snapshot(SimpleNamespace(
            out=str(archive), include_research=True,
            include_attachments=True, pretty=False,
        ))
        assert Path(str(db_path) + "-wal").exists()
        assert Path(str(db_path) + "-shm").exists()
        with tarfile.open(archive, "r:gz") as tar:
            assert set(tar.getnames()) == {"data/app.db", "data/notes-wal"}
    finally:
        writer.close()

    backup.cmd_restore(_restore_args(archive))
    restored = sqlite3.connect(data / "app.db")
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM evidence").fetchall() == [("at_snapshot",)]
    finally:
        restored.close()
    assert (data / "notes-wal").read_text(encoding="utf-8") == "ordinary user file"


@pytest.mark.parametrize("failure_at", ["source_connect", "destination_connect", "backup"])
def test_sqlite_snapshot_failure_closes_handles_without_raw_copy(tmp_path, monkeypatch, failure_at):
    backup = _load_backup_cli()
    src, dst = tmp_path / "source.db", tmp_path / "staged.db"
    src.write_bytes(b"must never be copied as a fallback")
    handles = []
    connect = sqlite3.connect

    class BrokenBackupConnection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            raise sqlite3.OperationalError("injected backup failure")

    def connect_with_failure(path):
        if ((failure_at == "source_connect" and path == str(src))
                or (failure_at == "destination_connect" and path == str(dst))):
            raise sqlite3.OperationalError("injected connection failure")
        connection = connect(path, factory=BrokenBackupConnection)
        handles.append(connection)
        return connection

    monkeypatch.setattr(backup.sqlite3, "connect", connect_with_failure)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        backup._sqlite_safe_copy(src, dst)
    assert len(handles) == {"source_connect": 0, "destination_connect": 1, "backup": 2}[failure_at]
    for connection in handles:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert not dst.exists() or dst.read_bytes() != src.read_bytes()


def test_invalid_sqlite_aborts_snapshot_before_overwriting_archive(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "app.db").write_bytes(b"not a SQLite database")
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "existing.tar.gz"
    archive.write_bytes(b"previous backup")
    emitted = []
    monkeypatch.setattr(backup, "emit", lambda payload, args: emitted.append(payload))

    with pytest.raises(sqlite3.DatabaseError):
        backup.cmd_snapshot(SimpleNamespace(
            out=str(archive), include_research=True,
            include_attachments=True, pretty=False,
        ))
    assert archive.read_bytes() == b"previous backup"
    assert emitted == []


def test_restore_rejects_symlink_escape(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    outside = tmp_path / "outside"
    data.mkdir(parents=True)
    outside.mkdir()
    (data / "keep.txt").write_text("still here", encoding="utf-8")
    _patch_repo(backup, monkeypatch, repo)

    tar_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        data_dir = tarfile.TarInfo("data")
        data_dir.type = tarfile.DIRTYPE
        tar.addfile(data_dir)

        link = tarfile.TarInfo("data/link")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tar.addfile(link)

        payload = b"escaped"
        escaped = tarfile.TarInfo("data/link/pwned.txt")
        escaped.size = len(payload)
        tar.addfile(escaped, io.BytesIO(payload))

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(tar_path))

    assert not (outside / "pwned.txt").exists()
    assert (data / "keep.txt").read_text(encoding="utf-8") == "still here"


def test_verify_rejects_symlink_escape(tmp_path):
    backup = _load_backup_cli()

    tar_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        link = tarfile.TarInfo("data/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp"
        tar.addfile(link)

    with pytest.raises(SystemExit):
        backup.cmd_verify(_verify_args(tar_path))


def test_restore_rejects_hardlink_entries(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)

    tar_path = tmp_path / "hardlink.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        link = tarfile.TarInfo("data/hardlink")
        link.type = tarfile.LNKTYPE
        link.linkname = "../outside.txt"
        tar.addfile(link)

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(tar_path))


def test_restore_extracts_regular_files_without_extractall(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "old.txt").write_text("old", encoding="utf-8")
    _patch_repo(backup, monkeypatch, repo)

    tar_path = tmp_path / "valid.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        folder = tarfile.TarInfo("data/nested")
        folder.type = tarfile.DIRTYPE
        tar.addfile(folder)

        payload = b"new"
        item = tarfile.TarInfo("data/nested/new.txt")
        item.size = len(payload)
        tar.addfile(item, io.BytesIO(payload))

    backup.cmd_restore(_restore_args(tar_path))

    assert (repo / "data" / "nested" / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (repo / "data" / "old.txt").exists()
    assert list(repo.glob("data.before-restore-*"))
