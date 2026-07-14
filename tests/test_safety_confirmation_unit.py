"""Pure contract tests for the safety-fact confirmation boundary."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from app.agent_runtime.reducer import DomainState
from app.core.exceptions import ValidationError
from app.models.consult import ConsultMessage
from app.schemas.domain import CollectionStatus
from app.schemas.intake import (
    EvidenceSpan,
    IntakeExtractionDecision,
    IntakeExtractionOutput,
    PatientSafetyDelta,
    SafetyListDelta,
)
from app.services.langgraph_intake import _intake_output_to_delta
from app.services.safety_confirmation import _evidence_refs


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
