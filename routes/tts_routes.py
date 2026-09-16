# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser).
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
import logging
import asyncio

from src.auth_helpers import get_current_user
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    format: str = "audio"  # "audio" or "base64"

def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])
    limiter = RateLimiter(max_requests=30, window_seconds=60)
    semaphore = asyncio.Semaphore(2)

    def _owner_key(request: Request) -> str:
        return get_current_user(request) or (request.client.host if request.client else "unknown")

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            return tts_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail="TTS statistics unavailable")

    @router.post("/synthesize")
    async def synthesize_speech(payload: TTSRequest, http_request: Request):
        """Synthesize speech from text"""
        if not limiter.check(_owner_key(http_request)):
            raise HTTPException(status_code=429, detail={"message": "TTS rate limit exceeded"})
        try:
            if not tts_service.available:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "TTS service not available"}
                )
            
            async with semaphore:
                if payload.format == "base64":
                    audio_b64 = await asyncio.to_thread(tts_service.synthesize_to_base64, payload.text)
                else:
                    audio_b64 = None
                if not audio_b64:
                    if payload.format == "base64":
                        raise HTTPException(status_code=500, detail={"message": "Synthesis failed"})
                if payload.format == "base64":
                    return {"audio": audio_b64}

                audio_data = await asyncio.to_thread(tts_service.synthesize, payload.text)
                if not audio_data:
                    raise HTTPException(status_code=500, detail={"message": "Synthesis failed"})
                
                # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
                is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
                mime = "audio/mpeg" if is_mp3 else "audio/wav"
                return Response(
                    content=audio_data,
                    media_type=mime,
                    headers={
                        "Content-Disposition": "inline; filename=speech.mp3" if "mpeg" in mime else "inline; filename=speech.wav"
                    }
                )
        
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": "Synthesis failed"}
            )

    @router.post("/clear-cache")
    async def clear_tts_cache(request: Request):
        """Clear TTS cache"""
        user = get_current_user(request)
        auth = getattr(request.app.state, "auth_manager", None)
        if not user or not auth or not auth.is_admin(user):
            raise HTTPException(status_code=403, detail="Administrator permission required")
        try:
            await asyncio.to_thread(tts_service.clear_cache)
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail="TTS cache could not be cleared")

    return router
