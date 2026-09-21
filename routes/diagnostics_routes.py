"""Diagnostics routes — /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Form, Request

from services.youtube.youtube_handler import extract_youtube_id, extract_transcript_async
from core.constants import DEFAULT_HOST, DATA_DIR
from core.middleware import require_admin

logger = logging.getLogger(__name__)


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

    active_runs = active_subagents = 0
    try:
        from core.database import ChatRunState, ChatSubagentRun, SessionLocal
        from src.subagent_runtime import ACTIVE_STATUSES
        db = SessionLocal()
        try:
            active_runs = db.query(ChatRunState).filter(ChatRunState.status == "running").count()
            active_subagents = db.query(ChatSubagentRun).filter(
                ChatSubagentRun.status.in_(ACTIVE_STATUSES),
                ChatSubagentRun.removed.is_(False),
            ).count()
        finally:
            db.close()
    except Exception:
        logger.debug("runtime diagnostics DB counters unavailable", exc_info=True)

    db_path = Path(DATA_DIR) / "app.db"
    total_percent = round(total_bytes * 100 / MAX_TOTAL_BYTES, 3)
    largest_percent = round(largest_run_bytes * 100 / MAX_RUN_BYTES, 3)
    return {
        "process": {"pid": os.getpid(), "rss_bytes": rss_bytes},
        "runs": {"active": active_runs, "active_subagents": active_subagents},
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
        result["runtime"] = _runtime_diagnostics()
        result["runtime"]["process"]["event_loop_lag_ms"] = round(loop_lag_ms, 3)
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
