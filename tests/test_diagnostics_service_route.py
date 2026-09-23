"""Route-level regression tests for GET /api/diagnostics/services.

The reviewer asked for explicit coverage of unauthenticated / non-admin / admin
access to this admin diagnostics route, beyond the unit tests for the collector.

These need a real FastAPI + TestClient (the conftest only stubs FastAPI when it
is *not* installed). When the full app deps aren't present we skip rather than
fail, so the suite stays green in minimal environments; CI installs
requirements, so the tests run there.
"""
import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

from fastapi import FastAPI, HTTPException, Request
from starlette.testclient import TestClient

# Importing the route module pulls a few app deps; skip cleanly if unavailable.
diag = pytest.importorskip("routes.diagnostics_routes")


def _client_with_admin_gate(monkeypatch, gate):
    """Mount the diagnostics router with `require_admin` and the collector
    patched (via monkeypatch so the module globals are restored afterwards),
    and return a TestClient. `gate` plays the role of require_admin."""
    import src.service_health as sh

    async def _fake_collect(_rag, _mem):
        return {"overall": "ok", "services": [], "timestamp": "t"}

    # monkeypatch.setattr restores these after the test — a plain assignment
    # would leak the fakes into every later test in the session.
    monkeypatch.setattr(diag, "require_admin", gate)
    monkeypatch.setattr(sh, "collect_service_health", _fake_collect)

    app = FastAPI()
    app.include_router(diag.setup_diagnostics_routes(
        rag_manager=None, rag_available=False, research_handler=None,
        memory_vector=None))
    return TestClient(app, raise_server_exceptions=False)


def test_unauthenticated_is_rejected(monkeypatch):
    def gate(_request: Request):
        raise HTTPException(401, "Not authenticated")
    client = _client_with_admin_gate(monkeypatch, gate)
    r = client.get("/api/diagnostics/services")
    assert r.status_code == 401


def test_non_admin_is_forbidden(monkeypatch):
    def gate(_request: Request):
        raise HTTPException(403, "Admin only")
    client = _client_with_admin_gate(monkeypatch, gate)
    r = client.get("/api/diagnostics/services")
    assert r.status_code == 403


def test_admin_gets_report(monkeypatch):
    def gate(_request: Request):
        return None  # admin allowed
    client = _client_with_admin_gate(monkeypatch, gate)
    r = client.get("/api/diagnostics/services")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"overall", "services", "timestamp", "runtime"}
    assert body["overall"] == "ok"
    assert set(body["runtime"]) == {"process", "runs", "storage", "slo"}
    assert body["runtime"]["storage"]["max_replay_run_bytes"] > 0
    assert body["runtime"]["storage"]["max_replay_total_bytes"] > 0
    assert body["runtime"]["storage"]["replay_status"] in {"ok", "warning", "critical"}
    assert body["runtime"]["process"]["event_loop_lag_ms"] >= 0
    assert body["runtime"]["runs"]["durable_lag_max_events"] >= 0
    assert body["runtime"]["slo"]["durable_lag_limit_events"] > 0
    assert isinstance(body["runtime"]["slo"]["alerts"], list)


def test_slo_threshold_rejects_invalid_or_zero_values(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_SLO_DURABLE_LAG_EVENTS", "invalid")
    assert diag._slo_threshold("ODYSSEUS_SLO_DURABLE_LAG_EVENTS", 50) == 50
    monkeypatch.setenv("ODYSSEUS_SLO_DURABLE_LAG_EVENTS", "0")
    assert diag._slo_threshold("ODYSSEUS_SLO_DURABLE_LAG_EVENTS", 50) == 1


def test_process_slo_reports_lag_and_memory_without_chat_content(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_SLO_EVENT_LOOP_LAG_MS", "100")
    monkeypatch.setenv("ODYSSEUS_SLO_PROCESS_RSS_MB", "100")
    runtime = {"process": {"event_loop_lag_ms": 101, "rss_bytes": 101 * 1024 * 1024},
               "slo": {"alerts": []}}
    diag._attach_process_slo(runtime)
    assert [item["code"] for item in runtime["slo"]["alerts"]] == [
        "event_loop_lag", "process_rss_high",
    ]
    assert "chat" not in str(runtime).lower()


def test_durable_health_reads_only_bounded_numeric_scalars(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Session, ChatRunState

    secret = "PRIVATE_PROMPT_DO_NOT_REPORT"
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}")
    for table in (Session.__table__, ChatRunState.__table__):
        table.create(engine)
    local = sessionmaker(bind=engine)
    with local.begin() as db:
        db.add(Session(id="health", name="health", endpoint_url="http://example.invalid/v1",
                       model="fixture-model", owner="alice"))
        db.flush()
        db.add(ChatRunState(
            run_id="a" * 32, session_id="health", owner="alice", status="running",
            continuation={"prompt": secret, "health_metrics": {
                "ttft_max_ms": 250.5, "tool_latency_max_ms": 1500,
                "prefill_tps_last": secret, "compaction_failures": 1,
                "compaction_max_ms": 3000, "sse_reconnects": 2,
            }},
            context_snapshot={"messages": [secret]},
        ))
    with local() as db:
        result = diag._durable_run_health_summary(db)
    assert result == {
        "measured_runs": 1, "max_ttft_ms": 250.5,
        "max_tool_latency_ms": 1500, "min_prefill_tps": None,
        "compaction_failures": 1, "max_compaction_ms": 3000,
        "sse_reconnects": 2,
    }
    assert secret not in str(result)
    assert diag._durable_health_number(secret, 1000) is None
    assert diag._durable_health_number(float("inf"), 1000) is None
    assert diag._durable_health_number(True, 1000) is None


def test_durable_health_json_projection_compiles_for_postgresql():
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from core.database import ChatRunState

    statement = select(*(
        ChatRunState.continuation["health_metrics"][key].as_string()
        for key in diag._DURABLE_HEALTH_LIMITS
    )).where(ChatRunState.status == "running")
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "health_metrics" in compiled.params.values()
    assert "context_snapshot" not in sql
    assert "SELECT chat_run_states.continuation" not in sql


def test_runtime_slo_uses_durable_metrics_when_local_run_is_absent(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database
    from src import agent_runs

    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    for table in (database.Session.__table__, database.ChatRunState.__table__,
                  database.ChatSubagentRun.__table__):
        table.create(engine)
    local = sessionmaker(bind=engine)
    with local.begin() as db:
        db.add(database.Session(id="health", name="health", endpoint_url="http://example.invalid/v1",
                                model="fixture-model", owner="alice"))
        db.flush()
        db.add(database.ChatRunState(
            run_id="b" * 32, session_id="health", owner="alice", status="running",
            last_seq=7, durable_seq=7,
            continuation={"prompt": "SECRET_NOT_FOR_ADMIN", "health_metrics": {
                "ttft_max_ms": 70000, "tool_latency_max_ms": 130000,
                "prefill_tps_last": 5, "compaction_failures": 1,
                "compaction_max_ms": 130000, "sse_reconnects": 21,
            }},
        ))
    monkeypatch.setattr(database, "SessionLocal", local)
    monkeypatch.setattr(diag, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_runs, "active_run_health_summary", lambda: {
        "measured_runs": 0, "max_ttft_ms": 0, "min_prefill_tps": None,
        "max_tool_latency_ms": 0, "compaction_failures": 0,
        "max_compaction_ms": 0, "sse_reconnects": 0,
    })
    runtime = diag._runtime_diagnostics()
    assert runtime["runs"]["latency"]["measured_runs"] == 1
    assert {item["code"] for item in runtime["slo"]["alerts"]} >= {
        "model_ttft_high", "tool_latency_high", "prefill_slow",
        "compaction_failed", "compaction_slow", "sse_reconnects_high",
    }
    assert "SECRET_NOT_FOR_ADMIN" not in str(runtime)
