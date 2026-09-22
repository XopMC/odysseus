"""Diagnostics routes — /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Dict, Any
from sqlalchemy import func

from fastapi import APIRouter, HTTPException, Form, Request

from services.youtube.youtube_handler import extract_youtube_id, extract_transcript_async
from core.constants import DEFAULT_HOST, DATA_DIR
from core.middleware import require_admin

logger = logging.getLogger(__name__)


def _slo_threshold(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _attach_process_slo(runtime: Dict[str, Any]) -> Dict[str, Any]:
    """Add content-free process alerts after event-loop lag is measured."""
    process = runtime.get("process") or {}
    slo = runtime.setdefault("slo", {"alerts": []})
    alerts = slo.setdefault("alerts", [])
    lag_limit = _slo_threshold("ODYSSEUS_SLO_EVENT_LOOP_LAG_MS", 200)
    rss_limit_mb = _slo_threshold("ODYSSEUS_SLO_PROCESS_RSS_MB", 4096)
    lag = float(process.get("event_loop_lag_ms") or 0)
    rss_mb = round(int(process.get("rss_bytes") or 0) / (1024 * 1024), 1)
    slo["event_loop_lag_limit_ms"] = lag_limit
    slo["process_rss_limit_mb"] = rss_limit_mb
    if lag > lag_limit:
        alerts.append({"code": "event_loop_lag", "observed": round(lag, 3),
                       "threshold": lag_limit, "unit": "ms"})
    if rss_mb > rss_limit_mb:
        alerts.append({"code": "process_rss_high", "observed": rss_mb,
                       "threshold": rss_limit_mb, "unit": "MiB"})
    return runtime


def _runtime_diagnostics() -> Dict[str, Any]:
    """Content-free long-run health counters for admin diagnostics."""
    from src.chat_replay_log import MAX_RUN_BYTES, MAX_TOTAL_BYTES

    replay_root = Path(DATA_DIR) / "chat-replay"
    files = [path for path in replay_root.iterdir() if path.is_file()] if replay_root.exists() else []
    total_bytes = sum(path.stat().st_size for path in files)
    per_run = {}
    for path in files:
        per_run[path.stem] = per_run.get(path.stem, 0) + path.stat().st_size
    largest_run_bytes = max(per_run.values(), default=0)

    rss_bytes = 0
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss_bytes = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass

    active_runs = active_subagents = durable_lag_max_events = 0
    child_queue_wait_max_ms = 0
    try:
        from core.database import ChatRunState, ChatSubagentRun, SessionLocal, utcnow_naive
        from src.subagent_runtime import ACTIVE_STATUSES
        db = SessionLocal()
        try:
            active_runs = db.query(ChatRunState).filter(ChatRunState.status == "running").count()
            durable_lag_max_events = db.query(
                func.max(ChatRunState.last_seq - ChatRunState.durable_seq)
            ).filter(ChatRunState.status == "running").scalar() or 0
            active_subagents = db.query(ChatSubagentRun).filter(
                ChatSubagentRun.status.in_(ACTIVE_STATUSES),
                ChatSubagentRun.removed.is_(False),
            ).count()
            child_times = db.query(
                ChatSubagentRun.created_at, ChatSubagentRun.started_at,
                ChatSubagentRun.status,
            ).filter(
                ChatSubagentRun.status.in_(("queued", "running")),
                ChatSubagentRun.removed.is_(False),
            ).all()
            now = utcnow_naive()
            child_queue_wait_max_ms = max((
                max(0, int(((row.started_at or now) - row.created_at).total_seconds() * 1000))
                for row in child_times if row.created_at
            ), default=0)
        finally:
            db.close()
    except Exception:
        logger.debug("runtime diagnostics DB counters unavailable", exc_info=True)

    db_path = Path(DATA_DIR) / "app.db"
    total_percent = round(total_bytes * 100 / MAX_TOTAL_BYTES, 3)
    largest_percent = round(largest_run_bytes * 100 / MAX_RUN_BYTES, 3)
    lag_limit = _slo_threshold("ODYSSEUS_SLO_DURABLE_LAG_EVENTS", 50)
    alerts = []
    try:
        from src.agent_runs import active_run_health_summary
        latency = active_run_health_summary()
    except Exception:
        latency = {"measured_runs": 0, "max_ttft_ms": 0, "min_prefill_tps": None,
                   "max_tool_latency_ms": 0, "compaction_failures": 0,
                   "max_compaction_ms": 0,
                   "sse_reconnects": 0}
        logger.debug("run latency diagnostics unavailable", exc_info=True)
    ttft_limit = _slo_threshold("ODYSSEUS_SLO_TTFT_MS", 60_000)
    tool_limit = _slo_threshold("ODYSSEUS_SLO_TOOL_LATENCY_MS", 120_000)
    prefill_min = _slo_threshold("ODYSSEUS_SLO_PREFILL_MIN_TPS", 10)
    child_queue_limit = _slo_threshold("ODYSSEUS_SLO_CHILD_QUEUE_WAIT_MS", 120_000)
    reconnect_limit = _slo_threshold("ODYSSEUS_SLO_SSE_RECONNECTS", 20)
    compaction_limit = _slo_threshold("ODYSSEUS_SLO_COMPACTION_MS", 120_000)
    if latency["max_ttft_ms"] > ttft_limit:
        alerts.append({"code": "model_ttft_high", "observed": latency["max_ttft_ms"],
                       "threshold": ttft_limit, "unit": "ms"})
    if latency["max_tool_latency_ms"] > tool_limit:
        alerts.append({"code": "tool_latency_high", "observed": latency["max_tool_latency_ms"],
                       "threshold": tool_limit, "unit": "ms"})
    if latency["min_prefill_tps"] is not None and latency["min_prefill_tps"] < prefill_min:
        alerts.append({"code": "prefill_slow", "observed": latency["min_prefill_tps"],
                       "threshold": prefill_min, "unit": "tokens_per_second"})
    if latency["compaction_failures"] > 0:
        alerts.append({"code": "compaction_failed", "observed": latency["compaction_failures"],
                       "threshold": 0, "unit": "failures"})
    if latency["max_compaction_ms"] > compaction_limit:
        alerts.append({"code": "compaction_slow", "observed": latency["max_compaction_ms"],
                       "threshold": compaction_limit, "unit": "ms"})
    if child_queue_wait_max_ms > child_queue_limit:
        alerts.append({"code": "child_queue_wait_high", "observed": child_queue_wait_max_ms,
                       "threshold": child_queue_limit, "unit": "ms"})
    if latency["sse_reconnects"] > reconnect_limit:
        alerts.append({"code": "sse_reconnects_high", "observed": latency["sse_reconnects"],
                       "threshold": reconnect_limit, "unit": "subscriptions"})
    if durable_lag_max_events > lag_limit:
        alerts.append({"code": "durable_cursor_lag", "observed": durable_lag_max_events,
                       "threshold": lag_limit, "unit": "events"})
    if max(total_percent, largest_percent) >= 90:
        alerts.append({"code": "replay_storage_critical", "observed": max(total_percent, largest_percent),
                       "threshold": 90, "unit": "percent"})
    return {
        "process": {"pid": os.getpid(), "rss_bytes": rss_bytes},
        "runs": {"active": active_runs, "active_subagents": active_subagents,
                 "durable_lag_max_events": durable_lag_max_events,
                 "child_queue_wait_max_ms": child_queue_wait_max_ms,
                 "latency": latency},
        "slo": {"durable_lag_limit_events": lag_limit,
                "ttft_limit_ms": ttft_limit, "tool_latency_limit_ms": tool_limit,
                "prefill_min_tps": prefill_min,
                "child_queue_wait_limit_ms": child_queue_limit,
                "sse_reconnect_limit": reconnect_limit,
                "compaction_limit_ms": compaction_limit,
                "alerts": alerts},
        "storage": {
            "database_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "replay_bytes": total_bytes,
            "replay_files": len(files),
            "replay_runs": len(per_run),
            "largest_replay_run_bytes": largest_run_bytes,
            "max_replay_run_bytes": MAX_RUN_BYTES,
            "max_replay_total_bytes": MAX_TOTAL_BYTES,
            "replay_total_percent": total_percent,
            "largest_run_percent": largest_percent,
            "replay_status": (
                "critical" if max(total_percent, largest_percent) >= 90
                else "warning" if max(total_percent, largest_percent) >= 70
                else "ok"
            ),
        },
    }


def setup_diagnostics_routes(
    rag_manager,
    rag_available: bool,
    research_handler,
    memory_vector=None,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])

    @router.get("/api/diagnostics/services")
    async def get_service_health(request: Request) -> Dict[str, Any]:
        """Consolidated degraded-state report for ChromaDB, SearXNG, email,
        ntfy, and provider endpoints. Non-intrusive probes — safe to poll."""
        require_admin(request)
        from src.service_health import collect_service_health
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0)
        loop_lag_ms = max(0.0, (asyncio.get_running_loop().time() - started) * 1000)
        result = await collect_service_health(rag_manager, memory_vector)
        # Replay storage can contain thousands of artifacts on a long chat.
        # Its stat scan and DB aggregates must not stall the SSE event loop.
        result["runtime"] = await asyncio.to_thread(_runtime_diagnostics)
        result["runtime"]["process"]["event_loop_lag_ms"] = round(loop_lag_ms, 3)
        _attach_process_slo(result["runtime"])
        return result

    @router.get("/api/diagnostics/logs")
    async def get_diagnostics_logs(request: Request, limit: int = 200) -> Dict[str, Any]:
        require_admin(request)
        limit = max(1, min(limit, 1000))
        try:
            log_file = os.path.join(DATA_DIR, "logs", "app.log")
            if not os.path.exists(log_file):
                return {"status": "success", "logs": []}

            # Safe tail read of the log file (max 5MB via rotation)
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            tail_lines = lines[-limit:] if len(lines) > limit else lines
            tail_lines = [line.rstrip('\r\n') for line in tail_lines]

            return {
                "status": "success",
                "logs": tail_lines
            }
        except Exception as e:
            logger.error(f"Diagnostics logs retrieval error: {e}")
            raise HTTPException(500, f"Failed to retrieve logs: {str(e)}")

    @router.get("/api/db/stats")
    async def get_database_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            from core.database import get_detailed_stats
            return get_detailed_stats()
        except Exception as e:
            logger.error(f"DB stats error: {e}")
            raise HTTPException(500, "Failed to retrieve database statistics")

    @router.get("/api/rag/stats")
    async def get_rag_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        if rag_available and rag_manager:
            return rag_manager.get_stats()
        return {"error": "RAG system not available"}

    @router.get("/api/test/youtube")
    async def test_youtube(request: Request, url: str) -> Dict[str, Any]:
        require_admin(request)
        try:
            video_id = extract_youtube_id(url)
            if not video_id:
                return {"error": "Invalid YouTube URL"}

            data = await extract_transcript_async(url, video_id)
            return {
                "video_id": video_id,
                "transcript_success": data.get("success", False),
                "transcript_length": len(data.get("transcript", "")) if data.get("success") else 0,
                "transcript_preview": (data.get("transcript", "")[:500] + "...")
                    if data.get("success") and len(data.get("transcript", "")) > 500
                    else data.get("transcript", ""),
                "error": data.get("error") if not data.get("success") else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @router.post("/api/test-research")
    async def test_research(request: Request, query: str = Form("What is machine learning?")) -> Dict[str, Any]:
        require_admin(request)
        try:
            endpoint = f"http://{DEFAULT_HOST}:8000/v1/chat/completions"
            model = "gpt-oss-120b"
            result = await research_handler.call_research_service(query, endpoint, model)
            return {
                "status": "success",
                "query": query,
                "result_preview": result[:200] + "..." if len(result) > 200 else result,
                "result_length": len(result),
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "query": query}

    return router
