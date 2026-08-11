"""Pure contract tests for the safety-fact confirmation boundary."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from app.agent_runtime.reducer import DomainState
from app.core.exceptions import ValidationError
from app.models.consult import ConsultMessage
from app.schemas.domain import CollectionStatus, SafetyProfileSchema
from app.schemas.intake import (
    EvidenceSpan,
    IntakeExtractionDecision,
    IntakeExtractionOutput,
    PatientSafetyDelta,
    SafetyListDelta,
)
from app.services.langgraph_intake import _intake_output_to_delta, _project_explicit_none_safety
from app.services.safety_confirmation import _evidence_refs


def _explicit_none_delta(message_id: uuid.UUID) -> PatientSafetyDelta:
    return PatientSafetyDelta(
        allergy=SafetyListDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=message_id,
            negation_span=EvidenceSpan(
                source_message_id=message_id,
                start_char=0,
                end_char=2,
                quote="没有",
            ),
        )
    )


def _collected_delta(message_id: uuid.UUID, allergen: str = "青霉素") -> PatientSafetyDelta:
    return PatientSafetyDelta(
        allergy=SafetyListDelta(
            status=CollectionStatus.COLLECTED,
            values=(allergen,),
            source_message_id=message_id,
            value_spans=(
                EvidenceSpan(
                    source_message_id=message_id,
                    start_char=0,
                    end_char=len(allergen),
                    quote=allergen,
                ),
            ),
        )
    )


def _output(message_id: uuid.UUID, *, quote: str = "青霉素") -> IntakeExtractionOutput:
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.EXTRACTED,
        patient_safety_delta=PatientSafetyDelta(
            allergy=SafetyListDelta(
                status=CollectionStatus.COLLECTED,
                values=("青霉素",),
                source_message_id=message_id,
                value_spans=(
                    EvidenceSpan(
                        source_message_id=message_id,
                        start_char=2,
                        end_char=5,
                        quote=quote,
                    ),
                ),
            )
        ),
    )


def test_model_safety_candidate_never_enters_domain_delta() -> None:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    state = DomainState(session_id=session_id, state_version=1)

    delta = _intake_output_to_delta(
        run_id=uuid.uuid4(),
        session_id=session_id,
        expected_state_version=1,
        source_message_id=message_id,
        state=state,
        observations=(),
        safety_delta=_output(message_id).patient_safety_delta,
    )

    assert delta.safety_profile is None
    assert len(delta.artifact_revisions) == 1
    assert delta.artifact_revisions[0].artifact_type == "intake_noop"


def test_unknown_profile_projects_deterministic_explicit_none() -> None:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    projected = _project_explicit_none_safety(
        None,
        _explicit_none_delta(message_id),
        session_id=session_id,
    )
    assert projected is not None
    assert projected.allergy_collection_status is CollectionStatus.EXPLICITLY_NONE
    assert projected.allergens is None
    # Other safety fields stay untouched (UNKNOWN here because there was no profile).
    assert projected.medications_collection_status is CollectionStatus.UNKNOWN


def test_explicit_none_after_explicit_none_is_noop_no_state_churn() -> None:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    current = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.EXPLICITLY_NONE,
    )
    projected = _project_explicit_none_safety(current, _explicit_none_delta(message_id), session_id=session_id)
    assert projected is None


def test_later_explicit_none_clears_previous_positive_value() -> None:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    current = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.COLLECTED,
        allergens=["青霉素"],
    )
    projected = _project_explicit_none_safety(current, _explicit_none_delta(message_id), session_id=session_id)
    assert projected is not None
    assert projected.allergy_collection_status is CollectionStatus.EXPLICITLY_NONE
    assert projected.allergens is None


def test_positive_candidate_does_not_overwrite_explicit_none() -> None:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    current = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.EXPLICITLY_NONE,
    )
    projected = _project_explicit_none_safety(current, _collected_delta(message_id), session_id=session_id)
    assert projected is None


def test_model_collected_candidate_never_projects_a_fresh_profile() -> None:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    projected = _project_explicit_none_safety(None, _collected_delta(message_id), session_id=session_id)
    assert projected is None


def test_empty_delta_with_existing_profile_is_noop_none_and_does_not_mutate() -> None:
    session_id = uuid.uuid4()
    current = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.COLLECTED,
        allergens=["青霉素"],
    )
    # An empty PatientSafetyDelta carries no candidate at all: the projection
    # must be a no-op None (a delta projection, not the current stored profile),
    # so the reducer never sees a spurious safety change.
    projected = _project_explicit_none_safety(current, PatientSafetyDelta(), session_id=session_id)
    assert projected is None
    # The input profile is untouched.
    assert current.allergy_collection_status is CollectionStatus.COLLECTED
    assert current.allergens == ["青霉素"]


def test_evidence_reference_keeps_digest_and_coordinates_not_raw_quote() -> None:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    message = ConsultMessage(
        id=message_id,
        session_id=session_id,
        role="patient_proxy",
        stage="inquiry",
        content="我对青霉素过敏",
        created_at=datetime.now(UTC),
    )
    span = _output(message_id).patient_safety_delta.allergy.value_spans
    assert span is not None

    refs = _evidence_refs(span, message)

    assert refs == [
        {
            "source_message_id": str(message_id),
            "start_char": 2,
            "end_char": 5,
            "quote_digest": hashlib.sha256("青霉素".encode()).hexdigest(),
        }
    ]
    assert "青霉素" not in str(refs)


def test_evidence_reference_rejects_tampered_quote() -> None:
    message_id = uuid.uuid4()
    message = ConsultMessage(
        id=message_id,
        session_id=uuid.uuid4(),
        role="patient_proxy",
        stage="inquiry",
        content="我对头孢过敏",
        created_at=datetime.now(UTC),
    )
    spans = _output(message_id).patient_safety_delta.allergy.value_spans
    assert spans is not None

    with pytest.raises(ValidationError) as exc_info:
        _evidence_refs(spans, message)
    assert exc_info.value.detail is not None and "does not match" in exc_info.value.detail


def test_evidence_reference_rejects_cross_message_span() -> None:
    message_id = uuid.uuid4()
    message = ConsultMessage(
        id=message_id,
        session_id=uuid.uuid4(),
        role="patient_proxy",
        stage="inquiry",
        content="我对青霉素过敏",
        created_at=datetime.now(UTC),
    )
    spans = _output(uuid.uuid4()).patient_safety_delta.allergy.value_spans
    assert spans is not None

    with pytest.raises(ValidationError) as exc_info:
        _evidence_refs(spans, message)
    assert exc_info.value.detail is not None and "different source" in exc_info.value.detail
