"""Focused tests for the R9 intake coverage-binding verifier.

Drills ``_verify_coverage_binding`` on Unicode exact spans, contract/answer
binding, aspect-set equality, message scope, and the legacy no-contract path.
One end-to-end ``verify_intake_artifact`` test proves the verifier is wired into
the intake chain.  No model gateway or database is required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent_runtime.intake_verifier import (
    INTAKE_AGENT_NAME,
    INTAKE_AGENT_VERSION,
    INTAKE_POLICY_VERSION,
    INTAKE_PROMPT_VERSION,
    INTAKE_VERIFIER_CHAIN,
    IntakeVerificationFailureCode,
    IntakeVerifierName,
    _verify_coverage_binding,
    verify_intake_artifact,
)
from app.agent_runtime.specs import (
    AgentSpec,
    Capability,
    FailurePolicy,
    ModelPolicy,
    RunArtifact,
    RunSpec,
)
from app.schemas.intake import (
    IntakeExtractionDecision,
    IntakeExtractionInput,
    IntakeExtractionOutput,
    IntakeMessage,
    IntakeMessageRole,
)
from app.schemas.question_contract import (
    ContractReplyContext,
    CoverageCandidateItem,
    CoverageEvidenceCandidate,
    QuestionCoverageCandidate,
    build_question_contract,
)


def _contract():
    return build_question_contract(
        session_id=uuid4(),
        question_message_id=uuid4(),
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension="ten_questions.respiratory",
        selection_kind="required",
        aspect_criteria=("咳嗽性质", "痰液颜色", "痰液量"),
    )


def _message(*, message_id, content: str) -> IntakeMessage:
    return IntakeMessage(message_id=message_id, role=IntakeMessageRole.PATIENT, content=content)


def _input(*, context: ContractReplyContext | None, answer_id, content: str) -> IntakeExtractionInput:
    return IntakeExtractionInput(
        current_messages=(_message(message_id=answer_id, content=content),),
        contract_reply_context=context,
    )


def _item(*, aspect_id, status: str, content: str | None = None, answer_id=None) -> CoverageCandidateItem:
    if status in ("addressed", "not_applicable"):
        assert content is not None and answer_id is not None
        return CoverageCandidateItem(
            aspect_id=aspect_id,
            status=status,
            evidence=(
                CoverageEvidenceCandidate(
                    source_message_id=answer_id,
                    start_char=content.find("痰"),
                    end_char=content.find("痰") + 1,
                    quote="痰",
                ),
            ),
        )
    return CoverageCandidateItem(aspect_id=aspect_id, status=status)


def _candidate(*, contract, answer_id, statuses: tuple[str, ...] = ("addressed", "unanswered", "unanswered"), content: str = "有痰") -> QuestionCoverageCandidate:
    return QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=tuple(
            _item(aspect_id=aspect.aspect_id, status=status, content=content, answer_id=answer_id)
            for aspect, status in zip(contract.aspects, statuses, strict=True)
        ),
    )


def _context(contract, answer_id) -> ContractReplyContext:
    return ContractReplyContext(contract=contract, answer_message_id=answer_id)


def _output(question_coverage: QuestionCoverageCandidate | None) -> IntakeExtractionOutput:
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.ABSTAINED,
        question_coverage=question_coverage,
    )


def test_exact_unicode_span_passes() -> None:
    """Chinese code points are exact half-open ranges; an astral-plane emoji
    proves offsets are Python code points, not UTF-16 units."""
    contract = _contract()
    answer_id = uuid4()
    content = "😀咳嗽有痰，痰是黄色的"
    context = _context(contract, answer_id)
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
                        start_char=3,
                        end_char=5,
                        quote="有痰",
                    ),
                ),
            ),
            CoverageCandidateItem(aspect_id=contract.aspects[1].aspect_id, status="unanswered"),
            CoverageCandidateItem(aspect_id=contract.aspects[2].aspect_id, status="unanswered"),
        ),
    )
    assert content[3:5] == "有痰"
    assert _verify_coverage_binding(_output(candidate), _input(context=context, answer_id=answer_id, content=content)) is None


def test_quote_mismatch_fails_span() -> None:
    contract = _contract()
    answer_id = uuid4()
    content = "有痰"
    candidate = _candidate(contract=contract, answer_id=answer_id, content=content)
    # Simulate a model quote that does not match the raw message slice.
    first = candidate.items[0].model_copy(
        update={
            "evidence": (
                CoverageEvidenceCandidate(
                    source_message_id=answer_id,
                    start_char=0,
                    end_char=2,
                    quote="有痰X",
                ),
            ),
        }
    )
    output = _output(candidate.model_copy(update={"items": (first, *candidate.items[1:])}))
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content=content)
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_SPAN_INVALID
    )


def test_range_exceeding_message_is_repaired_not_rejected() -> None:
    """R9-B: a quote that is a genuine substring of the answer passes even when
    the model miscounted offsets; ``build_coverage_event`` re-anchors them."""
    contract = _contract()
    answer_id = uuid4()
    content = "有痰"
    first = contract.aspects[0]
    span = CoverageEvidenceCandidate(
        source_message_id=answer_id,
        start_char=0,
        end_char=len(content) + 1,  # model over-counted the end offset
        quote="有痰",
    )
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(aspect_id=first.aspect_id, status="addressed", evidence=(span,)),
            *(CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered") for aspect in contract.aspects[1:]),
        ),
    )
    output = _output(candidate)
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content=content)
    assert _verify_coverage_binding(output, payload) is None
    from app.schemas.question_contract import build_coverage_event

    event = build_coverage_event(contract=contract, candidate=candidate, message_contents={answer_id: content})
    repaired = event.items[0].evidence[0]
    assert repaired.start_char == 0
    assert repaired.end_char == len(content)


def test_span_from_wrong_message_fails_span() -> None:
    contract = _contract()
    answer_id = uuid4()
    content = "有痰"
    first = contract.aspects[0]
    span = CoverageEvidenceCandidate(
        source_message_id=uuid4(),  # not the bound answer
        start_char=0,
        end_char=1,
        quote="有",
    )
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(aspect_id=first.aspect_id, status="addressed", evidence=(span,)),
            *(CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered") for aspect in contract.aspects[1:]),
        ),
    )
    output = _output(candidate)
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content=content)
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_SPAN_INVALID
    )


def test_mask_wildcard_quote_passes_and_digests_raw_substring() -> None:
    """D3: a quote crossing a privacy-masked identity sequence passes the
    verifier, and the persisted evidence digest covers the raw answer text."""
    from hashlib import sha256

    from app.schemas.question_contract import build_coverage_event

    contract = _contract()
    answer_id = uuid4()
    content = "我的电话是13812345678，痰是黄色的"
    context = _context(contract, answer_id)
    masked_phone = "█" * 11
    first = contract.aspects[0]
    span = CoverageEvidenceCandidate(
        source_message_id=answer_id,
        start_char=0,
        end_char=len(content),
        quote=f"我的电话是{masked_phone}，痰是黄色的",
    )
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(aspect_id=first.aspect_id, status="addressed", evidence=(span,)),
            *(CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered") for aspect in contract.aspects[1:]),
        ),
    )
    assert (
        _verify_coverage_binding(_output(candidate), _input(context=context, answer_id=answer_id, content=content))
        is None
    )
    event = build_coverage_event(contract=contract, candidate=candidate, message_contents={answer_id: content})
    ref = event.items[0].evidence[0]
    assert content[ref.start_char : ref.end_char] == "我的电话是13812345678，痰是黄色的"
    assert ref.quote_sha256 == sha256(content.encode("utf-8")).hexdigest()


def test_coverage_without_contract_fails() -> None:
    contract = _contract()
    answer_id = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    output = _output(candidate)
    payload = _input(context=None, answer_id=answer_id, content="有痰")
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_WITHOUT_CONTRACT
    )


def test_wrong_contract_id_fails() -> None:
    contract = _contract()
    answer_id = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    other = _contract()
    output = _output(
        candidate.model_copy(update={"contract_id": other.contract_id})
    )
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content="有痰")
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_CONTRACT_MISMATCH
    )


def test_wrong_answer_message_id_fails() -> None:
    contract = _contract()
    answer_id = uuid4()
    other_answer = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    output = _output(
        candidate.model_copy(update={"answer_message_id": other_answer})
    )
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content="有痰")
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_CONTRACT_MISMATCH
    )


def test_answer_not_in_current_messages_fails_source() -> None:
    contract = _contract()
    answer_id = uuid4()
    # The contract binds this answer, but it is not among the current messages.
    candidate = _candidate(contract=contract, answer_id=answer_id)
    output = _output(candidate)
    payload = IntakeExtractionInput(
        current_messages=(_message(message_id=uuid4(), content="有痰"),),
        contract_reply_context=_context(contract, answer_id),
    )
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_SOURCE_NOT_ALLOWED
    )


def test_extra_aspect_fails_aspect_set() -> None:
    contract = _contract()
    answer_id = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    extra = _contract()
    output = _output(
        candidate.model_copy(
            update={
                "items": (
                    *candidate.items,
                    CoverageCandidateItem(aspect_id=extra.aspects[0].aspect_id, status="unanswered"),
                )
            }
        )
    )
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content="有痰")
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_ASPECT_MISMATCH
    )


def test_missing_aspect_fails_aspect_set() -> None:
    contract = _contract()
    answer_id = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    output = _output(
        candidate.model_copy(update={"items": candidate.items[:2]})
    )
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content="有痰")
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_ASPECT_MISMATCH
    )


def test_reordered_aspects_fail_aspect_set() -> None:
    contract = _contract()
    answer_id = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    output = _output(
        candidate.model_copy(update={"items": (candidate.items[2], candidate.items[0], candidate.items[1])})
    )
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content="有痰")
    assert (
        _verify_coverage_binding(output, payload)
        is IntakeVerificationFailureCode.COVERAGE_ASPECT_MISMATCH
    )


def test_duplicate_aspect_is_rejected_at_dto_construction() -> None:
    contract = _contract()
    answer_id = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    with pytest.raises(ValidationError):
        QuestionCoverageCandidate(
            contract_id=contract.contract_id,
            answer_message_id=answer_id,
            items=(candidate.items[0], candidate.items[0], candidate.items[1]),
        )


def test_unanswered_aspect_carrying_evidence_is_rejected_at_dto() -> None:
    contract = _contract()
    answer_id = uuid4()
    with pytest.raises(ValidationError):
        CoverageCandidateItem(
            aspect_id=contract.aspects[0].aspect_id,
            status="unanswered",
            evidence=(
                CoverageEvidenceCandidate(
                    source_message_id=answer_id,
                    start_char=0,
                    end_char=1,
                    quote="有",
                ),
            ),
        )


def test_legacy_no_contract_no_coverage_passes() -> None:
    answer_id = uuid4()
    output = _output(None)
    payload = _input(context=None, answer_id=answer_id, content="有痰")
    assert _verify_coverage_binding(output, payload) is None


def test_contract_present_but_coverage_missing_is_conservatively_allowed() -> None:
    contract = _contract()
    answer_id = uuid4()
    output = _output(None)
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content="有痰")
    # Coverage is never inferred from a missing candidate; the legacy path is
    # allowed to continue while the service-layer fallback is added later.
    assert _verify_coverage_binding(output, payload) is None


def test_full_chain_rejects_coverage_without_contract() -> None:
    spec = AgentSpec(
        name=INTAKE_AGENT_NAME,
        version=INTAKE_AGENT_VERSION,
        input_schema=IntakeExtractionInput,
        output_schema=IntakeExtractionOutput,
        model_policy=ModelPolicy(model="fake-model", max_attempts=1),
        tool_permissions=frozenset({Capability.READ_STATE}),
        verifier_chain=INTAKE_VERIFIER_CHAIN,
        failure_policy=FailurePolicy(),
    )
    run_spec = RunSpec(
        run_id=uuid4(),
        session_id=uuid4(),
        state_version=1,
        stage="inquiry",
        agent_spec_version=INTAKE_AGENT_VERSION,
        prompt_version=INTAKE_PROMPT_VERSION,
        policy_version=INTAKE_POLICY_VERSION,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        total_attempt_budget=1,
        idempotency_key="idempotency-key",
        trace_id="trace-1",
    )
    contract = _contract()
    answer_id = uuid4()
    candidate = _candidate(contract=contract, answer_id=answer_id)
    output = _output(candidate)
    artifact = RunArtifact(
        output=output,
        model_actual="fake-model",
        attempts=1,
        latency_ms=1,
        trace_id=run_spec.trace_id,
        run_id=run_spec.run_id,
        agent_spec_version=INTAKE_AGENT_VERSION,
        prompt_version=INTAKE_PROMPT_VERSION,
    )
    payload = _input(context=None, answer_id=answer_id, content="有痰")
    report = verify_intake_artifact(
        agent_spec=spec,
        run_spec=run_spec,
        artifact=artifact,
        input_payload=payload,
    )
    assert report.passed is False
    assert report.failure_code is IntakeVerificationFailureCode.COVERAGE_WITHOUT_CONTRACT
    failing = next(check for check in report.checks if check.failure_code is not None)
    assert failing.verifier is IntakeVerifierName.COVERAGE_BINDING


def test_full_chain_passes_with_grounded_coverage() -> None:
    spec = AgentSpec(
        name=INTAKE_AGENT_NAME,
        version=INTAKE_AGENT_VERSION,
        input_schema=IntakeExtractionInput,
        output_schema=IntakeExtractionOutput,
        model_policy=ModelPolicy(model="fake-model", max_attempts=1),
        tool_permissions=frozenset({Capability.READ_STATE}),
        verifier_chain=INTAKE_VERIFIER_CHAIN,
        failure_policy=FailurePolicy(),
    )
    run_spec = RunSpec(
        run_id=uuid4(),
        session_id=uuid4(),
        state_version=1,
        stage="inquiry",
        agent_spec_version=INTAKE_AGENT_VERSION,
        prompt_version=INTAKE_PROMPT_VERSION,
        policy_version=INTAKE_POLICY_VERSION,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        total_attempt_budget=1,
        idempotency_key="idempotency-key",
        trace_id="trace-1",
    )
    contract = _contract()
    answer_id = uuid4()
    content = "有痰"
    candidate = _candidate(contract=contract, answer_id=answer_id, content=content)
    output = _output(candidate)
    artifact = RunArtifact(
        output=output,
        model_actual="fake-model",
        attempts=1,
        latency_ms=1,
        trace_id=run_spec.trace_id,
        run_id=run_spec.run_id,
        agent_spec_version=INTAKE_AGENT_VERSION,
        prompt_version=INTAKE_PROMPT_VERSION,
    )
    payload = _input(context=_context(contract, answer_id), answer_id=answer_id, content=content)
    report = verify_intake_artifact(
        agent_spec=spec,
        run_spec=run_spec,
        artifact=artifact,
        input_payload=payload,
    )
    assert report.passed is True, report.failure_code
