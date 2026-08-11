"""Focused R9-B tests for the orchestration wiring helpers.

Drills the four pure seam functions behind wiring points A/B/C/E without any
database or model gateway: reply→contract binding, deterministic coverage
attachment, residual follow-up decision, and delta amendment.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.agent_runtime.contract_projection import project_contract_dimensions
from app.agent_runtime.reducer import DomainDelta, DomainState
from app.core.config import get_settings
from app.models.consult import ConsultMessage
from app.schemas.completeness import InquiryDimension
from app.schemas.intake import (
    IntakeExtractionDecision,
    IntakeExtractionInput,
    IntakeExtractionOutput,
    IntakeMessage,
    IntakeMessageRole,
)
from app.schemas.question_contract import (
    ContractCoverageDisposition,
    CoverageCandidateItem,
    CoverageEvidenceCandidate,
    QuestionCoverageCandidate,
    build_coverage_event,
    build_question_contract,
)
from app.services.langgraph_intake import (
    _attach_bound_coverage,
    _build_intake_input,
    _contract_context_from_message,
    _delta_with_contract,
    _residual_contract,
)


def _enable_contracts(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "question_contract_enabled", True)


def _disable_contracts(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "question_contract_enabled", False)


def _contract(
    *,
    session_id: UUID | None = None,
    question_message_id: UUID | None = None,
    dimension: str = "ten_questions.respiratory",
    safety_critical: bool = False,
    max_followups: int = 2,
):
    return build_question_contract(
        session_id=session_id or uuid4(),
        question_message_id=question_message_id or uuid4(),
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension=dimension,
        selection_kind="required",
        aspect_criteria=("咳嗽性质", "痰液颜色", "痰液量"),
        safety_critical=safety_critical,
        max_followups=max_followups,
    )


def _answer_event(contract, *, statuses: tuple[str, ...] = ("addressed", "unanswered", "unanswered")):
    answer_id = uuid4()
    items = []
    for aspect, status in zip(contract.aspects, statuses, strict=True):
        if status == "unanswered":
            items.append(CoverageCandidateItem(aspect_id=aspect.aspect_id, status=status))
        else:
            items.append(
                CoverageCandidateItem(
                    aspect_id=aspect.aspect_id,
                    status=status,
                    evidence=(
                        CoverageEvidenceCandidate(
                            source_message_id=answer_id,
                            start_char=0,
                            end_char=2,
                            quote="有痰",
                        ),
                    ),
                )
            )
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=tuple(items),
    )
    return answer_id, build_coverage_event(contract=contract, candidate=candidate, message_contents={answer_id: "有痰"})


def _reply_message(
    *,
    session_id: UUID,
    question_message_id: UUID | None,
    content: str = "有痰",
    dimension: str = "ten_questions.respiratory",
) -> ConsultMessage:
    structured = None
    if question_message_id is not None:
        structured = {
            "reply_context": {
                "question_message_id": str(question_message_id),
                "selected_dimension": dimension,
                "selection_kind": "required",
            },
            "binding_version": "intake-reply-binding.v1",
        }
    return ConsultMessage(
        id=uuid4(),
        session_id=session_id,
        role="patient",
        stage="inquiry",
        content=content,
        structured_delta=structured,
    )


def _state(session_id: UUID, contracts=(), events=()) -> DomainState:
    return DomainState(
        session_id=session_id,
        state_version=1,
        question_contracts=tuple(contracts),
        question_coverage_events=tuple(events),
    )


# ---- wiring B: _contract_context_from_message ----


def test_contract_context_binds_open_contract(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    message = _reply_message(session_id=contract.session_id, question_message_id=contract.question_message_id)
    context = _contract_context_from_message(_state(contract.session_id, [contract]), message)
    assert context is not None
    assert context.contract == contract
    assert context.answer_message_id == message.id


def test_contract_context_skips_without_reply_binding(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    message = _reply_message(session_id=contract.session_id, question_message_id=None)
    assert _contract_context_from_message(_state(contract.session_id, [contract]), message) is None


def test_contract_context_skips_unbound_question_message(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    other_question_id = uuid4()
    message = _reply_message(session_id=contract.session_id, question_message_id=other_question_id)
    assert _contract_context_from_message(_state(contract.session_id, [contract]), message) is None


def test_contract_context_skips_satisfied_contract(monkeypatch) -> None:
    """A late reply to a satisfied chain is absorbed as an observation only."""
    _enable_contracts(monkeypatch)
    contract = _contract()
    _, event = _answer_event(contract, statuses=("addressed", "addressed", "addressed"))
    message = _reply_message(session_id=contract.session_id, question_message_id=contract.question_message_id)
    assert _contract_context_from_message(_state(contract.session_id, [contract], [event]), message) is None


def test_contract_context_skips_exhausted_contract(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract(max_followups=1)
    _, first = _answer_event(contract, statuses=("addressed", "unanswered", "unanswered"))
    followup = build_question_contract(
        session_id=contract.session_id,
        question_message_id=uuid4(),
        question_text="剩余两项能确认吗？",
        parent_contract=contract,
        residual_aspects=tuple(contract.aspects[1:]),
    )
    _, second = _answer_event(followup, statuses=("unavailable", "unavailable"))
    message = _reply_message(
        session_id=contract.session_id,
        question_message_id=followup.question_message_id,
    )
    state = _state(contract.session_id, [contract, followup], [first, second])
    assert _contract_context_from_message(state, message) is None


def test_contract_context_respects_feature_flag(monkeypatch) -> None:
    _disable_contracts(monkeypatch)
    contract = _contract()
    message = _reply_message(session_id=contract.session_id, question_message_id=contract.question_message_id)
    assert _contract_context_from_message(_state(contract.session_id, [contract]), message) is None


def test_build_intake_input_carries_contract_context(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    message = _reply_message(session_id=contract.session_id, question_message_id=contract.question_message_id)
    intake_input = _build_intake_input(_state(contract.session_id, [contract]), message)
    assert intake_input.contract_reply_context is not None
    assert intake_input.contract_reply_context.contract.contract_id == contract.contract_id
    assert intake_input.reply_context is not None


def test_build_intake_input_legacy_shape_without_contract(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    session_id = uuid4()
    message = _reply_message(session_id=session_id, question_message_id=None)
    intake_input = _build_intake_input(_state(session_id), message)
    assert intake_input.contract_reply_context is None


# ---- wiring C: _attach_bound_coverage ----


def _intake_input_with_context(contract) -> IntakeExtractionInput:
    from app.schemas.question_contract import ContractReplyContext

    message = IntakeMessage(message_id=uuid4(), role=IntakeMessageRole.PATIENT, content="有痰")
    return IntakeExtractionInput(
        current_messages=(message,),
        contract_reply_context=ContractReplyContext(contract=contract, answer_message_id=message.message_id),
    )


def test_bound_coverage_attaches_for_extracted_reply(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    intake_input = _intake_input_with_context(contract)
    output = IntakeExtractionOutput(decision=IntakeExtractionDecision.EXTRACTED)
    attached = _attach_bound_coverage(output, intake_input)
    assert attached.question_coverage is not None
    assert attached.question_coverage.contract_id == contract.contract_id
    assert tuple(item.aspect_id for item in attached.question_coverage.items) == tuple(
        aspect.aspect_id for aspect in contract.aspects
    )
    assert all(item.status.value == "addressed" for item in attached.question_coverage.items)
    assert all(item.evidence[0].quote == "有痰" for item in attached.question_coverage.items)


def test_bound_coverage_skips_abstained_reply(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    intake_input = _intake_input_with_context(contract)
    output = IntakeExtractionOutput(decision=IntakeExtractionDecision.ABSTAINED)
    assert _attach_bound_coverage(output, intake_input) is output


def test_bound_coverage_skips_without_contract_context(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    message = IntakeMessage(message_id=uuid4(), role=IntakeMessageRole.PATIENT, content="有痰")
    intake_input = IntakeExtractionInput(current_messages=(message,))
    output = IntakeExtractionOutput(decision=IntakeExtractionDecision.EXTRACTED)
    assert _attach_bound_coverage(output, intake_input) is output


# ---- wiring E: _residual_contract ----


def test_residual_contract_returns_open_residual(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    _, event = _answer_event(contract)
    state = _state(contract.session_id, [contract], [event])
    decision = _residual_contract(state)
    assert decision is not None
    dimension, residual, latest = decision
    assert dimension is InquiryDimension.TEN_RESPIRATORY
    assert tuple(item.criterion for item in residual) == ("痰液颜色", "痰液量")
    assert latest == contract


def test_residual_contract_none_without_open_contract(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract()
    _, event = _answer_event(contract, statuses=("addressed", "addressed", "addressed"))
    state = _state(contract.session_id, [contract], [event])
    assert _residual_contract(state) is None


def test_residual_contract_skips_safety_contract(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    contract = _contract(dimension="safety.allergy_status", safety_critical=True)
    state = _state(contract.session_id, [contract])
    assert _residual_contract(state) is None


def test_residual_contract_picks_first_open_dimension(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    respiratory = _contract(dimension="ten_questions.respiratory")
    sleep = _contract(dimension="ten_questions.sleep")
    # Same session so the ledger stays one session.
    respiratory = build_question_contract(
        session_id=sleep.session_id,
        question_message_id=respiratory.question_message_id,
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension="ten_questions.respiratory",
        selection_kind="required",
        aspect_criteria=("咳嗽性质", "痰液颜色", "痰液量"),
    )
    state = _state(sleep.session_id, [sleep, respiratory])
    decision = _residual_contract(state)
    assert decision is not None
    assert decision[0] == InquiryDimension.TEN_RESPIRATORY


# ---- wiring A: _delta_with_contract ----


def test_delta_with_contract_appends_append_only(monkeypatch) -> None:
    _enable_contracts(monkeypatch)
    session_id = uuid4()
    run_id = uuid4()
    contract = _contract(session_id=session_id)
    delta = DomainDelta(
        delta_id=uuid4(),
        run_id=run_id,
        session_id=session_id,
        expected_state_version=1,
        source_message_ids=(uuid4(),),
    )
    amended = _delta_with_contract(delta, contract)
    assert amended.question_contracts == (contract,)
    assert delta.question_contracts == ()
    assert amended.observations == delta.observations


def test_delta_contract_flow_reduces_into_state(monkeypatch) -> None:
    """The amended delta passes reducer legality and the new root contract
    validates as an open chain."""
    from app.agent_runtime.reducer import validate_domain_delta

    _enable_contracts(monkeypatch)
    session_id = uuid4()
    state = _state(session_id)
    contract = _contract(session_id=session_id)
    delta = _delta_with_contract(
        DomainDelta(
            delta_id=uuid4(),
            run_id=uuid4(),
            session_id=session_id,
            expected_state_version=1,
            source_message_ids=(uuid4(),),
        ),
        contract,
    )
    validate_domain_delta(state, delta)
    projection = project_contract_dimensions((contract,), ())
    assert projection.open_dimensions == (InquiryDimension.TEN_RESPIRATORY,)
    assert projection.roots[0].coverage.disposition is ContractCoverageDisposition.OPEN


# ---- wiring C: _correct_coverage_semantics (R9-C) ----


def test_semantic_correction_downgrades_color_addressed_without_term() -> None:
    from app.schemas.question_contract import CoverageStatus
    from app.services.langgraph_intake import _correct_coverage_semantics

    contract = _contract()
    answer_id = uuid4()
    message = IntakeMessage(message_id=answer_id, role=IntakeMessageRole.PATIENT, content="有痰")
    # Model claims the sputum-color aspect is addressed by "有痰" — a semantic
    # miss: no color term.  The correction downgrades it to unclear.
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(
                aspect_id=contract.aspects[1].aspect_id,  # 痰液颜色
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=answer_id, start_char=0, end_char=2, quote="有痰"
                    ),
                ),
            ),
            *(CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered") for aspect in contract.aspects if aspect.ordinal != 1),
        ),
    )
    corrected = _correct_coverage_semantics(
        candidate,
        contract,
        {answer_id: message.content},
    )
    color_item = next(item for item in corrected.items if item.aspect_id == contract.aspects[1].aspect_id)
    assert color_item.status is CoverageStatus.UNCLEAR


def test_semantic_correction_keeps_addressed_with_color_term() -> None:
    from app.services.langgraph_intake import _correct_coverage_semantics

    contract = _contract()
    answer_id = uuid4()
    content = "痰是黄色的"
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(
                aspect_id=contract.aspects[1].aspect_id,  # 痰液颜色
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=answer_id, start_char=0, end_char=len(content), quote=content
                    ),
                ),
            ),
            *(CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered") for aspect in contract.aspects if aspect.ordinal != 1),
        ),
    )
    corrected = _correct_coverage_semantics(candidate, contract, {answer_id: content})
    assert corrected is candidate  # unchanged candidate returned as-is
