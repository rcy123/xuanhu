"""会话 SSE / Redis Stream 事件服务。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SessionNotFoundError, ValidationError
from app.core.redis import get_redis
from app.models.consult import ConsultSession
from app.schemas.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    EventAppendResult,
    SessionEvent,
    SupportedEventType,
)

logger = logging.getLogger("xuanhu.events")

EVENT_STREAM_PREFIX = "xuanhu:events:"
EVENT_DEDUPE_PREFIX = "xuanhu:events:dedupe:"
EVENT_STREAM_MAXLEN = 1000
EVENT_DEDUPE_TTL_SECONDS = 86_400
DEFAULT_READ_COUNT = 100

_FORBIDDEN_PAYLOAD_KEYS = {
    "api_key",
    "apiKey",
    "authorization",
    "password",
    "secret",
    "token",
    "prompt",
    "raw_prompt",
    "raw_response",
    "full_model_response",
}


def session_event_stream_key(session_id: str) -> str:
    """构造会话事件 Redis Stream key。"""
    return f"{EVENT_STREAM_PREFIX}{session_id}"


def session_event_dedupe_key(session_id: str) -> str:
    """Build the Redis hash used by the atomic outbox publication script."""
    return f"{EVENT_DEDUPE_PREFIX}{session_id}"


def session_event_dedupe_order_key(session_id: str) -> str:
    """Build the Redis list that orders retained outbox dedupe markers."""
    return f"{EVENT_DEDUPE_PREFIX}{session_id}:order"


_APPEND_ONCE_LUA = """
local existing = redis.call('HGET', KEYS[2], ARGV[1])
local stream_id = existing
local created = 0
if not existing then
  stream_id = redis.call(
    'XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*',
    'event_type', ARGV[4], 'payload', ARGV[5]
  )
  redis.call('HSET', KEYS[2], ARGV[1], stream_id)
  redis.call('RPUSH', KEYS[3], ARGV[1])
  created = 1
elseif not redis.call('LPOS', KEYS[3], ARGV[1]) then
  -- During a rolling upgrade an existing hash marker may not yet have an
  -- order entry. Index it without publishing a duplicate event.
  redis.call('RPUSH', KEYS[3], ARGV[1])
end

local cap = tonumber(ARGV[2])
local overflow = redis.call('LLEN', KEYS[3]) - cap
for _ = 1, overflow do
  local evicted = redis.call('LPOP', KEYS[3])
  if evicted then
    redis.call('HDEL', KEYS[2], evicted)
  end
end

-- Repair hashes created before the bounded order index existed.  This branch
-- is normally a no-op, but guarantees the hard cap during a rolling upgrade.
local hash_count = redis.call('HLEN', KEYS[2])
if hash_count > cap then
  local retained = {}
  for _, marker in ipairs(redis.call('LRANGE', KEYS[3], 0, -1)) do
    retained[marker] = true
  end
  for _, marker in ipairs(redis.call('HKEYS', KEYS[2])) do
    if hash_count <= cap then
      break
    end
    if not retained[marker] then
      redis.call('HDEL', KEYS[2], marker)
      hash_count = hash_count - 1
    end
  end
end

redis.call('EXPIRE', KEYS[2], ARGV[3])
redis.call('EXPIRE', KEYS[3], ARGV[3])
return {stream_id, created}
"""


def _now_iso() -> str:
    """返回 UTC ISO 时间字符串。"""
    return datetime.now(UTC).isoformat()


def _parse_stream_id(event_id: str) -> tuple[int, int] | None:
    """解析 Redis Stream ID。"""
    try:
        left, right = event_id.split("-", 1)
        return int(left), int(right)
    except (TypeError, ValueError):
        return None


def _stream_id_lt(left: str, right: str) -> bool:
    """比较 Redis Stream ID：left < right。"""
    left_parts = _parse_stream_id(left)
    right_parts = _parse_stream_id(right)
    if left_parts is None or right_parts is None:
        return False
    return left_parts < right_parts


def _assert_safe_payload(payload: dict[str, Any]) -> None:
    """阻止明显敏感字段进入 Redis Stream payload。"""

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key)
                key_lower = key_text.lower()
                if key_text in _FORBIDDEN_PAYLOAD_KEYS or key_lower in _FORBIDDEN_PAYLOAD_KEYS:
                    raise ValidationError(
                        message="事件 payload 包含禁止字段",
                        detail=f"payload 字段 {key_text} 不允许写入事件流",
                        retryable=False,
                    )
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)


def _validate_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    """校验事件 payload 的 P3-3 契约。"""
    _assert_safe_payload(payload)
    if event_type == "review.required":
        if "modified_formula" not in payload:
            raise ValidationError(
                message="review.required 必须包含 modified_formula",
                detail="review.required payload 缺少 modified_formula 字段",
                retryable=False,
            )
        if "base_formula" in payload:
            raise ValidationError(
                message="review.required 不允许使用 base_formula",
                detail="review.required payload 应使用 modified_formula，而不是 base_formula",
                retryable=False,
            )


class EventService:
    """会话事件写入、读取和 SSE 格式化服务。"""

    def __init__(
        self,
        redis: Redis | None = None,
        *,
        dedupe_ttl_seconds: int = EVENT_DEDUPE_TTL_SECONDS,
    ) -> None:
        if dedupe_ttl_seconds < 1:
            raise ValueError("dedupe_ttl_seconds must be positive")
        self._redis = redis
        self._dedupe_ttl_seconds = dedupe_ttl_seconds

    async def _get_redis(self) -> Redis:
        if self._redis is not None:
            return self._redis
        self._redis = await get_redis()
        return self._redis

    async def ensure_session_exists(self, db: AsyncSession, session_id: str) -> ConsultSession:
        """校验会话存在；terminated 会话允许连接 stream。"""
        try:
            sid = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 格式非法",
                retryable=False,
            ) from exc

        result = await db.execute(select(ConsultSession).where(ConsultSession.id == sid))
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 在数据库中未找到",
                retryable=False,
            )
        return session

    async def append_session_event(
        self,
        session_id: str,
        event_type: SupportedEventType,
        payload: dict[str, Any],
        *,
        maxlen: int = EVENT_STREAM_MAXLEN,
    ) -> EventAppendResult:
        """写入一条会话事件到 Redis Stream。"""
        payload = dict(payload)
        payload.setdefault("session_id", session_id)
        payload.setdefault("timestamp", _now_iso())
        payload["schema_version"] = SESSION_EVENT_SCHEMA_VERSION
        _validate_event_payload(event_type, payload)

        redis = await self._get_redis()
        key = session_event_stream_key(session_id)
        event_id = await redis.xadd(
            key,
            {
                "event_type": event_type,
                "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
            maxlen=maxlen,
            approximate=True,
        )
        return EventAppendResult(event_id=str(event_id), stream_key=key)

    async def append_session_event_once(
        self,
        session_id: str,
        event_type: SupportedEventType,
        payload: dict[str, Any],
        *,
        dedupe_id: str,
        maxlen: int = EVENT_STREAM_MAXLEN,
    ) -> EventAppendResult:
        """Atomically append one event for a durable outbox delivery token.

        Redis executes the Lua script as one operation: either the stream row and
        dedupe marker both exist, or neither does. While the marker remains in the
        bounded retention window, re-delivery after a process crash returns the
        original stream id without appending again. The marker hash is capped at
        the same target length as the Stream and both marker keys use a deliberately
        refreshed idle TTL. ``dedupe_id`` is an opaque internal reference and must
        not contain payload text.
        """
        if not dedupe_id or len(dedupe_id) > 200:
            raise ValueError("dedupe_id must contain 1..200 characters")
        if maxlen < 1:
            raise ValueError("maxlen must be positive")
        payload = dict(payload)
        payload.setdefault("session_id", session_id)
        payload.setdefault("timestamp", _now_iso())
        payload["schema_version"] = SESSION_EVENT_SCHEMA_VERSION
        _validate_event_payload(event_type, payload)

        redis = await self._get_redis()
        stream_key = session_event_stream_key(session_id)
        dedupe_key = session_event_dedupe_key(session_id)
        dedupe_order_key = session_event_dedupe_order_key(session_id)
        encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = await cast(
            Awaitable[object],
            redis.eval(
                _APPEND_ONCE_LUA,
                3,
                stream_key,
                dedupe_key,
                dedupe_order_key,
                dedupe_id,
                str(maxlen),
                str(self._dedupe_ttl_seconds),
                event_type,
                encoded_payload,
            ),
        )
        if not isinstance(result, list | tuple) or len(result) != 2:
            raise RuntimeError("unexpected Redis append-once result")
        return EventAppendResult(
            event_id=str(result[0]),
            stream_key=stream_key,
            deduplicated=int(result[1]) == 0,
        )

    async def read_events_after(
        self,
        session_id: str,
        last_event_id: str | None,
        *,
        count: int = DEFAULT_READ_COUNT,
    ) -> tuple[list[SessionEvent], bool]:
        """读取 last_event_id 之后的事件。

        Returns:
            (events, needs_resync)
        """
        redis = await self._get_redis()
        key = session_event_stream_key(session_id)

        start_id = last_event_id or "0-0"
        if last_event_id and await self._needs_resync(redis, key, last_event_id):
            return [], True

        rows = await redis.xread({key: start_id}, count=count)
        return self._decode_xread(cast(list[tuple[str, list[tuple[str, dict[str, str]]]]], rows)), False

    async def wait_for_events(
        self,
        session_id: str,
        last_event_id: str,
        *,
        count: int = DEFAULT_READ_COUNT,
        block_ms: int = 30_000,
    ) -> list[SessionEvent]:
        """阻塞等待新事件，超时返回空列表。"""
        redis = await self._get_redis()
        key = session_event_stream_key(session_id)
        rows = await redis.xread({key: last_event_id}, count=count, block=block_ms)
        return self._decode_xread(cast(list[tuple[str, list[tuple[str, dict[str, str]]]]], rows))

    async def iter_sse(
        self,
        session_id: str,
        *,
        last_event_id: str | None,
        heartbeat_interval_seconds: float,
    ) -> AsyncIterator[str]:
        """生成 SSE 文本流。"""
        current_id = last_event_id or "0-0"
        events, needs_resync = await self.read_events_after(session_id, last_event_id)
        if needs_resync:
            yield self.format_sse_event(self.resync_event(session_id, "last_event_id_not_found"))
            current_id = await self.latest_event_id(session_id)
        else:
            for event in events:
                current_id = event.event_id
                yield self.format_sse_event(event)

        heartbeat_ms = max(int(heartbeat_interval_seconds * 1000), 100)
        while True:
            events = await self.wait_for_events(session_id, current_id, block_ms=heartbeat_ms)
            if events:
                for event in events:
                    current_id = event.event_id
                    yield self.format_sse_event(event)
            else:
                yield self.format_sse_event(self.heartbeat_event())
                await asyncio.sleep(0)

    def heartbeat_event(self) -> SessionEvent:
        """构造本地 heartbeat 事件。"""
        return SessionEvent(
            event_id=f"heartbeat-{uuid.uuid4()}",
            event_type="heartbeat",
            payload={
                "timestamp": _now_iso(),
                "schema_version": SESSION_EVENT_SCHEMA_VERSION,
            },
        )

    def resync_event(self, session_id: str, reason: str) -> SessionEvent:
        """构造本地 resync 事件。"""
        return SessionEvent(
            event_id=f"resync-{uuid.uuid4()}",
            event_type="resync",
            payload={
                "session_id": session_id,
                "reason": reason,
                "timestamp": _now_iso(),
                "schema_version": SESSION_EVENT_SCHEMA_VERSION,
            },
        )

    def format_sse_event(self, event: SessionEvent) -> str:
        """格式化为标准 SSE 文本。"""
        payload_json = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event.event_type}\nid: {event.event_id}\ndata: {payload_json}\n\n"

    async def latest_event_id(self, session_id: str) -> str:
        """返回当前 Stream 最新事件 ID；无事件时返回 0-0。"""
        redis = await self._get_redis()
        key = session_event_stream_key(session_id)
        rows = await redis.xrange(key, count=EVENT_STREAM_MAXLEN)
        if not rows:
            return "0-0"
        return str(rows[-1][0])

    async def _needs_resync(self, redis: Redis, key: str, last_event_id: str) -> bool:
        """判断 last_event_id 是否已不可用。"""
        if last_event_id == "0-0":
            return False
        if _parse_stream_id(last_event_id) is None:
            return True

        first_entries = await redis.xrange(key, count=1)
        if not first_entries:
            return True

        first_id = str(first_entries[0][0])
        if _stream_id_lt(last_event_id, first_id):
            return True

        exact = await redis.xrange(key, min=last_event_id, max=last_event_id, count=1)
        return not bool(exact)

    def _decode_xread(self, rows: list[tuple[str, list[tuple[str, dict[str, str]]]]]) -> list[SessionEvent]:
        """解码 Redis xread 返回值。"""
        events: list[SessionEvent] = []
        for _stream, entries in rows:
            for event_id, fields in entries:
                event_type = fields.get("event_type")
                payload_raw = fields.get("payload", "{}")
                payload = json.loads(payload_raw)
                events.append(
                    SessionEvent(
                        event_id=str(event_id),
                        event_type=cast(SupportedEventType, event_type),
                        payload=payload,
                    )
                )
        return events
