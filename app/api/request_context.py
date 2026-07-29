"""Shared request metadata for public write operations.

Trace identifiers describe an execution attempt.  Idempotency identifiers
describe a logical command and therefore must never be inferred from a trace
identifier.  Requests which omit ``X-Idempotency-Key`` receive a fresh nonce,
making every such request an independent, explicitly non-idempotent command.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Header, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.services.http_idempotency import HttpCommandExecutor, HttpCommandResult

IDEMPOTENCY_KEY_HEADER = "X-Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 128
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


@dataclass(frozen=True, slots=True)
class WriteRequestContext:
    """Attempt metadata and the effective logical-command identifier."""

    trace_id: str
    idempotency_key: str
    is_idempotent: bool


def get_trace_id(request: Request) -> str:
    """Return the caller's trace identifier or create one for this attempt."""

    return request.headers.get("x-request-id") or request.headers.get("x-trace-id") or str(uuid.uuid4())


def validate_idempotency_key(value: str) -> str:
    """Validate an externally supplied idempotency key without normalising it.

    Normalising malformed values would allow distinct public keys to collapse
    onto one command.  The deliberately small ASCII alphabet also rejects all
    whitespace, control characters (including CR/LF), and header delimiters.
    """

    if not 1 <= len(value) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        raise _invalid_idempotency_key()
    if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise _invalid_idempotency_key()
    return value


def write_request_context(
    request: Request,
    x_idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
) -> WriteRequestContext:
    """Build context for a public write request.

    Duplicate headers are rejected because choosing one value would make proxy
    and application interpretations ambiguous.  A missing key gets a fresh
    UUID nonce; it is intentionally not derived from ``X-Request-Id``.
    """

    del x_idempotency_key  # raw request values are required to detect duplicate headers
    values = request.headers.getlist("x-idempotency-key")
    if len(values) > 1:
        raise _invalid_idempotency_key("X-Idempotency-Key must appear at most once")
    if values:
        public_key = validate_idempotency_key(values[0])
        return WriteRequestContext(
            trace_id=get_trace_id(request),
            idempotency_key=public_key,
            is_idempotent=True,
        )
    return WriteRequestContext(
        trace_id=get_trace_id(request),
        idempotency_key=uuid.uuid4().hex,
        is_idempotent=False,
    )


async def execute_model_write(
    db: AsyncSession,
    context: WriteRequestContext,
    *,
    operation: str,
    scope_key: str,
    concurrency_scope: str | None,
    request_payload: dict[str, Any],
    success_status: int,
    success_message: str,
    handler: Callable[[], Awaitable[BaseModel]],
    durable_outcome_resolver: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
) -> HttpCommandResult:
    """Execute and persist a Pydantic-returning public write operation."""

    async def dump_model() -> dict[str, Any]:
        result = await handler()
        return result.model_dump(mode="json")

    return await HttpCommandExecutor(db).execute(
        operation=operation,
        scope_key=scope_key,
        concurrency_scope=concurrency_scope,
        idempotency_key=context.idempotency_key,
        is_idempotent=context.is_idempotent,
        request_payload=request_payload,
        success_status=success_status,
        success_message=success_message,
        handler=dump_model,
        durable_outcome_resolver=durable_outcome_resolver,
    )


def _invalid_idempotency_key(
    detail: str = "X-Idempotency-Key must contain 1-128 ASCII letters, digits, '.', '_', ':', or '-'",
) -> ValidationError:
    return ValidationError(
        message="X-Idempotency-Key 格式无效",
        detail=detail,
        retryable=False,
    )
