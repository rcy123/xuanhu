"""Durable safety-fact proposal, confirmation, and projection service."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.request_context import WriteRequestContext
from app.core.exceptions import (
    IdempotencyConflictError,
    InvalidStageTransitionError,
    SessionNotFoundError,
    ValidationError,
)
from app.db.session import get_session_factory
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import SafetyFactAssertion, SafetyFactTransition, SafetyProfile
from app.schemas.domain import CollectionStatus, SafetyProfileSchema
from app.schemas.intake import (
    EvidenceSpan,
    IntakeExtractionOutput,
    LactationDelta,
    PatientSafetyDelta,
    PregnancyDelta,
)
from app.schemas.safety_confirmation import (
    SafetyAssertionStatus,
    SafetyEvidenceRef,
    SafetyFactAssertionList,
    SafetyFactAssertionRead,
    SafetyFactField,
)

SafetyAction = Literal["confirm", "reject", "retract"]
_PROJECTED_FIELDS = frozenset(
    {
        SafetyFactField.ALLERGY,
        SafetyFactField.PREGNANCY,
        SafetyFactField.LACTATION,
        SafetyFactField.MEDICATIONS,
        SafetyFactField.MAJOR_CONDITIONS,
        SafetyFactField.CONTRAINDICATIONS,
    }
)


@dataclass(frozen=True, slots=True)
class _Proposal:
    field_name: SafetyFactField
    value: dict[str, Any]
    evidence_spans: tuple[EvidenceSpan, ...]


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assertion_fingerprint(
    *,
    session_id: uuid.UUID,
    field_name: str,
    value_digest: str,
    source_kind: str,
    source_message_id: uuid.UUID,
    extraction_run_id: uuid.UUID | None,
    template_version: str,
    evidence_digest: str,
) -> str:
    return _canonical_digest(
        {
            "session_id": str(session_id),
            "field_name": field_name,
            "value_digest": value_digest,
            "source_kind": source_kind,
            "source_message_id": str(source_message_id),
            "extraction_run_id": str(extraction_run_id) if extraction_run_id else None,
            "template_version": template_version,
            "evidence_digest": evidence_digest,
        }
    )


def _evidence_refs(
    spans: tuple[EvidenceSpan, ...],
    source_message: ConsultMessage,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for span in spans:
        if span.source_message_id != source_message.id:
            raise ValidationError(detail="safety evidence belongs to a different source message")
        if span.end_char > len(source_message.content):
            raise ValidationError(detail="safety evidence range exceeds its source message")
        quote = source_message.content[span.start_char : span.end_char]
        if quote != span.quote:
            raise ValidationError(detail="safety evidence does not match its source message")
        refs.append(
            {
                "source_message_id": str(source_message.id),
                "start_char": span.start_char,
                "end_char": span.end_char,
                "quote_digest": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            }
        )
    return refs


def _list_value(status: CollectionStatus, values: tuple[str, ...] | None) -> dict[str, Any]:
    return {
        "collection_status": status.value,
        "values": list(values) if status is CollectionStatus.COLLECTED and values else None,
    }


def _scalar_value(status: CollectionStatus, value: object | None) -> dict[str, Any]:
    return {
        "collection_status": status.value,
        "value": value.value if hasattr(value, "value") else value,
    }


def _intake_proposals(output: IntakeExtractionOutput) -> tuple[_Proposal, ...]:
    delta: PatientSafetyDelta = output.patient_safety_delta
    proposals: list[_Proposal] = []
    for field, item in (
        (SafetyFactField.ALLERGY, delta.allergy),
        (SafetyFactField.MEDICATIONS, delta.medications),
        (SafetyFactField.MAJOR_CONDITIONS, delta.major_conditions),
        (SafetyFactField.CONTRAINDICATIONS, delta.contraindications),
    ):
        if item.status is CollectionStatus.UNKNOWN:
            continue
        spans = item.value_spans or ((item.negation_span,) if item.negation_span is not None else ())
        proposals.append(_Proposal(field, _list_value(item.status, item.values), tuple(spans)))
    scalar_items: tuple[tuple[SafetyFactField, PregnancyDelta | LactationDelta], ...] = (
        (SafetyFactField.PREGNANCY, delta.pregnancy),
        (SafetyFactField.LACTATION, delta.lactation),
    )
    for field, scalar_item in scalar_items:
        if scalar_item.status is CollectionStatus.UNKNOWN:
            continue
        proposals.append(
            _Proposal(
                field,
                _scalar_value(scalar_item.status, scalar_item.value),
                (scalar_item.span,) if scalar_item.span is not None else (),
            )
        )
    proposals.extend(
        _Proposal(
            SafetyFactField.RED_FLAG,
            {
                "category": item.category.value,
                "severity": item.severity.value,
                "confidence": item.confidence,
            },
            (item.span,),
        )
        for item in output.red_flag_candidates
    )
    return tuple(proposals)


def _read(row: SafetyFactAssertion) -> SafetyFactAssertionRead:
    return SafetyFactAssertionRead(
        assertion_id=row.id,
        session_id=row.session_id,
        field_name=SafetyFactField(row.field_name),
        value=dict(row.value),
        value_digest=row.value_digest,
        status=SafetyAssertionStatus(row.status),
        source_kind=row.source_kind,
        source_message_id=row.source_message_id,
        extraction_run_id=row.extraction_run_id,
        template_version=row.template_version,
        evidence_spans=tuple(SafetyEvidenceRef.model_validate(item) for item in row.evidence_spans),
        evidence_digest=row.evidence_digest,
        proposed_at=row.proposed_at,
        confirmed_at=row.confirmed_at,
        rejected_at=row.rejected_at,
        retracted_at=row.retracted_at,
        superseded_at=row.superseded_at,
        supersedes_assertion_id=row.supersedes_assertion_id,
    )


class SafetyConfirmationService:
    """Owns assertion integrity, decision transitions, and SafetyProfile projection."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def propose_from_intake(
        self,
        *,
        session_id: uuid.UUID,
        source_message: ConsultMessage,
        output: IntakeExtractionOutput,
        extraction_run_id: uuid.UUID,
        template_version: str,
        source_kind: Literal["model_extraction", "deterministic_precheck"] = "model_extraction",
        trace_id: str,
    ) -> tuple[SafetyFactAssertionRead, ...]:
        """Persist grounded candidates without changing authoritative safety state."""

        if source_message.session_id != session_id:
            raise ValidationError(detail="safety proposal source belongs to a different session")
        result: list[SafetyFactAssertionRead] = []
        for proposal in _intake_proposals(output):
            evidence = _evidence_refs(proposal.evidence_spans, source_message)
            if not evidence:
                raise ValidationError(detail="extracted safety facts require grounded evidence")
            row, created = await self._insert_assertion(
                session_id=session_id,
                field_name=proposal.field_name,
                value=proposal.value,
                source_kind=source_kind,
                source_message_id=source_message.id,
                extraction_run_id=extraction_run_id,
                template_version=template_version,
                evidence_spans=evidence,
                evidence_digest=_canonical_digest(evidence),
                proposed_by_actor_type="model" if source_kind == "model_extraction" else "system",
                proposed_by_actor_id=None,
                status=SafetyAssertionStatus.PROPOSED,
            )
            if created:
                self._db.add(
                    AuditEvent(
                        session_id=session_id,
                        event_type="safety_fact.proposed",
                        actor_type="agent" if source_kind == "model_extraction" else "system",
                        actor_id=None,
                        payload={
                            "assertion_id": str(row.id),
                            "field_name": row.field_name,
                            "status": row.status,
                            "value_digest": row.value_digest,
                            "evidence_digest": row.evidence_digest,
                            "source_message_id": str(row.source_message_id),
                            "extraction_run_id": str(row.extraction_run_id),
                            "template_version": row.template_version,
                        },
                        trace_id=trace_id[:64],
                    )
                )
            result.append(_read(row))
        await self._db.flush()
        return tuple(result)

    async def add_confirmed_structured_form(
        self,
        *,
        session_id: uuid.UUID,
        source_message_id: uuid.UUID,
        safety_profile: SafetyProfileSchema,
        payload_digest: str,
        actor_type: Literal["doctor", "system"],
        actor_id: str | None,
        trace_id: str,
        template_version: str,
    ) -> tuple[SafetyFactAssertionRead, ...]:
        """Record explicitly supplied structured form facts as confirmed provenance."""

        values: tuple[tuple[SafetyFactField, dict[str, Any]], ...] = (
            (
                SafetyFactField.ALLERGY,
                _list_value(safety_profile.allergy_collection_status, tuple(safety_profile.allergens or ())),
            ),
            (
                SafetyFactField.PREGNANCY,
                _scalar_value(safety_profile.pregnancy_collection_status, safety_profile.pregnancy_value),
            ),
            (
                SafetyFactField.LACTATION,
                _scalar_value(safety_profile.lactation_collection_status, safety_profile.lactation_value),
            ),
            (
                SafetyFactField.MEDICATIONS,
                _list_value(safety_profile.medications_collection_status, tuple(safety_profile.medications or ())),
            ),
            (
                SafetyFactField.MAJOR_CONDITIONS,
                _list_value(
                    safety_profile.major_conditions_collection_status,
                    tuple(safety_profile.major_conditions or ()),
                ),
            ),
            (
                SafetyFactField.CONTRAINDICATIONS,
                _list_value(
                    safety_profile.contraindications_collection_status,
                    tuple(safety_profile.contraindications or ()),
                ),
            ),
        )
        now = datetime.now(UTC)
        rows: list[SafetyFactAssertionRead] = []
        for field_name, value in values:
            if value["collection_status"] == CollectionStatus.UNKNOWN.value:
                continue
            row, created = await self._insert_assertion(
                session_id=session_id,
                field_name=field_name,
                value=value,
                source_kind="structured_form",
                source_message_id=source_message_id,
                extraction_run_id=None,
                template_version=template_version,
                evidence_spans=[],
                evidence_digest=payload_digest,
                proposed_by_actor_type=actor_type,
                proposed_by_actor_id=actor_id,
                status=SafetyAssertionStatus.CONFIRMED,
                confirmed_by_actor_type=actor_type,
                confirmed_by_actor_id=actor_id,
                confirmed_at=now,
            )
            if created:
                self._db.add(
                    AuditEvent(
                        session_id=session_id,
                        event_type="safety_fact.confirmed_from_form",
                        actor_type=actor_type,
                        actor_id=actor_id,
                        payload={
                            "assertion_id": str(row.id),
                            "field_name": row.field_name,
                            "status": row.status,
                            "value_digest": row.value_digest,
                            "evidence_digest": row.evidence_digest,
                            "source_message_id": str(source_message_id),
                            "template_version": template_version,
                        },
                        trace_id=trace_id[:64],
                    )
                )
            rows.append(_read(row))
        await self._db.flush()
        return tuple(rows)

    async def list_assertions(
        self,
        session_id: uuid.UUID,
        *,
        status: SafetyAssertionStatus | None = None,
    ) -> SafetyFactAssertionList:
        session = await self._db.get(ConsultSession, session_id)
        if session is None or session.agent_runtime != "langgraph":
            raise SessionNotFoundError(detail="LangGraph session was not found")
        statement = select(SafetyFactAssertion).where(SafetyFactAssertion.session_id == session_id)
        if status is not None:
            statement = statement.where(SafetyFactAssertion.status == status.value)
        rows = (
            await self._db.scalars(statement.order_by(SafetyFactAssertion.proposed_at, SafetyFactAssertion.id))
        ).all()
        return SafetyFactAssertionList(items=tuple(_read(row) for row in rows))

    async def transition(
        self,
        *,
        session_id: uuid.UUID,
        assertion_id: uuid.UUID,
        action: SafetyAction,
        actor_id: str,
        context: WriteRequestContext,
        reason_code: str | None,
    ) -> SafetyFactAssertionRead:
        actor_id = actor_id.strip()
        if not actor_id or len(actor_id) > 128:
            raise ValidationError(detail="X-Doctor-Id is required and must be at most 128 characters")
        session = await self._db.scalar(
            select(ConsultSession).where(ConsultSession.id == session_id).with_for_update()
        )
        if session is None or session.agent_runtime != "langgraph":
            raise SessionNotFoundError(detail="LangGraph session was not found")
        if session.status == "terminated":
            raise InvalidStageTransitionError(detail="terminated sessions reject safety-fact decisions")

        key_digest = hashlib.sha256(context.idempotency_key.encode("utf-8")).hexdigest()
        request_digest = _canonical_digest(
            {
                "assertion_id": str(assertion_id),
                "action": action,
                "actor_id": actor_id,
                "reason_code": reason_code,
            }
        )
        replay = await self._db.scalar(
            select(SafetyFactTransition).where(
                SafetyFactTransition.session_id == session_id,
                SafetyFactTransition.idempotency_key_digest == key_digest,
            )
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise IdempotencyConflictError(detail="idempotency key was used for a different safety decision")
            replayed = await self._db.get(SafetyFactAssertion, replay.assertion_id)
            if replayed is None:
                raise ValidationError(detail="safety assertion transition ledger is inconsistent")
            return _read(replayed)

        assertion = await self._db.scalar(
            select(SafetyFactAssertion)
            .where(SafetyFactAssertion.id == assertion_id, SafetyFactAssertion.session_id == session_id)
            .with_for_update()
        )
        if assertion is None:
            raise SessionNotFoundError(detail="safety assertion was not found")

        changed = False
        now = datetime.now(UTC)
        if action == "confirm":
            if assertion.status == SafetyAssertionStatus.PROPOSED.value:
                await self._verify_integrity(assertion)
                await self._confirm(assertion, actor_id=actor_id, at=now)
                changed = True
            elif assertion.status != SafetyAssertionStatus.CONFIRMED.value:
                raise InvalidStageTransitionError(detail=f"cannot confirm a {assertion.status} assertion")
            resulting_status = SafetyAssertionStatus.CONFIRMED
        elif action == "reject":
            if assertion.status == SafetyAssertionStatus.PROPOSED.value:
                assertion.status = SafetyAssertionStatus.REJECTED.value
                assertion.rejected_by_actor_type = "doctor"
                assertion.rejected_by_actor_id = actor_id
                assertion.rejected_at = now
                changed = True
            elif assertion.status != SafetyAssertionStatus.REJECTED.value:
                raise InvalidStageTransitionError(detail=f"cannot reject a {assertion.status} assertion")
            resulting_status = SafetyAssertionStatus.REJECTED
        else:
            if assertion.status == SafetyAssertionStatus.CONFIRMED.value:
                assertion.status = SafetyAssertionStatus.RETRACTED.value
                assertion.retracted_by_actor_type = "doctor"
                assertion.retracted_by_actor_id = actor_id
                assertion.retracted_at = now
                await self._clear_projection(assertion)
                changed = True
            elif assertion.status != SafetyAssertionStatus.RETRACTED.value:
                raise InvalidStageTransitionError(detail=f"cannot retract a {assertion.status} assertion")
            resulting_status = SafetyAssertionStatus.RETRACTED

        if changed:
            session.state_version += 1
        transition = SafetyFactTransition(
            id=uuid.uuid4(),
            session_id=session_id,
            assertion_id=assertion.id,
            action=action,
            idempotency_key_digest=key_digest,
            request_digest=request_digest,
            resulting_status=resulting_status.value,
            actor_id=actor_id,
            reason_code=reason_code,
        )
        self._db.add(transition)
        self._db.add(
            AuditEvent(
                session_id=session_id,
                event_type=f"safety_fact.{action}",
                actor_type="doctor",
                actor_id=actor_id,
                payload={
                    "assertion_id": str(assertion.id),
                    "transition_id": str(transition.id),
                    "field_name": assertion.field_name,
                    "resulting_status": resulting_status.value,
                    "value_digest": assertion.value_digest,
                    "evidence_digest": assertion.evidence_digest,
                    "reason_code": reason_code,
                    "state_version": session.state_version,
                },
                trace_id=context.trace_id[:64],
            )
        )
        await self._db.flush()
        return _read(assertion)

    async def _insert_assertion(
        self,
        *,
        session_id: uuid.UUID,
        field_name: SafetyFactField,
        value: dict[str, Any],
        source_kind: str,
        source_message_id: uuid.UUID,
        extraction_run_id: uuid.UUID | None,
        template_version: str,
        evidence_spans: list[dict[str, Any]],
        evidence_digest: str,
        proposed_by_actor_type: str,
        proposed_by_actor_id: str | None,
        status: SafetyAssertionStatus,
        confirmed_by_actor_type: str | None = None,
        confirmed_by_actor_id: str | None = None,
        confirmed_at: datetime | None = None,
    ) -> tuple[SafetyFactAssertion, bool]:
        value_digest = _canonical_digest(value)
        fingerprint = _assertion_fingerprint(
            session_id=session_id,
            field_name=field_name.value,
            value_digest=value_digest,
            source_kind=source_kind,
            source_message_id=source_message_id,
            extraction_run_id=extraction_run_id,
            template_version=template_version,
            evidence_digest=evidence_digest,
        )
        assertion_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:safety-assertion:{session_id}:{fingerprint}")
        existing = await self._db.get(SafetyFactAssertion, assertion_id)
        if existing is not None:
            if (
                existing.assertion_fingerprint != fingerprint
                or existing.value_digest != value_digest
                or existing.evidence_digest != evidence_digest
            ):
                raise ValidationError(detail="stable safety assertion identifier has conflicting content")
            return existing, False
        row = SafetyFactAssertion(
            id=assertion_id,
            session_id=session_id,
            field_name=field_name.value,
            value=value,
            value_digest=value_digest,
            assertion_fingerprint=fingerprint,
            status=status.value,
            source_kind=source_kind,
            source_message_id=source_message_id,
            extraction_run_id=extraction_run_id,
            template_version=template_version,
            evidence_spans=evidence_spans,
            evidence_digest=evidence_digest,
            proposed_by_actor_type=proposed_by_actor_type,
            proposed_by_actor_id=proposed_by_actor_id,
            proposed_at=datetime.now(UTC),
            confirmed_by_actor_type=confirmed_by_actor_type,
            confirmed_by_actor_id=confirmed_by_actor_id,
            confirmed_at=confirmed_at,
        )
        self._db.add(row)
        return row, True

    async def _verify_integrity(self, assertion: SafetyFactAssertion) -> None:
        if _canonical_digest(assertion.value) != assertion.value_digest:
            raise ValidationError(detail="safety assertion value digest mismatch")
        expected_fingerprint = _assertion_fingerprint(
            session_id=assertion.session_id,
            field_name=assertion.field_name,
            value_digest=assertion.value_digest,
            source_kind=assertion.source_kind,
            source_message_id=assertion.source_message_id,
            extraction_run_id=assertion.extraction_run_id,
            template_version=assertion.template_version,
            evidence_digest=assertion.evidence_digest,
        )
        if expected_fingerprint != assertion.assertion_fingerprint:
            raise ValidationError(detail="safety assertion provenance fingerprint mismatch")
        source = await self._db.get(ConsultMessage, assertion.source_message_id)
        if source is None or source.session_id != assertion.session_id:
            raise ValidationError(detail="safety assertion source message is unavailable")
        if assertion.source_kind == "structured_form":
            source_digest = (source.structured_delta or {}).get("payload_digest")
            if source_digest != assertion.evidence_digest or assertion.evidence_spans:
                raise ValidationError(detail="structured safety assertion provenance mismatch")
            return
        refs: list[dict[str, Any]] = []
        for raw in assertion.evidence_spans:
            ref = SafetyEvidenceRef.model_validate(raw)
            if ref.source_message_id != source.id or ref.end_char > len(source.content):
                raise ValidationError(detail="safety assertion evidence range is invalid")
            quote = source.content[ref.start_char : ref.end_char]
            if hashlib.sha256(quote.encode("utf-8")).hexdigest() != ref.quote_digest:
                raise ValidationError(detail="safety assertion evidence was tampered with")
            refs.append(ref.model_dump(mode="json"))
        if not refs or _canonical_digest(refs) != assertion.evidence_digest:
            raise ValidationError(detail="safety assertion evidence digest mismatch")

    async def _confirm(self, assertion: SafetyFactAssertion, *, actor_id: str, at: datetime) -> None:
        field = SafetyFactField(assertion.field_name)
        if field in _PROJECTED_FIELDS:
            previous = (
                await self._db.scalars(
                    select(SafetyFactAssertion)
                    .where(
                        SafetyFactAssertion.session_id == assertion.session_id,
                        SafetyFactAssertion.field_name == assertion.field_name,
                        SafetyFactAssertion.status == SafetyAssertionStatus.CONFIRMED.value,
                        SafetyFactAssertion.id != assertion.id,
                    )
                    .with_for_update()
                )
            ).all()
            if len(previous) > 1:
                raise ValidationError(detail="multiple authoritative safety assertions exist for one field")
            if previous:
                old = previous[0]
                old.status = SafetyAssertionStatus.SUPERSEDED.value
                old.superseded_at = at
                assertion.supersedes_assertion_id = old.id
                await self._db.flush()
        assertion.status = SafetyAssertionStatus.CONFIRMED.value
        assertion.confirmed_by_actor_type = "doctor"
        assertion.confirmed_by_actor_id = actor_id
        assertion.confirmed_at = at
        if field in _PROJECTED_FIELDS:
            await self._project(assertion)

    async def _profile(self, session_id: uuid.UUID) -> SafetyProfile:
        row = await self._db.scalar(
            select(SafetyProfile).where(SafetyProfile.session_id == session_id).with_for_update()
        )
        if row is None:
            row = SafetyProfile(id=uuid.uuid4(), session_id=session_id)
            self._db.add(row)
            await self._db.flush()
        return row

    async def _project(self, assertion: SafetyFactAssertion) -> None:
        row = await self._profile(assertion.session_id)
        values = _profile_values(row)
        _apply_assertion(values, SafetyFactField(assertion.field_name), assertion.value)
        validated = SafetyProfileSchema.model_validate({"session_id": assertion.session_id, **values})
        _write_profile(row, validated)

    async def _clear_projection(self, assertion: SafetyFactAssertion) -> None:
        field = SafetyFactField(assertion.field_name)
        if field not in _PROJECTED_FIELDS:
            return
        row = await self._profile(assertion.session_id)
        values = _profile_values(row)
        _clear_field(values, field)
        validated = SafetyProfileSchema.model_validate({"session_id": assertion.session_id, **values})
        _write_profile(row, validated)


def _profile_values(row: SafetyProfile) -> dict[str, Any]:
    return {
        "allergy_collection_status": row.allergy_collection_status,
        "allergens": row.allergens,
        "pregnancy_collection_status": row.pregnancy_collection_status,
        "pregnancy_value": row.pregnancy_value,
        "lactation_collection_status": row.lactation_collection_status,
        "lactation_value": row.lactation_value,
        "medications_collection_status": row.medications_collection_status,
        "medications": row.medications,
        "major_conditions_collection_status": row.major_conditions_collection_status,
        "major_conditions": row.major_conditions,
        "contraindications_collection_status": row.contraindications_collection_status,
        "contraindications": row.contraindications,
    }


def _field_columns(field: SafetyFactField) -> tuple[str, str]:
    return {
        SafetyFactField.ALLERGY: ("allergy_collection_status", "allergens"),
        SafetyFactField.PREGNANCY: ("pregnancy_collection_status", "pregnancy_value"),
        SafetyFactField.LACTATION: ("lactation_collection_status", "lactation_value"),
        SafetyFactField.MEDICATIONS: ("medications_collection_status", "medications"),
        SafetyFactField.MAJOR_CONDITIONS: ("major_conditions_collection_status", "major_conditions"),
        SafetyFactField.CONTRAINDICATIONS: ("contraindications_collection_status", "contraindications"),
    }[field]


def _apply_assertion(values: dict[str, Any], field: SafetyFactField, assertion_value: dict[str, Any]) -> None:
    status_column, value_column = _field_columns(field)
    try:
        status = CollectionStatus(assertion_value["collection_status"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValidationError(detail="safety assertion collection status is invalid") from exc
    raw_value = assertion_value.get("values" if field not in {SafetyFactField.PREGNANCY, SafetyFactField.LACTATION} else "value")
    values[status_column] = status.value
    values[value_column] = raw_value if status is CollectionStatus.COLLECTED else None


def _clear_field(values: dict[str, Any], field: SafetyFactField) -> None:
    status_column, value_column = _field_columns(field)
    values[status_column] = CollectionStatus.UNKNOWN.value
    values[value_column] = None


def _write_profile(row: SafetyProfile, profile: SafetyProfileSchema) -> None:
    for name, value in profile.model_dump(mode="json", exclude={"session_id"}).items():
        setattr(row, name, value)


async def persist_intake_safety_assertions(
    *,
    session_id: uuid.UUID,
    source_message_id: uuid.UUID,
    output: IntakeExtractionOutput,
    extraction_run_id: uuid.UUID,
    template_version: str,
    source_kind: Literal["model_extraction", "deterministic_precheck"],
    trace_id: str,
) -> tuple[SafetyFactAssertionRead, ...]:
    """Persist intake proposals in a retry-safe transaction after Domain commit."""

    factory = get_session_factory()
    async with factory() as db, db.begin():
        source = await db.get(ConsultMessage, source_message_id)
        if source is None:
            raise ValidationError(detail="safety proposal source message is unavailable")
        return await SafetyConfirmationService(db).propose_from_intake(
            session_id=session_id,
            source_message=source,
            output=output,
            extraction_run_id=extraction_run_id,
            template_version=template_version,
            source_kind=source_kind,
            trace_id=trace_id,
        )


__all__ = ["SafetyConfirmationService", "persist_intake_safety_assertions"]
