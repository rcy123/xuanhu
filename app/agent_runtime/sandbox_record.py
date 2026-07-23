"""Offline sandbox medical record DTO and deterministic assembler (L6-1)."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_runtime.sandbox_recheck import SandboxRecheckSnapshotV1
from app.agent_runtime.sandbox_review import (
    SandboxReviewAction,
    canonical_review_bytes,
)
from app.agent_runtime.sandbox_safety import SandboxSafetyDecision

_MAX_RECORD_BYTES = 256 * 1024
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_RECORD_REF_PATTERN = r"^sandbox-record-[0-9a-f]{64}$"
SANDBOX_RECORD_DISCLAIMER: Literal[
    "sandbox_assemble_only_not_a_medical_record"
] = "sandbox_assemble_only_not_a_medical_record"

RECORD_SCHEMA_VERSION: Literal["sandbox-medical-record.v1"] = "sandbox-medical-record.v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SandboxRecordError(ValueError):
    """A fixed, payload-free, chainless record-boundary failure."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("SANDBOX_RECORD_UNAVAILABLE")


class SandboxMedicalRecordData(_StrictFrozenModel):
    """Immutable, deterministic medical record DTO — no model calls, no free text."""

    record_id: str = Field(pattern=_RECORD_REF_PATTERN)
    session_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    revision_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    reviewed_formula: tuple[dict[str, object], ...]
    safety_result: dict[str, object]
    review_confirm_ref: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    assembled_at: int = Field(ge=0)
    record_version: Literal["sandbox-medical-record.v1"]
    disclaimer: Literal["sandbox_assemble_only_not_a_medical_record"]

    @model_validator(mode="after")
    def record_id_is_derived(self) -> SandboxMedicalRecordData:
        expected = _record_id(
            session_id=self.session_id,
            revision_id=self.revision_id,
            reviewed_formula=self.reviewed_formula,
            safety_result=self.safety_result,
            review_confirm_ref=self.review_confirm_ref,
            assembled_at=self.assembled_at,
            record_version=self.record_version,
            disclaimer=self.disclaimer,
        )
        if self.record_id != expected:
            raise ValueError("record id mismatch")
        return self


class SandboxRecordAssembler:
    """Deterministically assemble a medical record from confirmed review state.

    The assembler reads only from accepted L5-3/L5-4 in-memory coordinators.
    It never calls a model, generates free text, or mutates inputs.
    """

    __slots__ = ()

    def assemble(
        self,
        recheck_snapshot: object,
        *,
        namespace: str,
        session_id: str,
        thread_id: str,
        checkpoint_id: str,
        now: int,
    ) -> SandboxMedicalRecordData:
        """Assemble a record from a confirmed review state, or fail fixed-closed."""
        record = self._build_record(
            recheck_snapshot,
            namespace=namespace,
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            now=now,
        )
        if record is None:
            raise SandboxRecordError()
        return record

    @staticmethod
    def _build_record(
        recheck_snapshot: object,
        *,
        namespace: str,
        session_id: str,
        thread_id: str,
        checkpoint_id: str,
        now: int,
    ) -> SandboxMedicalRecordData | None:
        """Build the record or return None; never propagates a payload-bearing exception."""
        try:
            if isinstance(recheck_snapshot, SandboxRecheckSnapshotV1):
                snapshot = recheck_snapshot
            else:
                snapshot = SandboxRecheckSnapshotV1.model_validate_json(
                    canonical_review_bytes(recheck_snapshot), strict=True
                )
            revisions = snapshot.revisions
            if not revisions:
                return None

            current = revisions[-1]
            if current.status != "review_required":
                return None

            if current.result is None or current.result.decision is not SandboxSafetyDecision.ALLOW:
                return None

            if current.challenge_ref is None:
                return None

            review_snapshot = snapshot.review_snapshot
            review_challenges = tuple(
                challenge
                for challenge in review_snapshot.challenges
                if challenge.challenge_ref == current.challenge_ref
            )
            if len(review_challenges) != 1:
                return None
            challenge = review_challenges[0]

            if challenge.state != "applied":
                return None

            review_events = tuple(
                event
                for event in review_snapshot.events
                if event.challenge_ref == current.challenge_ref
                and event.action is SandboxReviewAction.CONFIRM
            )
            if len(review_events) != 1:
                return None
            applied_event = review_events[0]

            review_attempts = tuple(
                attempt
                for attempt in review_snapshot.attempts
                if attempt.resume_attempt_ref == applied_event.resume_attempt_ref
                and attempt.challenge_ref == current.challenge_ref
                and attempt.state == "applied"
            )
            if len(review_attempts) != 1:
                return None

            if (
                current.namespace != namespace
                or current.test_session_id != session_id
                or current.thread_id != thread_id
                or current.checkpoint_id != checkpoint_id
            ):
                return None

            subject = current.subject
            reviewed_formula = tuple(
                {
                    "item_id": item.item_id,
                    "component": item.component,
                    "amount_milliunits": item.amount_milliunits,
                    "unit": item.unit,
                }
                for item in subject.formula_items
            )

            safety_result = current.result.model_dump(mode="json")

            review_confirm_ref = applied_event.resume_attempt_ref

            revision_id = _revision_id(current.revision_ref)

            return SandboxMedicalRecordData(
                record_id=_record_id(
                    session_id=session_id,
                    revision_id=revision_id,
                    reviewed_formula=reviewed_formula,
                    safety_result=safety_result,
                    review_confirm_ref=review_confirm_ref,
                    assembled_at=now,
                    record_version=RECORD_SCHEMA_VERSION,
                    disclaimer=SANDBOX_RECORD_DISCLAIMER,
                ),
                session_id=session_id,
                revision_id=revision_id,
                reviewed_formula=reviewed_formula,
                safety_result=safety_result,
                review_confirm_ref=review_confirm_ref,
                assembled_at=now,
                record_version=RECORD_SCHEMA_VERSION,
                disclaimer=SANDBOX_RECORD_DISCLAIMER,
            )
        except Exception:
            return None


def _record_id(
    *,
    session_id: str,
    revision_id: str,
    reviewed_formula: tuple[dict[str, object], ...],
    safety_result: dict[str, object],
    review_confirm_ref: str,
    assembled_at: int,
    record_version: str,
    disclaimer: str,
) -> str:
    body: dict[str, object] = {
        "assembled_at": assembled_at,
        "disclaimer": disclaimer,
        "record_version": record_version,
        "review_confirm_ref": review_confirm_ref,
        "reviewed_formula": reviewed_formula,
        "revision_id": revision_id,
        "safety_result": safety_result,
        "session_id": session_id,
    }
    return "sandbox-record-" + _digest(body)


def _revision_id(revision_ref: str) -> str:
    return revision_ref.removeprefix("sandbox-recheck-revision-")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_review_bytes(value)).hexdigest()
