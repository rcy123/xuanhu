"""Durable PostgreSQL-outbox to Redis-Stream publisher.

The request transaction writes only PostgreSQL. This worker claims committed
rows later, maps an exact versioned internal event contract to privacy-minimal
client events, and acknowledges only after Redis confirms every mapped event.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, cast

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.agent_runtime.async_command import (
    ASYNC_COMMAND_ERROR_CODES,
    ASYNC_COMMAND_OPERATIONS,
)
from app.agent_runtime.repository import (
    OutboxErrorCode,
    OutboxHealth,
    OutboxMessage,
    OutboxRepository,
)
from app.schemas.events import SupportedEventType
from app.services.events import EventService

logger = logging.getLogger("xuanhu.outbox_publisher")


class OutboxMappingError(ValueError):
    """An internal event has no safe mapping for its exact schema version."""


@dataclass(frozen=True, slots=True)
class MappedSessionEvent:
    event_type: SupportedEventType
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    claimed: int = 0
    published: int = 0
    retried: int = 0
    dead_lettered: int = 0
    ownership_lost: int = 0


class SessionEventSink(Protocol):
    async def append_session_event_once(
        self,
        session_id: str,
        event_type: SupportedEventType,
        payload: dict[str, object],
        *,
        dedupe_id: str,
    ) -> object: ...


def _string(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


def _common(message: OutboxMessage) -> dict[str, object]:
    return {
        "source_event_id": str(message.event_id),
        "state_version": message.state_version,
    }


def _agent_event(message: OutboxMessage, event_type: SupportedEventType, name: str) -> MappedSessionEvent:
    if message.graph_run_id is None:
        raise OutboxMappingError("agent event requires a graph_run_id")
    return MappedSessionEvent(
        event_type,
        {
            **_common(message),
            "agent_name": name,
            "agent_run_id": str(message.graph_run_id),
        },
    )


# Exact version -> (expected command status, client event type).
_ASYNC_COMMAND_EVENT_VERSIONS: dict[str, tuple[str, str]] = {
    "async_command.queued.v1": ("queued", "command.queued"),
    "async_command.running.v1": ("running", "command.running"),
    "async_command.succeeded.v1": ("succeeded", "command.succeeded"),
    "async_command.failed.v1": ("failed", "command.failed"),
}


def _command_event(message: OutboxMessage, version: str) -> MappedSessionEvent:
    """Map an async-command lifecycle row from its fixed allowlist.

    Never copies arbitrary payload fields: only the bounded identifiers,
    operation/status, attempt, and (for failures) the sanitized error code are
    projected. Private request payload, result payload, exception text and
    digests never appear here.
    """
    expected_status, event_type = _ASYNC_COMMAND_EVENT_VERSIONS[version]
    payload = message.payload
    command_id = _string(payload, "command_id")
    operation = _string(payload, "operation")
    status = _string(payload, "status")
    attempt = payload.get("attempt")
    if not command_id or not operation or status != expected_status:
        raise OutboxMappingError(f"{version} has an invalid command contract")
    if operation not in ASYNC_COMMAND_OPERATIONS:
        # A corrupt/unknown operation must never be broadcast: fail closed.
        raise OutboxMappingError(f"{version} has an operation outside the allowlist")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise OutboxMappingError(f"{version} has an invalid attempt count")
    out: dict[str, object] = {
        "command_id": command_id,
        "operation": operation,
        "status": status,
        "attempt": attempt,
    }
    if version == "async_command.failed.v1":
        error_code = _string(payload, "error_code")
        if error_code is None:
            raise OutboxMappingError("async_command.failed.v1 requires a sanitized error_code")
        if error_code not in ASYNC_COMMAND_ERROR_CODES:
            raise OutboxMappingError(
                "async_command.failed.v1 error_code is outside the fixed allowlist"
            )
        out["error_code"] = error_code
    return MappedSessionEvent(cast(SupportedEventType, event_type), out)


def map_outbox_event(message: OutboxMessage) -> tuple[MappedSessionEvent, ...]:
    """Map exact internal versions without copying arbitrary payload fields.

    Every output is built from identifiers, enum-like decisions, versions and
    digests. In particular, free-form message/clinical text is never projected.
    Unknown versions fail closed so a future producer cannot silently leak a new
    payload shape to clients.
    """
    payload = message.payload
    common = _common(message)

    if message.event_type == "intake.message_created.v1":
        message_id = _string(payload, "message_id")
        role = _string(payload, "role")
        stage = _string(payload, "stage")
        if not message_id or not role or not stage:
            raise OutboxMappingError("intake.message_created.v1 missing required references")
        return (
            MappedSessionEvent(
                "message.created",
                {**common, "message_id": message_id, "role": role, "stage": stage},
            ),
            _agent_event(message, "agent.started", "intake"),
        )

    if message.event_type == "intake.command_completed.v1":
        events: list[MappedSessionEvent] = [_agent_event(message, "agent.finished", "intake")]
        question_message_id = _string(payload, "question_message_id")
        if question_message_id:
            events.append(
                MappedSessionEvent(
                    "message.created",
                    {
                        **common,
                        "message_id": question_message_id,
                        "role": "agent",
                        "stage": "inquiry",
                        "agent_name": "question_composer",
                    },
                )
            )
        completeness_disposition = _string(payload, "completeness_disposition")
        triage_decision = _string(payload, "triage_decision")
        # triage_decision 缺失（如澄清回复等无 triage 重算的命令）不视为阻断；
        # 仅当显式非 "passed" 时才广播 safety.blocked。
        if (
            (triage_decision is not None and triage_decision != "passed")
            or completeness_disposition == "triage_blocked"
        ):
            events.extend(
                (
                    MappedSessionEvent(
                        "safety.blocked",
                        {**common, "issues": [], "rollback_target": "none", "reason_code": "TRIAGE_BLOCKED"},
                    ),
                    MappedSessionEvent(
                        "session.blocked",
                        {**common, "blocked_reason": "triage_blocked"},
                    ),
                )
            )
        elif completeness_disposition == "stagnated":
            events.append(
                MappedSessionEvent(
                    "session.blocked",
                    {**common, "blocked_reason": "intake_stagnated_manual_required"},
                )
            )
        return tuple(events)

    if message.event_type == "safety_confirmation.recomputed.v1":
        stage = _string(payload, "stage")
        action = _string(payload, "action")
        assertion_id = _string(payload, "assertion_id")
        completeness_disposition = _string(payload, "completeness_disposition")
        if (
            stage not in {"inquiry", "blocked"}
            or action not in {"confirm", "reject", "retract"}
            or not assertion_id
            or not completeness_disposition
        ):
            raise OutboxMappingError("safety_confirmation.recomputed.v1 has an invalid contract")
        events = [_agent_event(message, "agent.finished", "safety_confirmation")]
        question_message_id = _string(payload, "question_message_id")
        if stage == "blocked":
            blocked_reason = _string(payload, "blocked_reason")
            if not blocked_reason or question_message_id:
                raise OutboxMappingError("blocked safety recompute has an invalid contract")
            events.append(
                MappedSessionEvent(
                    "session.blocked",
                    {**common, "blocked_reason": blocked_reason},
                )
            )
        elif question_message_id:
            events.append(
                MappedSessionEvent(
                    "message.created",
                    {
                        **common,
                        "message_id": question_message_id,
                        "role": "agent",
                        "stage": "inquiry",
                        "agent_name": "question_composer",
                    },
                )
            )
        return tuple(events)

    if message.event_type == "advance.command_started.v1":
        events = [_agent_event(message, "agent.started", "reasoning")]
        from_stage = _string(payload, "from_stage")
        to_stage = _string(payload, "to_stage")
        if from_stage and to_stage and from_stage != to_stage:
            events.insert(
                0,
                MappedSessionEvent(
                    "stage.changed",
                    {**common, "from_stage": from_stage, "to_stage": to_stage},
                ),
            )
        return tuple(events)

    if message.event_type == "reasoning.artifact_committed.v1":
        artifact_type = _string(payload, "artifact_type") or "reasoning"
        event = _agent_event(message, "agent.finished", artifact_type)
        safe_payload = dict(event.payload)
        for name in ("artifact_id", "content_digest", "decision"):
            value = _string(payload, name)
            if value:
                safe_payload[name] = value
        revision = payload.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
            safe_payload["revision"] = revision
        return (MappedSessionEvent(event.event_type, safe_payload),)

    if message.event_type in {"reasoning.command_completed.v1", "advance.command_completed.v1"}:
        events = [_agent_event(message, "agent.finished", "reasoning")]
        route = _string(payload, "route")
        if route == "needs_more_info":
            events.append(
                MappedSessionEvent(
                    "stage.changed",
                    {**common, "from_stage": "syndrome", "to_stage": "inquiry"},
                )
            )
            question_message_id = _string(payload, "question_message_id")
            if question_message_id:
                events.append(
                    MappedSessionEvent(
                        "message.created",
                        {
                            **common,
                            "message_id": question_message_id,
                            "role": "agent",
                            "stage": "inquiry",
                            "agent_name": "question_composer",
                        },
                    )
                )
        elif route == "manual_required":
            events.append(
                MappedSessionEvent(
                    "session.blocked",
                    {**common, "blocked_reason": "reasoning_manual_required"},
                )
            )
        else:
            events.append(
                MappedSessionEvent(
                    "stage.changed",
                    {**common, "from_stage": "syndrome", "to_stage": "safety"},
                )
            )
        return tuple(events)

    if message.event_type == "domain.state_committed.v1":
        return (_agent_event(message, "agent.finished", "domain_commit"),)

    if message.event_type in _ASYNC_COMMAND_EVENT_VERSIONS:
        return (_command_event(message, message.event_type),)

    raise OutboxMappingError(f"unsupported internal event type: {message.event_type}")


class OutboxPublisher:
    """Claim, publish and settle durable outbox rows."""

    def __init__(
        self,
        repository: OutboxRepository,
        event_sink: SessionEventSink | EventService,
        *,
        worker_id: str,
        batch_size: int = 50,
        lease_seconds: int = 30,
        max_attempts: int = 8,
        base_retry_seconds: int = 1,
        max_retry_seconds: int = 300,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1..128 characters")
        if batch_size < 1 or lease_seconds < 1 or max_attempts < 1:
            raise ValueError("batch_size, lease_seconds and max_attempts must be positive")
        if base_retry_seconds < 0 or max_retry_seconds < base_retry_seconds:
            raise ValueError("invalid retry interval bounds")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._repository = repository
        self._event_sink = cast(SessionEventSink, event_sink)
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._base_retry_seconds = base_retry_seconds
        self._max_retry_seconds = max_retry_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def run_once(self) -> PublishBatchResult:
        messages = await self._repository.claim(
            worker_id=self._worker_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        published = retried = dead_lettered = ownership_lost = 0
        for message in messages:
            outcome = await self._process(message)
            published += outcome == "published"
            retried += outcome == "retried"
            dead_lettered += outcome == "dead_lettered"
            ownership_lost += outcome == "ownership_lost"
        return PublishBatchResult(
            claimed=len(messages),
            published=published,
            retried=retried,
            dead_lettered=dead_lettered,
            ownership_lost=ownership_lost,
        )

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until stopped; finish an already claimed batch before returning."""
        while not stop.is_set():
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("outbox claim cycle failed: %s", type(exc).__name__)
                result = PublishBatchResult()
            if result.claimed:
                await asyncio.sleep(0)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)

    async def health(self) -> OutboxHealth:
        return await self._repository.get_outbox_health()

    async def _process(self, message: OutboxMessage) -> str:
        try:
            mapped = map_outbox_event(message)
            for index, event in enumerate(mapped):
                await self._event_sink.append_session_event_once(
                    str(message.session_id),
                    event.event_type,
                    event.payload,
                    dedupe_id=f"{message.event_id}:{index}",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._settle_failure(message, self._error_code(exc))

        try:
            acknowledged = await self._repository.acknowledge(message.event_id, worker_id=self._worker_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Publication may have succeeded. Leave the lease untouched; after it
            # expires another worker will replay through Redis dedupe and ack.
            logger.warning("outbox acknowledgement failed: %s", type(exc).__name__)
            return "ownership_lost"
        return "published" if acknowledged else "ownership_lost"

    async def _settle_failure(self, message: OutboxMessage, code: OutboxErrorCode) -> str:
        if message.attempt_count >= self._max_attempts:
            moved = await self._repository.dead_letter(
                message.event_id,
                worker_id=self._worker_id,
                error_code=code,
            )
            return "dead_lettered" if moved else "ownership_lost"
        released = await self._repository.release_failed(
            message.event_id,
            worker_id=self._worker_id,
            error_code=code,
            retry_after_seconds=self.retry_delay_seconds(message.attempt_count),
        )
        return "retried" if released else "ownership_lost"

    def retry_delay_seconds(self, attempt_count: int) -> int:
        exponent = min(63, max(0, attempt_count - 1))
        return min(self._max_retry_seconds, self._base_retry_seconds * (1 << exponent))

    @staticmethod
    def _error_code(exc: Exception) -> OutboxErrorCode:
        if isinstance(exc, asyncio.TimeoutError | RedisTimeoutError):
            return OutboxErrorCode.PUBLISH_TIMEOUT
        if isinstance(exc, RedisConnectionError | RedisError | ConnectionError):
            return OutboxErrorCode.PUBLISH_UNAVAILABLE
        if isinstance(exc, OutboxMappingError | ValueError):
            return OutboxErrorCode.PUBLISH_REJECTED
        return OutboxErrorCode.PUBLISH_UNKNOWN
