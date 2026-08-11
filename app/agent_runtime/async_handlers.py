"""R7 async-command handlers for the three allowlisted operations.

Each handler reuses the existing synchronous business execution (the very same
functions the POST routes call) with a fresh, job-local database session and
the already-started shared LangGraph runtime. No clinical logic is copied: a
handler only reconstructs the request body from the canonical private payload,
derives a deterministic downstream idempotency key from the ``command_id``,
and delegates to the shared service.

Handlers map deterministic business outcomes onto the finite, PHI-safe
error-code allowlist (``ASYNC_COMMAND_ERROR_CODES``) and surface retryable
infrastructure failures as ``HANDLER_UNAVAILABLE``. Exception text and arbitrary
error payloads are never persisted — the worker sanitizes terminal failures to
an empty object, and unexpected crashes surface as ``HANDLER_UNEXPECTED``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.agent_runtime.async_command import ASYNC_COMMAND_ERROR_CODES
from app.agent_runtime.async_command_admission import (
    derive_command_trace_id,
    derive_downstream_key,
)
from app.agent_runtime.async_command_worker import (
    AsyncCommandContext,
    AsyncCommandHandler,
    CommandFailureError,
    CommandSuccess,
)
from app.agent_runtime.lifecycle import SharedLangGraphRuntime
from app.core.exceptions import AgentTriggerFailedError, ModelGatewayError, XuanhuError
from app.db.session import get_session_factory
from app.schemas.advance import AdvanceRequest
from app.schemas.message import MessageCreateRequest
from app.schemas.review import ReviewRequest

_SUCCEEDED_HTTP_STATUS = 200


def build_async_command_handlers(
    shared_runtime: SharedLangGraphRuntime | None,
) -> dict[str, AsyncCommandHandler]:
    """Build the worker handler registry, closed over the shared runtime.

    Returns an empty registry when the runtime is unavailable so the worker
    fails closed and the R7 default async admission never routes to the
    substrate (requests fall through to the synchronous path).
    """
    if shared_runtime is None:
        return {}

    async def handle_intake_message(context: AsyncCommandContext) -> CommandSuccess:
        body = MessageCreateRequest.model_validate(context.request_payload["body"])
        downstream_key = derive_downstream_key(context.command_id, context.operation)
        trace_id = derive_command_trace_id(context.command_id)
        async with get_session_factory()() as db:
            from app.services.message import MessageService

            service = MessageService(
                db,
                shared_langgraph_runtime=shared_runtime,
                allow_request_local_langgraph_runtime=False,
            )
            data = await service.submit_message(
                str(context.session_id),
                body,
                doctor_id=context.request_payload.get("doctor_id"),
                trace_id=trace_id,
                x_state_version=context.request_payload.get("state_version"),
                idempotency_key=downstream_key,
            )
            await db.commit()
        return CommandSuccess(
            http_status=_SUCCEEDED_HTTP_STATUS,
            result_payload=data.model_dump(mode="json", exclude_none=True),
        )

    async def handle_session_advance(context: AsyncCommandContext) -> CommandSuccess:
        body = AdvanceRequest.model_validate(context.request_payload["body"])
        downstream_key = derive_downstream_key(context.command_id, context.operation)
        trace_id = derive_command_trace_id(context.command_id)
        async with get_session_factory()() as db:
            from app.api.advance import run_langgraph_advance_flow

            response = await run_langgraph_advance_flow(
                db,
                session_id=str(context.session_id),
                state_version=context.request_payload.get("state_version"),
                trace_id=trace_id,
                force=bool(body.force),
                idempotency_key=downstream_key,
                alternative_index=body.alternative_index,
                shared_runtime=shared_runtime,
                allow_request_local_runtime=False,
            )
            await db.commit()
        return CommandSuccess(
            http_status=_SUCCEEDED_HTTP_STATUS,
            result_payload=dict(response),
        )

    async def handle_prescription_review(context: AsyncCommandContext) -> CommandSuccess:
        body = ReviewRequest.model_validate(context.request_payload["body"])
        downstream_key = derive_downstream_key(context.command_id, context.operation)
        trace_id = derive_command_trace_id(context.command_id)
        async with get_session_factory()() as db:
            from app.services.langgraph_review import LangGraphReviewService

            service = LangGraphReviewService(db)
            data = await service.review(
                str(context.session_id),
                body,
                doctor_id=context.request_payload.get("doctor_id"),
                trace_id=trace_id,
                x_state_version=context.request_payload.get("state_version"),
                idempotency_key=downstream_key,
                shared_runtime=shared_runtime,
                allow_request_local_runtime=False,
            )
            await db.commit()
        return CommandSuccess(
            http_status=_SUCCEEDED_HTTP_STATUS,
            result_payload=data.model_dump(mode="json"),
        )

    return {
        "intake.message": _wrap_business(handle_intake_message),
        "session.advance": _wrap_business(handle_session_advance),
        "prescription.review": _wrap_business(handle_prescription_review),
    }


def _wrap_business(
    fn: Callable[[AsyncCommandContext], Awaitable[CommandSuccess]],
) -> AsyncCommandHandler:
    """Translate deterministic domain failures into finite PHI-safe codes."""

    async def wrapped(context: AsyncCommandContext) -> CommandSuccess:
        try:
            return await fn(context)
        except CommandFailureError:
            raise
        except XuanhuError as exc:
            raise _map_business_error(exc) from None
        except ModelGatewayError:
            raise CommandFailureError(error_code="HANDLER_UNAVAILABLE", retryable=True) from None

    return wrapped


def _map_business_error(exc: XuanhuError) -> CommandFailureError:
    """Map a known business exception to a finite, PHI-safe failure code.

    Retryable ``AgentTriggerFailedError`` (the service's runtime-unavailable
    signal) collapses to ``HANDLER_UNAVAILABLE``; the non-retryable legacy
    read-only variant surfaces as the deterministic ``AGENT_TRIGGER_FAILED``.
    Any other code outside the allowlist collapses to ``HANDLER_REJECTED``
    (non-retryable) or ``HANDLER_UNAVAILABLE`` (retryable).
    """
    if isinstance(exc, AgentTriggerFailedError):
        if exc.retryable:
            return CommandFailureError(error_code="HANDLER_UNAVAILABLE", retryable=True)
        return CommandFailureError(
            error_code="AGENT_TRIGGER_FAILED",
            retryable=False,
        )
    code = str(exc.code)
    if code not in ASYNC_COMMAND_ERROR_CODES:
        code = "HANDLER_REJECTED" if not exc.retryable else "HANDLER_UNAVAILABLE"
    return CommandFailureError(error_code=code, retryable=bool(exc.retryable))


__all__ = ["build_async_command_handlers"]
