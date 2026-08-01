"""Deterministic intake-complete notice shared by intake and safety recompute paths.

When the completeness gate becomes READY the chat must not end in silence: the
patient's final answer is acknowledged with a short completion notice that also
points the operator at the next step.  The notice is a plain consult message
(agent role, ``structured_delta.kind="completion_notice"``), deliberately not a
``QuestionComposerResult`` so it can never be mistaken for a pending question.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consult import ConsultMessage

INTAKE_COMPLETE_NOTICE_TEXT = "问诊要素已采集完整，可以进入辨证开方阶段。"
_INTAKE_COMPLETE_NOTICE_SCHEMA = "intake-complete-notice.v1"


def intake_complete_notice_delta() -> dict[str, object]:
    """Structured delta that marks a message as an intake-complete notice."""

    return {
        "source": "intake_complete",
        "kind": "completion_notice",
        "schema_version": _INTAKE_COMPLETE_NOTICE_SCHEMA,
        "question": INTAKE_COMPLETE_NOTICE_TEXT,
    }


def is_intake_complete_notice(message: ConsultMessage) -> bool:
    """True when ``message`` is an intake-complete notice, never a question."""

    delta = message.structured_delta
    if not isinstance(delta, dict):
        return False
    return (
        delta.get("kind") == "completion_notice"
        and delta.get("source") == "intake_complete"
    )


async def latest_agent_message_is_intake_complete(
    db: AsyncSession,
    session_id: UUID,
) -> bool:
    """True when the newest agent message in the session is already the notice.

    Used to avoid emitting duplicate notices when a follow-up patient message
    arrives after readiness (e.g. the operator types "你好" after completion).
    """

    message = await db.scalar(
        select(ConsultMessage)
        .where(
            ConsultMessage.session_id == session_id,
            ConsultMessage.role == "agent",
        )
        .order_by(ConsultMessage.created_at.desc(), ConsultMessage.id.desc())
        .limit(1)
    )
    return message is not None and is_intake_complete_notice(message)


__all__ = [
    "INTAKE_COMPLETE_NOTICE_TEXT",
    "intake_complete_notice_delta",
    "is_intake_complete_notice",
    "latest_agent_message_is_intake_complete",
]
