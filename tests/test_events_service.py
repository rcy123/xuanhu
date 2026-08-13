"""P3-3 Redis Stream 事件服务测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.exceptions import ValidationError
from app.schemas.events import SESSION_EVENT_SCHEMA_VERSION
from app.services.events import EventService, session_event_stream_key


class FakeRedis:
    """最小 Redis Stream fake。"""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.next_id = 1
        self.last_xadd: dict[str, Any] | None = None

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        event_id = f"{self.next_id}-0"
        self.next_id += 1
        self.streams.setdefault(key, []).append((event_id, fields))
        if maxlen is not None and len(self.streams[key]) > maxlen:
            self.streams[key] = self.streams[key][-maxlen:]
        self.last_xadd = {
            "key": key,
            "fields": fields,
            "maxlen": maxlen,
            "approximate": approximate,
        }
        return event_id

    async def xrange(
        self,
        key: str,
        min: str = "-",
        max: str = "+",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        rows = [
            row
            for row in self.streams.get(key, [])
            if self._gte(row[0], min) and self._lte(row[0], max)
        ]
        return rows[:count] if count is not None else rows

    async def xread(
        self,
        streams: dict[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del block
        result: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        for key, last_id in streams.items():
            rows = [row for row in self.streams.get(key, []) if self._gt(row[0], last_id)]
            if count is not None:
                rows = rows[:count]
            if rows:
                result.append((key, rows))
        return result

    def _parts(self, event_id: str) -> tuple[int, int]:
        if event_id == "-":
            return -1, -1
        if event_id == "+":
            return 10**30, 10**30
        left, right = event_id.split("-", 1)
        return int(left), int(right)

    def _gt(self, left: str, right: str) -> bool:
        return self._parts(left) > self._parts(right)

    def _gte(self, left: str, right: str) -> bool:
        return self._parts(left) >= self._parts(right)

    def _lte(self, left: str, right: str) -> bool:
        return self._parts(left) <= self._parts(right)


@pytest.mark.asyncio
async def test_append_session_event_writes_stream_entry() -> None:
    """事件写入 Redis Stream，包含 timestamp 和 session_id。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]

    result = await service.append_session_event(
        "sid-001",
        "stage.changed",
        {"from_stage": "inquiry", "to_stage": "sufficiency", "state_version": 2},
    )

    assert result.event_id == "1-0"
    assert result.stream_key == "xuanhu:events:sid-001"
    assert redis.last_xadd is not None
    assert redis.last_xadd["key"] == session_event_stream_key("sid-001")
    assert redis.last_xadd["maxlen"] == 1000
    assert redis.last_xadd["approximate"] is True

    payload = json.loads(redis.last_xadd["fields"]["payload"])
    assert payload["session_id"] == "sid-001"
    assert payload["from_stage"] == "inquiry"
    assert payload["schema_version"] == SESSION_EVENT_SCHEMA_VERSION
    assert "timestamp" in payload


@pytest.mark.asyncio
async def test_append_session_event_overwrites_untrusted_schema_version() -> None:
    """Producer, rather than a caller payload, owns the public event version."""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]

    await service.append_session_event(
        "sid-schema",
        "agent.started",
        {"agent_name": "intake", "schema_version": "attacker.v999"},
    )

    assert redis.last_xadd is not None
    payload = json.loads(redis.last_xadd["fields"]["payload"])
    assert payload["schema_version"] == SESSION_EVENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_append_agent_progress_writes_reasoning_stage() -> None:
    """agent.progress（开方阶段进度）可写入 Redis Stream，含 stage/label 字段。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]

    result = await service.append_session_event(
        "sid-progress",
        "agent.progress",
        {"stage": "syndrome", "label": "正在辨证…", "agent_name": "reasoning_subgraph"},
    )

    assert result.event_id == "1-0"
    assert redis.last_xadd is not None
    payload = json.loads(redis.last_xadd["fields"]["payload"])
    assert payload["stage"] == "syndrome"
    assert payload["label"] == "正在辨证…"
    assert payload["agent_name"] == "reasoning_subgraph"
    assert payload["session_id"] == "sid-progress"
    assert payload["schema_version"] == SESSION_EVENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_read_events_after_returns_only_newer_events() -> None:
    """last_event_id 之后的事件可补发。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]
    await service.append_session_event("sid-002", "stage.changed", {"to_stage": "inquiry"})
    second = await service.append_session_event(
        "sid-002",
        "message.created",
        {"message_id": "msg-001", "role": "doctor"},
    )

    events, needs_resync = await service.read_events_after("sid-002", "1-0")

    assert needs_resync is False
    assert [event.event_id for event in events] == [second.event_id]
    assert events[0].event_type == "message.created"
    assert events[0].payload["message_id"] == "msg-001"


@pytest.mark.asyncio
async def test_read_events_after_old_event_id_returns_resync() -> None:
    """last_event_id 早于 Stream 首条事件时需要 resync。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]
    await service.append_session_event("sid-003", "stage.changed", {"to_stage": "inquiry"})

    events, needs_resync = await service.read_events_after("sid-003", "0-1")

    assert events == []
    assert needs_resync is True


@pytest.mark.asyncio
async def test_read_events_after_missing_event_id_returns_resync() -> None:
    """last_event_id 位于现有范围内但不存在时需要 resync。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]
    await service.append_session_event("sid-004", "stage.changed", {"to_stage": "inquiry"})
    await service.append_session_event("sid-004", "message.created", {"message_id": "msg-001"})

    events, needs_resync = await service.read_events_after("sid-004", "9-0")

    assert events == []
    assert needs_resync is True


@pytest.mark.asyncio
async def test_iter_sse_fresh_connect_resyncs_without_replaying_history() -> None:
    """全新连接（无游标）不重放历史事件，直接发 resync 并跳至最新。

    历史重放会让客户端对每条 message.created/agent.finished 都触发一次全量
    refetch（打开会话即请求风暴），且重放的是过期瞬态；修复后无游标连接只发
    resync，客户端以 GET 权威读模型全量同步。
    """
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]
    await service.append_session_event("sid-010", "message.created", {"message_id": "msg-001"})
    await service.append_session_event("sid-010", "agent.finished", {"agent_name": "intake"})

    stream = service.iter_sse(
        "sid-010",
        last_event_id=None,
        heartbeat_interval_seconds=0.1,
    )
    first = await anext(stream)
    assert first.startswith("event: resync\n")
    assert "fresh_connect_full_sync" in first
    # 历史事件不得重放：紧跟 resync 的应是 heartbeat（无新事件），而非 message.created。
    second = await anext(stream)
    assert second.startswith("event: heartbeat\n")
    await stream.aclose()


@pytest.mark.asyncio
async def test_iter_sse_with_cursor_still_replays_events_after_cursor() -> None:
    """带游标的连接仍按既有语义补发游标之后的事件（断线重连增量补发）。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]
    first = await service.append_session_event(
        "sid-011",
        "message.created",
        {"message_id": "msg-001"},
    )
    await service.append_session_event("sid-011", "agent.finished", {"agent_name": "intake"})

    stream = service.iter_sse(
        "sid-011",
        last_event_id=first.event_id,
        heartbeat_interval_seconds=0.1,
    )
    second = await anext(stream)
    assert second.startswith("event: agent.finished\n")
    assert "intake" in second
    await stream.aclose()



def test_format_sse_event_includes_event_id_and_data() -> None:
    """SSE 格式包含 event/id/data 三段。"""
    service = EventService()
    event = service.resync_event("sid-005", "trimmed")

    text = service.format_sse_event(event)

    assert text.startswith("event: resync\n")
    assert "\nid: " in text
    assert '\ndata: {"session_id":"sid-005","reason":"trimmed"' in text
    assert text.endswith("\n\n")


def test_heartbeat_event_has_timestamp() -> None:
    """heartbeat 事件包含 timestamp。"""
    service = EventService()
    event = service.heartbeat_event()

    assert event.event_type == "heartbeat"
    assert event.event_id.startswith("heartbeat-")
    assert "timestamp" in event.payload
    assert event.payload["schema_version"] == SESSION_EVENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_review_required_requires_modified_formula() -> None:
    """review.required 必须使用 modified_formula 字段。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]

    result = await service.append_session_event(
        "sid-006",
        "review.required",
        {"modified_formula": {"name": "测试方"}, "safety_review": {"passed": True}},
    )

    assert result.event_id == "1-0"


@pytest.mark.asyncio
async def test_review_required_rejects_base_formula() -> None:
    """review.required 不允许用 base_formula 替代 modified_formula。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await service.append_session_event(
            "sid-007",
            "review.required",
            {"base_formula": {"name": "错误字段"}, "safety_review": {"passed": True}},
        )


@pytest.mark.asyncio
async def test_payload_rejects_sensitive_keys() -> None:
    """事件 payload 不允许明显敏感字段。"""
    redis = FakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await service.append_session_event(
            "sid-008",
            "agent.failed",
            {"error_code": "X", "api_key": "sk-nope"},
        )
