"""Focused R9-B tests for the contract-ledger → inquiry-dimension projection.

``project_contract_dimensions`` is the deterministic bridge between the
question-contract ledger and the completeness policy: open roots hold their
dimension, terminal roots resolve it, exhausted non-safety roots mark it
partial, and safety-critical roots are never projected.
"""

from __future__ import annotations

from uuid import uuid4

from app.agent_runtime.contract_projection import (
    ContractDimensionProjection,
    project_contract_dimensions,
)
from app.schemas.completeness import InquiryDimension
from app.schemas.question_contract import (
    CoverageCandidateItem,
    CoverageEvidenceCandidate,
    QuestionCoverageCandidate,
    build_coverage_event,
    build_question_contract,
)


def _root(*, dimension: str = "ten_questions.respiratory", safety_critical: bool = False, max_followups: int = 2) -> object:
    return build_question_contract(
        session_id=uuid4(),
        question_message_id=uuid4(),
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension=dimension,
        selection_kind="required",
        aspect_criteria=("咳嗽性质", "痰液颜色", "痰液量"),
        safety_critical=safety_critical,
        max_followups=max_followups,
    )


def _answer(contract, *, statuses: tuple[str, ...], content: str = "有痰") -> object:
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
                            end_char=len(content),
                            quote=content,
                        ),
                    ),
                )
            )
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=tuple(items),
    )
    return build_coverage_event(contract=contract, candidate=candidate, message_contents={answer_id: content})


def test_empty_ledger_projects_to_nothing() -> None:
    projection = project_contract_dimensions([], [])
    assert projection.open_dimensions == ()
    assert projection.resolved_dimensions == ()
    assert projection.partial_dimensions == ()
    assert projection.roots == ()


def test_open_contract_holds_its_dimension() -> None:
    root = _root()
    projection = project_contract_dimensions([root], [])
    assert projection.open_dimensions == (InquiryDimension.TEN_RESPIRATORY,)
    assert projection.resolved_dimensions == ()
    assert projection.partial_dimensions == ()


def test_partial_answer_keeps_dimension_open() -> None:
    root = _root()
    event = _answer(root, statuses=("addressed", "unanswered", "unanswered"))
    projection = project_contract_dimensions([root], [event])
    assert projection.open_dimensions == (InquiryDimension.TEN_RESPIRATORY,)
    assert projection.resolved_dimensions == ()


def test_satisfied_contract_resolves_dimension() -> None:
    root = _root()
    event = _answer(root, statuses=("addressed", "addressed", "addressed"))
    projection = project_contract_dimensions([root], [event])
    assert projection.open_dimensions == ()
    assert projection.resolved_dimensions == (InquiryDimension.TEN_RESPIRATORY,)
    assert projection.partial_dimensions == ()


def test_not_applicable_contract_resolves_dimension() -> None:
    root = _root()
    event = _answer(root, statuses=("not_applicable", "not_applicable", "not_applicable"), content="干咳")
    projection = project_contract_dimensions([root], [event])
    assert projection.open_dimensions == ()
    assert projection.resolved_dimensions == (InquiryDimension.TEN_RESPIRATORY,)


def test_exhausted_partial_contract_marks_dimension_partial() -> None:
    root = _root(max_followups=1)
    first = _answer(root, statuses=("addressed", "unanswered", "unanswered"))
    followup = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="剩余两项能确认吗？",
        parent_contract=root,
        residual_aspects=tuple(root.aspects[1:]),
    )
    second = _answer(followup, statuses=("unavailable", "unavailable"), content="不知道")
    projection = project_contract_dimensions([root, followup], [first, second])
    assert projection.open_dimensions == ()
    assert projection.resolved_dimensions == (InquiryDimension.TEN_RESPIRATORY,)
    assert projection.partial_dimensions == (InquiryDimension.TEN_RESPIRATORY,)


def test_safety_contract_is_never_projected() -> None:
    root = _root(dimension="safety.allergy_status", safety_critical=True)
    projection = project_contract_dimensions([root], [])
    assert projection.open_dimensions == ()
    assert projection.resolved_dimensions == ()
    assert projection.partial_dimensions == ()


def test_multiple_roots_are_independent() -> None:
    respiratory = _root(dimension="ten_questions.respiratory")
    sleep = _root(dimension="ten_questions.sleep")
    projection = project_contract_dimensions([respiratory, sleep], [])
    assert projection.open_dimensions == (
        InquiryDimension.TEN_RESPIRATORY,
        InquiryDimension.TEN_SLEEP,
    )
    assert projection.resolved_dimensions == ()
    assert len(projection.roots) == 2


def test_damaged_chain_fails_open_to_no_projection() -> None:
    """A malformed ledger (rejected at write time by the reducer) must not
    block the completeness path: the root is skipped, legacy projection stands."""
    root = _root()
    orphan = _answer(root, statuses=("addressed", "addressed", "addressed"))
    # Break the chain: report an event for a contract that never appears.
    broken = orphan.model_copy(update={"contract_id": uuid4(), "event_id": uuid4()})
    projection = project_contract_dimensions([root], [broken])
    assert projection.open_dimensions == ()
    assert projection.resolved_dimensions == ()
    assert projection.roots == ()


def test_projection_dimension_tuples_are_sorted_and_unique() -> None:
    respiratory = _root(dimension="ten_questions.respiratory")
    sleep = _root(dimension="ten_questions.sleep")
    cold_heat = _root(dimension="ten_questions.cold_heat")
    projection = project_contract_dimensions([respiratory, sleep, cold_heat], [])
    assert projection.open_dimensions == tuple(sorted(projection.open_dimensions, key=lambda item: item.value))
    assert len(projection.open_dimensions) == len(set(projection.open_dimensions))
    # Schema-level constraint: open and resolved sets must stay disjoint.
    ContractDimensionProjection._from_roots(projection.roots)
