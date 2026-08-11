"""Pure unit tests for the R9 repository ORM conversion and digest fail-closed.

These exercise ``_contract_record_values`` / ``_coverage_event_record_values`` /
``_contract_schema`` / ``_coverage_event_schema`` without any database: the ORM
records are constructed in memory (``created_at`` is a server default) and the
protected-table round trip is proven to preserve the immutable schema objects
byte-for-byte while every digest tampering is rejected.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent_runtime.repository import (
    _contract_record_values,
    _contract_schema,
    _coverage_event_record_values,
    _coverage_event_schema,
)
from app.models.question_contract import QuestionContractRecord, QuestionCoverageEventRecord
from app.schemas.question_contract import (
    CoverageCandidateItem,
    CoverageEvidenceCandidate,
    QuestionCoverageCandidate,
    build_coverage_event,
    build_question_contract,
)


def _contract(*, criteria: tuple[str, ...] = ("咳嗽性质", "痰液颜色", "痰液量")):
    return build_question_contract(
        session_id=uuid4(),
        question_message_id=uuid4(),
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension="ten_questions.respiratory",
        selection_kind="required",
        aspect_criteria=criteria,
    )


def _answered_event(contract, *, content: str = "有痰"):
    answer_id = uuid4()
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(
                aspect_id=contract.aspects[0].aspect_id,
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=answer_id,
                        start_char=0,
                        end_char=len(content),
                        quote=content,
                    ),
                ),
            ),
            *(
                CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered")
                for aspect in contract.aspects[1:]
            ),
        ),
    )
    return build_coverage_event(contract=contract, candidate=candidate, message_contents={answer_id: content})


def test_contract_record_values_round_trip_preserves_schema() -> None:
    contract = _contract()
    values = _contract_record_values(contract)
    record = QuestionContractRecord(**values)
    restored = _contract_schema(record)
    assert restored == contract
    assert restored.contract_digest == contract.contract_digest
    assert record.root_contract_id == contract.root_contract_id
    assert record.parent_contract_id is None
    assert record.revision == 1
    assert [item["criterion"] for item in record.aspects] == ["咳嗽性质", "痰液颜色", "痰液量"]


def test_followup_contract_record_values_round_trip_preserves_schema() -> None:
    root = _contract()
    followup = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="剩余信息目前能确认吗？",
        parent_contract=root,
        residual_aspects=tuple(root.aspects[1:]),
    )
    record = QuestionContractRecord(**_contract_record_values(followup))
    restored = _contract_schema(record)
    assert restored == followup
    assert record.root_contract_id == root.contract_id
    assert record.parent_contract_id == root.contract_id
    assert record.revision == 2


def test_coverage_event_record_values_round_trip_preserves_schema() -> None:
    contract = _contract()
    event = _answered_event(contract)
    values = _coverage_event_record_values(event)
    record = QuestionCoverageEventRecord(**values)
    restored = _coverage_event_schema(record)
    assert restored == event
    assert restored.event_digest == event.event_digest
    assert record.contract_id == contract.contract_id
    assert record.answer_message_id == event.answer_message_id
    # Persisted items must match the deterministic contract aspect order.
    assert [item["aspect_id"] for item in record.items] == [str(item.aspect_id) for item in contract.aspects]


def test_coverage_event_record_values_never_persist_raw_answer_text() -> None:
    """The protected event table keeps digests only; raw quotes never leave."""
    contract = _contract()
    content = "咳嗽三天，有痰，痰是黄色的"
    event = _answered_event(contract, content=content)
    values = _coverage_event_record_values(event)
    serialized = json.dumps(values, ensure_ascii=False, default=str)
    assert "quote_sha256" in serialized
    assert content not in serialized
    assert "咳嗽三天" not in serialized
    assert "黄色的" not in serialized


def test_tampered_contract_digest_fails_closed_on_load() -> None:
    contract = _contract()
    values = _contract_record_values(contract)
    values["contract_digest"] = "0" * 64
    record = QuestionContractRecord(**values)
    with pytest.raises(ValueError):
        _contract_schema(record)


def test_tampered_contract_aspect_criterion_fails_closed_on_load() -> None:
    contract = _contract()
    values = _contract_record_values(contract)
    values["aspects"][0] = {**values["aspects"][0], "criterion": "篡改的方面"}
    record = QuestionContractRecord(**values)
    with pytest.raises(ValueError):
        _contract_schema(record)


def test_tampered_contract_schema_version_fails_closed_on_load() -> None:
    contract = _contract()
    values = _contract_record_values(contract)
    values["schema_version"] = "question-contract.v9"
    record = QuestionContractRecord(**values)
    with pytest.raises(ValueError):
        _contract_schema(record)


def test_tampered_event_digest_fails_closed_on_load() -> None:
    contract = _contract()
    event = _answered_event(contract)
    values = _coverage_event_record_values(event)
    values["event_digest"] = "1" * 64
    record = QuestionCoverageEventRecord(**values)
    with pytest.raises(ValueError):
        _coverage_event_schema(record)


def test_tampered_evidence_quote_sha256_fails_closed_on_load() -> None:
    """quote_sha256 is covered by the canonical event digest, so mutating it
    breaks digest parity even though the raw quote is never stored."""
    contract = _contract()
    event = _answered_event(contract)
    values = _coverage_event_record_values(event)
    assert values["items"][0]["evidence"]
    values["items"][0]["evidence"] = [
        {**values["items"][0]["evidence"][0], "quote_sha256": "2" * 64}
    ]
    record = QuestionCoverageEventRecord(**values)
    with pytest.raises(ValueError):
        _coverage_event_schema(record)


def test_tampered_event_schema_version_fails_closed_on_load() -> None:
    contract = _contract()
    event = _answered_event(contract)
    values = _coverage_event_record_values(event)
    values["schema_version"] = "question-coverage-event.v9"
    record = QuestionCoverageEventRecord(**values)
    with pytest.raises(ValueError):
        _coverage_event_schema(record)


def test_schema_digest_rejects_are_pydantic_validation_failures() -> None:
    """Digest parity is re-verified through the DTO validators, so the failure
    is a ValidationError (a ValueError subclass) rather than a silent load."""
    contract = _contract()
    values = _contract_record_values(contract)
    values["contract_digest"] = "0" * 64
    with pytest.raises(ValidationError):
        _contract_schema(QuestionContractRecord(**values))
