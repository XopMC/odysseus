import io
import json
import zipfile
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.incident_export import build_incident_archive


def test_incident_archive_is_content_free_and_reproducible():
    secret = "PRIVATE_PROMPT_DO_NOT_EXPORT"
    run = SimpleNamespace(
        run_id="a" * 32, status="stopped", started_at=datetime(2026, 1, 1),
        terminal_at=None, last_seq=9, durable_seq=8, context_revision=3,
        terminal_reason_code="process_restarted", ttft_last_ms=120.5,
        ttft_max_ms=250.0, prefill_tps_last=50.0,
        tool_latency_mean_ms=80.0, tool_latency_max_ms=200.0,
        compaction_last_ms=None, compaction_max_ms=None,
        sse_reconnects=2, compaction_failures=1,
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
        assert runs[0]["terminal_reason_code"] == "process_restarted"
        assert runs[0]["health_metrics"]["ttft_max_ms"] == 250.0
        assert summary["max_ttft_ms"] == 250.0
        assert summary["max_tool_latency_ms"] == 200.0
        assert summary["total_sse_reconnects"] == 2
        assert summary["total_compaction_failures"] == 1
        assert summary["max_durable_lag_events"] == 1
        assert summary["unknown_effect_count"] == 1
        assert [event["seq"] for event in fixture["events"]] == list(range(7))
        assert {event["run_id"] for event in fixture["events"]} == {"f" * 32}
        assert "context_snapshot" not in runs[0]
        assert "receipt" not in runs[0]


def test_incident_statuses_are_allowlisted():
    from src.incident_export import _status, _safe_metric, _RUN_STATUSES, _EFFECT_STATUSES

    assert _status("error", _RUN_STATUSES) == "error"
    assert _status("prompt text accidentally stored as status", _RUN_STATUSES) == "unrecognized"
    assert _status("no_retry", _EFFECT_STATUSES) == "no_retry"
    assert _safe_metric("PRIVATE_PROMPT_DO_NOT_EXPORT", 1000) is None
    assert _safe_metric(float("inf"), 1000) is None
    assert _safe_metric(True, 1000) is None
    assert _safe_metric(1200, 1000) is None


def test_sqlite_incident_export_selects_only_safe_json_scalars(tmp_path):
    from core.database import ChatRunState, ChatToolIntent, Session

    secret = "PRIVATE_PROMPT_DO_NOT_EXPORT"
    engine = create_engine(f"sqlite:///{tmp_path / 'incident.db'}")
    for table in (Session.__table__, ChatRunState.__table__, ChatToolIntent.__table__):
        table.create(engine)
    local = sessionmaker(bind=engine)
    with local.begin() as db:
        db.add(Session(id="safe", name="safe", endpoint_url="http://example.invalid/v1",
                       model="fixture-model", owner="alice"))
        db.flush()
        db.add(ChatRunState(
            run_id="a" * 32, session_id="safe", owner="alice", status="error",
            last_seq=12, durable_seq=11, context_revision=4,
            continuation={
                "prompt": secret,
                "terminal_reason": secret,
                "health_metrics": {
                    "ttft_max_ms": 420.5, "tool_latency_max_ms": 1200,
                    "sse_reconnects": 3, "compaction_failures": 1,
                    "prefill_tps_last": secret,
                },
            },
            context_snapshot={"messages": [secret]},
        ))
    with patch("src.incident_export.SessionLocal", local), \
         patch("src.incident_export.ReplayLog", side_effect=FileNotFoundError):
        owned = build_incident_archive("safe", "alice")
        foreign = build_incident_archive("safe", "bob")
    assert secret.encode() not in owned
    with zipfile.ZipFile(io.BytesIO(owned)) as archive:
        run = json.loads(archive.read("runs.json"))[0]
        summary = json.loads(archive.read("summary.json"))
        assert run["terminal_reason_code"] == "unrecognized"
        assert run["health_metrics"]["ttft_max_ms"] == 420.5
        assert run["health_metrics"]["prefill_tps_last"] is None
        assert summary["max_tool_latency_ms"] == 1200
        assert summary["total_sse_reconnects"] == 3
        assert summary["total_compaction_failures"] == 1
    with zipfile.ZipFile(io.BytesIO(foreign)) as archive:
        assert json.loads(archive.read("runs.json")) == []


def test_non_sqlite_incident_export_keeps_safe_core_without_json_extract():
    from core.database import ChatRunState

    class Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [SimpleNamespace(run_id="b" * 32, status="done", started_at=None,
                                    terminal_at=None, last_seq=1, durable_seq=1,
                                    context_revision=1)] if self.is_run else []

    class Db:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def query(self, *columns):
            assert all("json_extract" not in str(column) for column in columns)
            result = Query()
            result.is_run = columns[0].class_ is ChatRunState
            return result

        def close(self):
            pass

    with patch("src.incident_export.SessionLocal", return_value=Db()), \
         patch("src.incident_export.ReplayLog", side_effect=FileNotFoundError):
        output = build_incident_archive("safe", "alice")
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        assert json.loads(archive.read("manifest.json"))["health_metrics_available"] is False
        assert json.loads(archive.read("runs.json"))[0]["health_metrics"]["ttft_max_ms"] is None


def test_incident_menu_downloads_only_owner_scoped_archive():
    app = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    assert "getCurrentSessionId()" in app
    assert "/api/chat/incident/${encodeURIComponent(sessionId)}" in app
    assert "credentials: 'same-origin'" in app
    assert "link.download = `odysseus-incident-${sessionId}.zip`" in app
