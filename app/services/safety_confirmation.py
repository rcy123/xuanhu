"""Durable safety-fact proposal, confirmation, and projection service."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.completeness_policy import (
    completeness_to_gate_result_schema,
    evaluate_completeness_policy,
)
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION
from app.core.config import get_settings
from app.agent_runtime.repository import SafetyFactAssertionSpec, SafetyFactEvidenceSpec
from app.agent_runtime.triage_policy import evaluate_triage_policy, to_gate_result_schema
from app.agents.question_composer import compose_question
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
from app.models.domain import (
    GateResult,
    GraphRun,
    GraphRunStep,
    Observation,
    OutboxEvent,
    SafetyFactAssertion,
    SafetyFactTransition,
    SafetyProfile,
)
from app.schemas.completeness import (
    CompletenessDisposition,
    CompletenessDomainSnapshot,
    CompletenessObservationFact,
    CompletenessPolicyInput,
    CompletenessProgress,
    CompletenessSafetyProfile,
    InquiryDimension,
)
from app.schemas.domain import CollectionStatus, GateResultSchema, ObservationStatus, SafetyProfileSchema
from app.schemas.intake import (
    EvidenceSpan,
    IntakeExtractionOutput,
    LactationDelta,
    PatientSafetyDelta,
    PregnancyDelta,
)
from app.schemas.question import QuestionComposerResult, QuestionCompositionStatus
from app.schemas.safety_confirmation import (
    SafetyAssertionStatus,
    SafetyEvidenceRef,
    SafetyFactAssertionList,
    SafetyFactAssertionRead,
    SafetyFactField,
)
from app.schemas.triage import TriageDisposition, TriagePolicyInput

SafetyAction = Literal["confirm", "reject", "retract"]
SAFETY_RECOMPUTE_EVENT = "safety_confirmation.recomputed.v1"
SAFETY_RECOMPUTE_VERSION = "safety-confirmation-recompute.v1"
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
_SAFETY_FIELD_DIMENSIONS = {
    SafetyFactField.ALLERGY: InquiryDimension.ALLERGY_STATUS,
    SafetyFactField.PREGNANCY: InquiryDimension.PREGNANCY_STATUS,
    SafetyFactField.LACTATION: InquiryDimension.LACTATION_STATUS,
    SafetyFactField.MEDICATIONS: InquiryDimension.MEDICATION_STATUS,
    SafetyFactField.MAJOR_CONDITIONS: InquiryDimension.MAJOR_CONDITION_STATUS,
}


@dataclass(frozen=True, slots=True)
class _Proposal:
    field_name: SafetyFactField
    value: dict[str, Any]
    evidence_spans: tuple[EvidenceSpan, ...]


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_CODE_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _normalized_code(value: Any) -> str | None:
    raw: Any = value
    if isinstance(value, dict):
        raw = value.get("normalized_code") or value.get("code") or value.get("value")
    if isinstance(raw, bool):
        raw = "true" if raw else "false"
    if isinstance(raw, int | float):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    code = _CODE_RE.sub("_", raw.strip().lower()).strip("_")
    return code[:64] or None


def _stable_transition_id(transition_id: uuid.UUID, kind: str) -> uuid.UUID:
    return uuid.uuid5(transition_id, f"{SAFETY_RECOMPUTE_VERSION}:{kind}")


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
    *,
    field_name: SafetyFactField | None = None,
    require_reply_binding: bool = False,
) -> list[dict[str, Any]]:
    reply_question_id: uuid.UUID | None = None
    reply_dimension: InquiryDimension | None = None
    if require_reply_binding:
        structured = source_message.structured_delta if isinstance(source_message.structured_delta, dict) else {}
        raw_context = structured.get("reply_context")
        if not isinstance(raw_context, dict):
            raise ValidationError(detail="deterministic safety reply is missing its question binding")
        try:
            reply_question_id = uuid.UUID(str(raw_context["question_message_id"]))
            reply_dimension = InquiryDimension(str(raw_context["selected_dimension"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(detail="deterministic safety reply binding is invalid") from exc
        if field_name is None or _SAFETY_FIELD_DIMENSIONS.get(field_name) is not reply_dimension:
            raise ValidationError(detail="deterministic safety reply dimension does not match its field")

    refs: list[dict[str, Any]] = []
    for span in spans:
        if span.source_message_id != source_message.id:
            raise ValidationError(detail="safety evidence belongs to a different source message")
        if span.end_char > len(source_message.content):
            raise ValidationError(detail="safety evidence range exceeds its source message")
        quote = source_message.content[span.start_char : span.end_char]
        if quote != span.quote:
            raise ValidationError(detail="safety evidence does not match its source message")
        ref: dict[str, Any] = {
            "source_message_id": str(source_message.id),
            "start_char": span.start_char,
            "end_char": span.end_char,
            "quote_digest": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        }
        if reply_question_id is not None and reply_dimension is not None:
            ref.update(
                {
                    "reply_to_question_message_id": str(reply_question_id),
                    "reply_dimension": reply_dimension.value,
                }
            )
        refs.append(ref)
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


def build_intake_safety_assertion_specs(
    *,
    session_id: uuid.UUID,
    source_message: ConsultMessage,
    output: IntakeExtractionOutput,
    extraction_run_id: uuid.UUID,
    template_version: str,
    source_kind: Literal[
        "model_extraction",
        "deterministic_precheck",
        "deterministic_reply_binding",
    ],
    trace_id: str,
) -> tuple[SafetyFactAssertionSpec, ...]:
    """Build verified, content-addressed intake proposals without database I/O."""

    if source_message.session_id != session_id:
        raise ValidationError(detail="safety proposal source belongs to a different session")
    specs: list[SafetyFactAssertionSpec] = []
    for proposal in _intake_proposals(output):
        evidence_dicts = _evidence_refs(
            proposal.evidence_spans,
            source_message,
            field_name=proposal.field_name,
            require_reply_binding=source_kind == "deterministic_reply_binding",
        )
        if not evidence_dicts:
            raise ValidationError(detail="extracted safety facts require grounded evidence")
        evidence = tuple(SafetyFactEvidenceSpec.model_validate(item) for item in evidence_dicts)
        evidence_payload = [item.model_dump(mode="json", exclude_none=True) for item in evidence]
        value_digest = _canonical_digest(proposal.value)
        evidence_digest = _canonical_digest(evidence_payload)
        fingerprint = _assertion_fingerprint(
            session_id=session_id,
            field_name=proposal.field_name.value,
            value_digest=value_digest,
            source_kind=source_kind,
            source_message_id=source_message.id,
            extraction_run_id=extraction_run_id,
            template_version=template_version,
            evidence_digest=evidence_digest,
        )
        assertion_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"xuanhu:safety-assertion:{session_id}:{fingerprint}",
        )
        audit_actor_type: Literal["agent", "system"] = (
            "agent" if source_kind == "model_extraction" else "system"
        )
        proposed_by_actor_type: Literal["model", "system"] = (
            "model" if source_kind == "model_extraction" else "system"
        )
        specs.append(
            SafetyFactAssertionSpec(
                assertion_id=assertion_id,
                session_id=session_id,
                field_name=proposal.field_name.value,
                value=proposal.value,
                value_digest=value_digest,
                assertion_fingerprint=fingerprint,
                source_kind=source_kind,
                source_message_id=source_message.id,
                extraction_run_id=extraction_run_id,
                template_version=template_version,
                evidence_spans=evidence,
                evidence_digest=evidence_digest,
                proposed_by_actor_type=proposed_by_actor_type,
                audit_event_id=uuid.uuid5(assertion_id, "safety-fact-proposal-audit.v1"),
                audit_actor_type=audit_actor_type,
                audit_trace_id=trace_id[:64],
            )
        )
    return tuple(specs)


def _proposal_audit_payload(spec: SafetyFactAssertionSpec) -> dict[str, object]:
    return {
        "assertion_id": str(spec.assertion_id),
        "field_name": spec.field_name,
        "status": spec.status,
        "value_digest": spec.value_digest,
        "evidence_digest": spec.evidence_digest,
        "source_message_id": str(spec.source_message_id),
        "extraction_run_id": str(spec.extraction_run_id),
        "template_version": spec.template_version,
    }


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
        source_kind: Literal[
            "model_extraction",
            "deterministic_precheck",
            "deterministic_reply_binding",
        ] = "model_extraction",
        trace_id: str,
    ) -> tuple[SafetyFactAssertionRead, ...]:
        """Persist grounded candidates without changing authoritative safety state."""

        specs = build_intake_safety_assertion_specs(
            session_id=session_id,
            source_message=source_message,
            output=output,
            extraction_run_id=extraction_run_id,
            template_version=template_version,
            source_kind=source_kind,
            trace_id=trace_id,
        )
        result: list[SafetyFactAssertionRead] = []
        for spec in specs:
            row, created = await self._insert_assertion(
                session_id=session_id,
                field_name=SafetyFactField(spec.field_name),
                value=dict(spec.value),
                source_kind=spec.source_kind,
                source_message_id=spec.source_message_id,
                extraction_run_id=spec.extraction_run_id,
                template_version=spec.template_version,
                evidence_spans=[
                    item.model_dump(mode="json", exclude_none=True)
                    for item in spec.evidence_spans
                ],
                evidence_digest=spec.evidence_digest,
                proposed_by_actor_type=spec.proposed_by_actor_type,
                proposed_by_actor_id=spec.proposed_by_actor_id,
                status=SafetyAssertionStatus.PROPOSED,
            )
            if row.id != spec.assertion_id:
                raise ValidationError(detail="stable safety assertion identifier mismatch")
            if created:
                self._db.add(
                    AuditEvent(
                        id=spec.audit_event_id,
                        session_id=session_id,
                        event_type=spec.audit_event_type,
                        actor_type=spec.audit_actor_type,
                        actor_id=spec.audit_actor_id,
                        payload=_proposal_audit_payload(spec),
                        trace_id=spec.audit_trace_id,
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
        if session.status != "active" or session.current_stage != "inquiry":
            raise InvalidStageTransitionError(
                detail=(
                    "safety-fact decisions require an active inquiry session; "
                    f"status={session.status} current_stage={session.current_stage}"
                )
            )

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
        if SafetyFactField(assertion.field_name) is SafetyFactField.RED_FLAG:
            raise InvalidStageTransitionError(
                detail=(
                    "red-flag candidates are owned by triage/recovery and cannot be "
                    "confirmed, rejected, or retracted as generic safety facts"
                )
            )

        target_status = {
            "confirm": SafetyAssertionStatus.CONFIRMED,
            "reject": SafetyAssertionStatus.REJECTED,
            "retract": SafetyAssertionStatus.RETRACTED,
        }[action]
        if assertion.status == target_status.value:
            # A duplicate with a different public key can be a response-loss
            # retry. Return the already authoritative result without inventing
            # a second doctor decision or contradictory audit identity.
            return _read(assertion)

        changed = False
        now = datetime.now(UTC)
        if action == "confirm":
            if assertion.status == SafetyAssertionStatus.PROPOSED.value:
                await self._verify_integrity(assertion)
                await self._confirm(assertion, actor_id=actor_id, at=now)
                changed = True
            elif assertion.status != SafetyAssertionStatus.CONFIRMED.value:
                raise InvalidStageTransitionError(detail=f"cannot confirm a {assertion.status} assertion")
            resulting_status = target_status
        elif action == "reject":
            if assertion.status == SafetyAssertionStatus.PROPOSED.value:
                assertion.status = SafetyAssertionStatus.REJECTED.value
                assertion.rejected_by_actor_type = "doctor"
                assertion.rejected_by_actor_id = actor_id
                assertion.rejected_at = now
                changed = True
            elif assertion.status != SafetyAssertionStatus.REJECTED.value:
                raise InvalidStageTransitionError(detail=f"cannot reject a {assertion.status} assertion")
            resulting_status = target_status
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
            resulting_status = target_status

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
        if changed:
            await self._recompute_after_transition(
                session=session,
                assertion=assertion,
                transition=transition,
                action=action,
                context=context,
            )
            await self._db.flush()
        return _read(assertion)

    async def _recompute_after_transition(
        self,
        *,
        session: ConsultSession,
        assertion: SafetyFactAssertion,
        transition: SafetyFactTransition,
        action: SafetyAction,
        context: WriteRequestContext,
    ) -> None:
        """Re-run inquiry gates and scheduling without inventing a patient turn."""

        domain_snapshot = await self._completeness_snapshot(session)
        triage_result = evaluate_triage_policy(
            TriagePolicyInput(
                input_state_version=session.state_version,
                red_flag_candidates=(),
            )
        )
        progress = _progress_from_snapshot(session.state_snapshot)
        if action == "confirm":
            # A doctor-confirmed projection is authoritative new information,
            # so it resets patient-turn stagnation without counting a new turn.
            progress = progress.model_copy(update={"no_new_facts_rounds": 0})
        completeness_result = evaluate_completeness_policy(
            CompletenessPolicyInput(
                input_state_version=session.state_version,
                domain_snapshot=domain_snapshot,
                triage_gate=triage_result.gate_result,
                progress=progress,
                # 2c 灰度: 槽位口径与主路径一致。
                slot_based=get_settings().intake_slot_path_enabled,
            )
        )
        pending_dimensions = await self._pending_safety_dimensions(session.id)
        outstanding_question = await self._outstanding_question(session)
        question_result: QuestionComposerResult | None = None
        if outstanding_question is None:
            outcome = await compose_question(
                completeness_result=completeness_result,
                pending_safety_dimensions=pending_dimensions,
            )
            if outcome.status is QuestionCompositionStatus.SUCCEEDED:
                if outcome.result is None:
                    raise ValidationError(detail="safety confirmation question result is missing")
                question_result = outcome.result
            elif outcome.status is not QuestionCompositionStatus.NO_QUESTION:
                raise ValidationError(
                    detail=(
                        "safety confirmation question composition failed: "
                        f"{outcome.failure_code or 'unknown'}"
                    )
                )

        triage_gate = to_gate_result_schema(triage_result)
        completeness_gate = completeness_to_gate_result_schema(completeness_result)
        await self._persist_recompute(
            session=session,
            assertion=assertion,
            transition=transition,
            action=action,
            context=context,
            triage_disposition=triage_result.disposition,
            completeness_disposition=completeness_result.disposition,
            triage_gate=triage_gate,
            completeness_gate=completeness_gate,
            progress=progress,
            pending_dimensions=pending_dimensions,
            question_result=question_result,
            outstanding_question=outstanding_question,
        )

    async def _outstanding_question(self, session: ConsultSession) -> ConsultMessage | None:
        snapshot = session.state_snapshot if isinstance(session.state_snapshot, dict) else {}
        intake = snapshot.get("langgraph_intake")
        raw_question_id = intake.get("last_question_message_id") if isinstance(intake, dict) else None
        if not raw_question_id:
            return None
        try:
            question_id = uuid.UUID(str(raw_question_id))
        except ValueError:
            return None
        question = await self._db.get(ConsultMessage, question_id)
        if (
            question is None
            or question.session_id != session.id
            or question.role != "agent"
            or question.agent_name != "question_composer"
            or question.stage != "inquiry"
        ):
            return None
        return question

    async def _completeness_snapshot(
        self,
        session: ConsultSession,
    ) -> CompletenessDomainSnapshot:
        observation_rows = (
            await self._db.scalars(
                select(Observation)
                .where(Observation.session_id == session.id)
                .order_by(Observation.created_at, Observation.id)
            )
        ).all()
        facts = tuple(
            CompletenessObservationFact(
                observation_id=row.id,
                session_id=row.session_id,
                fact_key=row.fact_key,
                value_fingerprint=_canonical_digest(
                    row.normalized_value if row.normalized_value is not None else row.value
                ),
                normalized_code=_normalized_code(
                    row.normalized_value if row.normalized_value is not None else row.value
                ),
                status=ObservationStatus(row.status),
                supersedes_observation_id=row.supersedes_observation_id,
            )
            for row in observation_rows
        )
        profile = await self._db.scalar(
            select(SafetyProfile).where(SafetyProfile.session_id == session.id)
        )
        safety_profile = (
            None
            if profile is None
            else CompletenessSafetyProfile(
                session_id=session.id,
                allergy_collection_status=CollectionStatus(profile.allergy_collection_status),
                allergen_count=len(profile.allergens or ()),
                pregnancy_collection_status=CollectionStatus(profile.pregnancy_collection_status),
                lactation_collection_status=CollectionStatus(profile.lactation_collection_status),
                medications_collection_status=CollectionStatus(profile.medications_collection_status),
                medication_count=len(profile.medications or ()),
                major_conditions_collection_status=CollectionStatus(
                    profile.major_conditions_collection_status
                ),
                major_condition_count=len(profile.major_conditions or ()),
                contraindications_collection_status=CollectionStatus(
                    profile.contraindications_collection_status
                ),
                contraindication_count=len(profile.contraindications or ()),
            )
        )
        return CompletenessDomainSnapshot(
            session_id=session.id,
            state_version=session.state_version,
            observations=facts,
            safety_profile=safety_profile,
        )

    async def _pending_safety_dimensions(
        self,
        session_id: uuid.UUID,
    ) -> tuple[InquiryDimension, ...]:
        pending_fields = set(
            await self._db.scalars(
                select(SafetyFactAssertion.field_name).where(
                    SafetyFactAssertion.session_id == session_id,
                    SafetyFactAssertion.status == SafetyAssertionStatus.PROPOSED.value,
                )
            )
        )
        return tuple(
            sorted(
                {
                    dimension
                    for field_name in pending_fields
                    if (dimension := _SAFETY_FIELD_DIMENSIONS.get(SafetyFactField(field_name)))
                    is not None
                },
                key=lambda item: item.value,
            )
        )

    async def _persist_recompute(
        self,
        *,
        session: ConsultSession,
        assertion: SafetyFactAssertion,
        transition: SafetyFactTransition,
        action: SafetyAction,
        context: WriteRequestContext,
        triage_disposition: TriageDisposition,
        completeness_disposition: CompletenessDisposition,
        triage_gate: GateResultSchema,
        completeness_gate: GateResultSchema,
        progress: CompletenessProgress,
        pending_dimensions: tuple[InquiryDimension, ...],
        question_result: QuestionComposerResult | None,
        outstanding_question: ConsultMessage | None,
    ) -> None:
        run_id = _stable_transition_id(transition.id, "run")
        completed_at = datetime.now(UTC)
        graph_run = GraphRun(
            id=run_id,
            session_id=session.id,
            graph_version=DEFAULT_GRAPH_VERSION,
            command_id=f"safety-confirmation:{transition.id}",
            input_state_version=session.state_version,
            status="completed",
            completed_at=completed_at,
        )
        self._db.add(graph_run)
        # These persistence models deliberately have no ORM relationships;
        # flush the parent explicitly so FK insert ordering is unambiguous.
        await self._db.flush([graph_run])

        question_message: ConsultMessage | None = None
        if question_result is not None:
            question_id = _stable_transition_id(transition.id, "question")
            question_message = ConsultMessage(
                id=question_id,
                session_id=session.id,
                role="agent",
                stage="inquiry",
                agent_name="question_composer",
                content=question_result.question,
                structured_delta=question_result.model_dump(mode="json"),
                trace_id=context.trace_id[:64],
            )
            self._db.add(question_message)

        step_names = (
            "safety_fact_transition",
            "triage_gate",
            "completeness_gate",
            "preserve_outstanding_question"
            if outstanding_question is not None
            else "compose_question"
            if question_message is not None
            else "compose_question:no_question",
            f"route:{completeness_disposition.value}",
        )
        for index, step_name in enumerate(step_names):
            self._db.add(
                GraphRunStep(
                    id=_stable_transition_id(transition.id, f"step:{index}"),
                    graph_run_id=run_id,
                    step_index=index,
                    step_name=step_name,
                    status="completed",
                    step_metadata={"recompute_version": SAFETY_RECOMPUTE_VERSION},
                )
            )
        for kind, gate in (("triage", triage_gate), ("completeness", completeness_gate)):
            self._db.add(
                GateResult(
                    id=_stable_transition_id(transition.id, f"gate:{kind}"),
                    session_id=session.id,
                    graph_run_id=run_id,
                    gate_name=gate.gate_name,
                    policy_version=gate.policy_version,
                    input_state_version=gate.input_state_version,
                    decision=gate.decision.value,
                    details=gate.details,
                )
            )

        next_progress = (
            progress.model_copy(update={"followup_rounds": progress.followup_rounds + 1})
            if question_message is not None
            else progress
        )
        _apply_recompute_session_state(
            session,
            run_id=run_id,
            assertion=assertion,
            triage_disposition=triage_disposition,
            completeness_disposition=completeness_disposition,
            triage_gate=triage_gate,
            completeness_gate=completeness_gate,
            progress=next_progress,
            pending_dimensions=pending_dimensions,
            active_question_message_id=(
                question_message.id
                if question_message is not None
                else outstanding_question.id
                if outstanding_question is not None
                else None
            ),
            trace_id=context.trace_id,
        )

        if question_message is not None and question_result is not None:
            self._db.add(
                AuditEvent(
                    session_id=session.id,
                    event_type="message.created",
                    actor_type="agent",
                    actor_id="question_composer",
                    payload={
                        "message_id": str(question_message.id),
                        "role": "agent",
                        "agent_name": "question_composer",
                        "stage": "inquiry",
                        "content_length": len(question_message.content),
                        "selected_dimension": question_result.selected_dimension.value,
                        "state_version": session.state_version,
                        "trigger": SAFETY_RECOMPUTE_VERSION,
                    },
                    trace_id=context.trace_id[:64],
                )
            )
        self._db.add(
            AuditEvent(
                session_id=session.id,
                event_type=SAFETY_RECOMPUTE_EVENT,
                actor_type="system",
                actor_id=None,
                payload={
                    "assertion_id": str(assertion.id),
                    "transition_id": str(transition.id),
                    "action": action,
                    "run_id": str(run_id),
                    "state_version": session.state_version,
                    "triage_disposition": triage_disposition.value,
                    "completeness_disposition": completeness_disposition.value,
                    "question_message_id": (
                        str(question_message.id) if question_message is not None else None
                    ),
                },
                trace_id=context.trace_id[:64],
            )
        )
        self._db.add(
            OutboxEvent(
                id=_stable_transition_id(transition.id, "outbox"),
                event_type=SAFETY_RECOMPUTE_EVENT,
                session_id=session.id,
                graph_run_id=run_id,
                state_version=session.state_version,
                trace_id=f"trace:{hashlib.sha256(context.trace_id.encode('utf-8')).hexdigest()}",
                payload={
                    "session_id": str(session.id),
                    "state_version": session.state_version,
                    "stage": session.current_stage,
                    "blocked_reason": session.blocked_reason,
                    "run_id": str(run_id),
                    "action": action,
                    "assertion_id": str(assertion.id),
                    "completeness_disposition": completeness_disposition.value,
                    "question_message_id": (
                        str(question_message.id) if question_message is not None else None
                    ),
                },
                status="pending",
                attempt_count=0,
            )
        )

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
            refs.append(ref.model_dump(mode="json", exclude_none=True))
        if not refs or _canonical_digest(refs) != assertion.evidence_digest:
            raise ValidationError(detail="safety assertion evidence digest mismatch")
        if assertion.source_kind == "deterministic_reply_binding":
            await self._verify_reply_binding(
                assertion,
                source,
                tuple(SafetyEvidenceRef.model_validate(item) for item in assertion.evidence_spans),
            )

    async def _verify_reply_binding(
        self,
        assertion: SafetyFactAssertion,
        source: ConsultMessage,
        refs: tuple[SafetyEvidenceRef, ...],
    ) -> None:
        expected_dimension = _SAFETY_FIELD_DIMENSIONS.get(SafetyFactField(assertion.field_name))
        if expected_dimension is None or not refs:
            raise ValidationError(detail="deterministic safety reply field is not bindable")
        question_ids = {item.reply_to_question_message_id for item in refs}
        dimensions = {item.reply_dimension for item in refs}
        if (
            len(question_ids) != 1
            or None in question_ids
            or dimensions != {expected_dimension.value}
        ):
            raise ValidationError(detail="deterministic safety reply evidence binding is inconsistent")
        question_id = next(iter(question_ids))
        assert question_id is not None

        source_metadata = source.structured_delta if isinstance(source.structured_delta, dict) else {}
        raw_context = source_metadata.get("reply_context")
        if (
            source_metadata.get("binding_version") != "intake-reply-binding.v1"
            or not isinstance(raw_context, dict)
            or str(raw_context.get("question_message_id")) != str(question_id)
            or raw_context.get("selected_dimension") != expected_dimension.value
        ):
            raise ValidationError(detail="deterministic safety reply source binding was tampered with")

        question = await self._db.get(ConsultMessage, question_id)
        structured = question.structured_delta if question is not None else None
        if (
            question is None
            or question.session_id != assertion.session_id
            or question.role != "agent"
            or question.agent_name != "question_composer"
            or question.stage != "inquiry"
            or not isinstance(structured, dict)
            or structured.get("selected_dimension") != expected_dimension.value
        ):
            raise ValidationError(detail="deterministic safety reply question binding is invalid")

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


def _progress_from_snapshot(snapshot: dict[str, Any] | None) -> CompletenessProgress:
    raw: dict[str, Any] = {}
    if isinstance(snapshot, dict):
        intake = snapshot.get("langgraph_intake")
        if isinstance(intake, dict) and isinstance(intake.get("progress"), dict):
            raw = dict(intake["progress"])
    return CompletenessProgress.model_validate(raw)


def _apply_recompute_session_state(
    session: ConsultSession,
    *,
    run_id: uuid.UUID,
    assertion: SafetyFactAssertion,
    triage_disposition: TriageDisposition,
    completeness_disposition: CompletenessDisposition,
    triage_gate: GateResultSchema,
    completeness_gate: GateResultSchema,
    progress: CompletenessProgress,
    pending_dimensions: tuple[InquiryDimension, ...],
    active_question_message_id: uuid.UUID | None,
    trace_id: str,
) -> None:
    routable = {
        CompletenessDisposition.READY,
        CompletenessDisposition.INCOMPLETE,
        CompletenessDisposition.CONFLICT,
    }
    if completeness_disposition in routable:
        session.current_stage = "inquiry"
        session.status = "active"
        session.recovery_status = "normal"
        session.blocked_reason = None
        session.blocked_at = None
    else:
        session.current_stage = "blocked"
        session.status = "blocked"
        session.recovery_status = "manual_required"
        session.blocked_reason = (
            f"triage_hold:{triage_disposition.value}"
            if completeness_disposition is CompletenessDisposition.TRIAGE_BLOCKED
            else "intake_stagnated_manual_required"
        )
        session.blocked_at = datetime.now(UTC).replace(tzinfo=None)

    details = completeness_gate.details or {}
    missing_required = {str(item) for item in details.get("missing_required") or ()}
    pending_values = {item.value for item in pending_dimensions}
    awaiting_confirmation = (
        completeness_disposition is CompletenessDisposition.INCOMPLETE
        and active_question_message_id is None
        and bool(missing_required)
        and missing_required <= pending_values
    )
    dialogue_status = (
        "awaiting_safety_confirmation"
        if awaiting_confirmation
        else "questioning"
        if active_question_message_id is not None
        else "complete"
        if completeness_disposition is CompletenessDisposition.READY
        else "manual_required"
    )

    snapshot = dict(session.state_snapshot or {})
    previous_intake = snapshot.get("langgraph_intake")
    intake = dict(previous_intake) if isinstance(previous_intake, dict) else {}
    last_patient_message_id = intake.get("last_patient_message_id") or str(assertion.source_message_id)
    intake.update(
        {
            "version": "intake-subgraph.v1",
            "last_run_id": str(run_id),
            "last_patient_message_id": last_patient_message_id,
            "last_question_message_id": (
                str(active_question_message_id) if active_question_message_id is not None else None
            ),
            "triage": {
                "decision": triage_gate.decision.value,
                "policy_version": triage_gate.policy_version,
                "disposition": (triage_gate.details or {}).get("disposition"),
            },
            "completeness": {
                "decision": completeness_gate.decision.value,
                "policy_version": completeness_gate.policy_version,
                "disposition": (completeness_gate.details or {}).get("disposition"),
            },
            "progress": progress.model_dump(mode="json"),
            "dialogue_status": dialogue_status,
            "pending_safety_dimensions": [item.value for item in pending_dimensions],
            "trace_id": trace_id,
            "recompute_version": SAFETY_RECOMPUTE_VERSION,
        }
    )
    snapshot.update(
        {
            "agent_runtime": "langgraph",
            "current_stage": session.current_stage,
            "state_version": session.state_version,
            "recovery_status": session.recovery_status,
            "blocked_reason": session.blocked_reason,
            "sufficiency_report": {
                "sufficient": completeness_disposition is CompletenessDisposition.READY,
                "covered": list(details.get("covered_dimensions") or ()),
                "missing": list(details.get("missing_required") or ()),
                "suggestions": [],
            },
            "langgraph_intake": intake,
        }
    )
    session.state_snapshot = snapshot


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
    source_kind: Literal[
        "model_extraction",
        "deterministic_precheck",
        "deterministic_reply_binding",
    ],
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


__all__ = [
    "SafetyConfirmationService",
    "build_intake_safety_assertion_specs",
    "persist_intake_safety_assertions",
]
