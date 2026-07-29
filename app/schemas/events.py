"""SSE 事件流 Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SESSION_EVENT_SCHEMA_VERSION: Literal["session-event.v2"] = "session-event.v2"

SupportedEventType = Literal[
    "stage.changed",
    "message.created",
    "agent.started",
    "agent.finished",
    "agent.failed",
    "review.required",
    "safety.blocked",
    "session.blocked",
    "session.done",
    "session.terminated",
    "doctor.reviewed",
    "heartbeat",
    "resync",
]

SUPPORTED_EVENT_TYPES: tuple[str, ...] = (
    "stage.changed",
    "message.created",
    "agent.started",
    "agent.finished",
    "agent.failed",
    "review.required",
    "safety.blocked",
    "session.blocked",
    "session.done",
    "session.terminated",
    "doctor.reviewed",
    "heartbeat",
    "resync",
)


class SessionEvent(BaseModel):
    """Redis Stream 中的一条会话事件。"""

    event_id: str
    event_type: SupportedEventType
    payload: dict[str, Any] = Field(default_factory=dict)


class EventAppendResult(BaseModel):
    """事件写入结果。"""

    event_id: str
    stream_key: str
    deduplicated: bool = False


class HeartbeatPayload(BaseModel):
    """心跳 payload。"""

    timestamp: datetime


__all__ = [
    "SESSION_EVENT_SCHEMA_VERSION",
    "SUPPORTED_EVENT_TYPES",
    "EventAppendResult",
    "HeartbeatPayload",
    "ResyncPayload",
    "SessionEvent",
    "SupportedEventType",
]


class ResyncPayload(BaseModel):
    """要求前端全量同步的 payload。"""

    session_id: str
    reason: str
    timestamp: datetime
