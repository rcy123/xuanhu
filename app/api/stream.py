"""SSE 事件流 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.services.events import EventService

router = APIRouter(prefix="/api/v1/consult", tags=["events"])


@router.get("/sessions/{session_id}/stream")
async def stream_session_events(
    session_id: str,
    last_event_id: str | None = Query(default=None),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """连接会话 SSE 事件流。"""
    service = EventService()

    factory = get_session_factory()
    async with factory() as db:
        await service.ensure_session_exists(db, session_id)

    effective_last_event_id = last_event_id or last_event_id_header
    settings = get_settings()
    return StreamingResponse(
        service.iter_sse(
            session_id,
            last_event_id=effective_last_event_id,
            heartbeat_interval_seconds=settings.sse_heartbeat_interval_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
