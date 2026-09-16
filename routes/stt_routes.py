# routes/stt_routes.py
"""STT API routes — multi-provider (local Whisper, API endpoint, browser)."""

from fastapi import APIRouter, HTTPException, UploadFile, File, Request
import logging
import asyncio

from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES
from src.auth_helpers import get_current_user
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])
    limiter = RateLimiter(max_requests=30, window_seconds=60)
    semaphore = asyncio.Semaphore(2)

    @router.get("/stats")
    async def get_stt_stats():
        """Get STT service statistics"""
        try:
            return stt_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get STT stats: {e}")
            raise HTTPException(status_code=500, detail="STT statistics unavailable")

    @router.post("/transcribe")
    async def transcribe_audio(request: Request, file: UploadFile = File(...)):
        """Transcribe uploaded audio file to text"""
        owner = get_current_user(request)
        key = owner or (request.client.host if request.client else "unknown")
        if not limiter.check(key):
            raise HTTPException(status_code=429, detail={"message": "STT rate limit exceeded"})
        try:
            if not stt_service.available:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "STT service not available or set to browser mode"}
                )

            audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
            if not audio_bytes:
                raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

            async with semaphore:
                text = await asyncio.to_thread(stt_service.transcribe, audio_bytes)
            if text is None:
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Transcription failed"}
                )

            return {"text": text}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": "Transcription failed"}
            )

    return router
