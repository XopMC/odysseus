import io
import json
import zipfile
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.incident_export import build_incident_archive


def test_incident_archive_is_content_free_and_reproducible():
    secret = "PRIVATE_PROMPT_DO_NOT_EXPORT"
    run = SimpleNamespace(
        run_id="a" * 32, status="stopped", started_at=datetime(2026, 1, 1),
        terminal_at=None, last_seq=9, durable_seq=8, context_revision=3,
        context_snapshot={"messages": [secret]}, continuation={"prompt": secret},
    )
    intent = SimpleNamespace(run_id=run.run_id, status="unknown", receipt={"secret": secret})

    class Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return self.rows

    class Db:
        def query(self, *columns):
            from core.database import ChatRunState
            names = {column.key for column in columns}
            assert not names & {"context_snapshot", "continuation", "receipt", "action_hash"}
            return Query([run] if columns[0].class_ is ChatRunState else [intent])

        def close(self):
            pass

    with patch("src.incident_export.SessionLocal", return_value=Db()), \
         patch("src.incident_export.ReplayLog", side_effect=FileNotFoundError):
        first = build_incident_archive("session", "owner")
        second = build_incident_archive("session", "owner")

    assert first == second
    assert secret.encode() not in first
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["manifest.json", "summary.json", "runs.json", "event-schema.json", "replay-fixture.json"]
        runs = json.loads(archive.read("runs.json"))
        summary = json.loads(archive.read("summary.json"))
        fixture = json.loads(archive.read("replay-fixture.json"))
        assert runs[0]["effect_status_counts"] == {"unknown": 1}
        assert runs[0]["durable_seq"] == 8
        assert summary["max_durable_lag_events"] == 1
        assert summary["unknown_effect_count"] == 1
        assert [event["seq"] for event in fixture["events"]] == list(range(7))
        assert {event["run_id"] for event in fixture["events"]} == {"f" * 32}
        assert "context_snapshot" not in runs[0]
        assert "receipt" not in runs[0]


def test_incident_statuses_are_allowlisted():
    from src.incident_export import _status, _RUN_STATUSES, _EFFECT_STATUSES

    assert _status("error", _RUN_STATUSES) == "error"
    assert _status("prompt text accidentally stored as status", _RUN_STATUSES) == "unrecognized"
    assert _status("no_retry", _EFFECT_STATUSES) == "no_retry"


def test_incident_menu_downloads_only_owner_scoped_archive():
    app = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    assert "getCurrentSessionId()" in app
    assert "/api/chat/incident/${encodeURIComponent(sessionId)}" in app
    assert "credentials: 'same-origin'" in app
    assert "link.download = `odysseus-incident-${sessionId}.zip`" in app
