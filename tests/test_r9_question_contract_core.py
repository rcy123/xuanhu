"""Focused R9 tests for generic question contracts and the coverage ledger."""

from __future__ import annotations

import json
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from app.agent_runtime.question_contract import (
    QuestionContractIntegrityError,
    contract_projection_metadata,
    evaluate_contract_coverage,
)
from app.models import QuestionContractRecord, QuestionCoverageEventRecord
from app.schemas.question_contract import (
    ContractCoverageDisposition,
    CoverageCandidateItem,
    CoverageEvidenceCandidate,
    QuestionContract,
    QuestionCoverageCandidate,
    build_coverage_event,
    build_question_contract,
)

MIGRATION_MODULE = "app.db.migrations.versions.20260811_0017_question_contract_coverage"


def _root(*, safety_critical: bool = False, max_followups: int = 2) -> QuestionContract:
    return build_question_contract(
        session_id=uuid4(),
        question_message_id=uuid4(),
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension="ten_questions.respiratory",
        selection_kind="required",
        aspect_criteria=("咳嗽性质", "痰液颜色", "痰液量"),
        max_followups=max_followups,
        safety_critical=safety_critical,
    )


def _partial_first_answer(root: QuestionContract) -> tuple[UUID, object]:
    answer_id = uuid4()
    candidate = QuestionCoverageCandidate(
        contract_id=root.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(
                aspect_id=root.aspects[0].aspect_id,
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=answer_id,
                        start_char=0,
                        end_char=2,
                        quote="有痰",
                    ),
                ),
            ),
            CoverageCandidateItem(aspect_id=root.aspects[1].aspect_id, status="unanswered"),
            CoverageCandidateItem(aspect_id=root.aspects[2].aspect_id, status="unanswered"),
        ),
    )
    return answer_id, build_coverage_event(contract=root, candidate=candidate, message_contents={answer_id: "有痰"})


def test_contract_identity_digest_and_public_ref_are_stable_and_minimal() -> None:
    session_id = uuid4()
    message_id = uuid4()
    kwargs = {
        "session_id": session_id,
        "question_message_id": message_id,
        "question_text": "请说明最重要的三个特征？",
        "dimension": "present_illness.change",
        "selection_kind": "required",
        "aspect_criteria": ("第一个特征", "第二个特征", "第三个特征"),
    }
    first = build_question_contract(**kwargs)
    second = build_question_contract(**kwargs)
    assert first == second
    assert first.contract_id.version == 5
    assert all(item.aspect_id.version == 5 for item in first.aspects)

    ref_payload = first.to_ref().model_dump(mode="json")
    assert ref_payload["contract_id"] == str(first.contract_id)
    assert "aspects" not in ref_payload
    assert "criterion" not in json.dumps(ref_payload, ensure_ascii=False)
    assert "第一个特征" not in repr(first)

    tampered = first.model_dump(mode="json")
    tampered["contract_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="contract_digest"):
        QuestionContract.model_validate(tampered)
    tampered = first.model_dump(mode="json")
    tampered["contract_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="stable question-message identity"):
        QuestionContract.model_validate(tampered)


def test_coverage_event_requires_exact_contract_and_current_message_grounding() -> None:
    root = _root()
    answer_id = uuid4()
    missing_aspects = QuestionCoverageCandidate(
        contract_id=root.contract_id,
        answer_message_id=answer_id,
        items=(CoverageCandidateItem(aspect_id=root.aspects[0].aspect_id, status="unanswered"),),
    )
    with pytest.raises(ValueError, match="every and only"):
        build_coverage_event(contract=root, candidate=missing_aspects, message_contents={answer_id: "有痰"})

    ungrounded = QuestionCoverageCandidate(
        contract_id=root.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(
                aspect_id=root.aspects[0].aspect_id,
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=answer_id,
                        start_char=0,
                        end_char=2,
                        quote="干咳",
                    ),
                ),
            ),
            CoverageCandidateItem(aspect_id=root.aspects[1].aspect_id, status="unanswered"),
            CoverageCandidateItem(aspect_id=root.aspects[2].aspect_id, status="unanswered"),
        ),
    )
    with pytest.raises(ValueError, match="not grounded"):
        build_coverage_event(contract=root, candidate=ungrounded, message_contents={answer_id: "有痰"})
    assert "干咳" not in repr(ungrounded)

    _, event = _partial_first_answer(root)
    persisted = event.model_dump(mode="json")
    serialized = json.dumps(persisted, ensure_ascii=False)
    assert "有痰" not in serialized
    assert persisted["items"][0]["evidence"][0].keys() == {
        "source_message_id",
        "start_char",
        "end_char",
        "quote_sha256",
    }


def test_partial_answer_residual_followup_and_joint_projection() -> None:
    root = _root()
    first_answer_id, first_event = _partial_first_answer(root)
    partial = evaluate_contract_coverage([root], [first_event])
    assert partial.disposition is ContractCoverageDisposition.OPEN
    assert tuple(item.criterion for item in partial.residual_aspects) == ("痰液颜色", "痰液量")

    followup = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="痰是什么颜色，量多还是少？",
        parent_contract=root,
        residual_aspects=partial.residual_aspects,
        max_followups=root.max_followups,
        safety_critical=root.safety_critical,
    )
    second_answer_id = uuid4()
    second_candidate = QuestionCoverageCandidate(
        contract_id=followup.contract_id,
        answer_message_id=second_answer_id,
        items=(
            CoverageCandidateItem(
                aspect_id=followup.aspects[0].aspect_id,
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=second_answer_id,
                        start_char=0,
                        end_char=2,
                        quote="白色",
                    ),
                ),
            ),
            CoverageCandidateItem(
                aspect_id=followup.aspects[1].aspect_id,
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=second_answer_id,
                        start_char=3,
                        end_char=5,
                        quote="量少",
                    ),
                ),
            ),
        ),
    )
    second_event = build_coverage_event(
        contract=followup,
        candidate=second_candidate,
        message_contents={second_answer_id: "白色，量少"},
    )
    complete = evaluate_contract_coverage([followup, root], [second_event, first_event])
    assert complete.disposition is ContractCoverageDisposition.SATISFIED
    assert complete.unresolved_aspect_ids == ()
    assert complete.event_count == 2
    assert complete.joint_evidence[0].evidence[0].source_message_id == first_answer_id
    assert all(
        item.evidence[0].source_message_id == second_answer_id for item in complete.joint_evidence[1:]
    )
    assert contract_projection_metadata(complete)["remaining_count"] == 0


def test_fold_rejects_followup_target_expansion() -> None:
    root = _root()
    _, first_event = _partial_first_answer(root)
    expanded = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="请把前面所有内容再回答一次？",
        parent_contract=root,
        residual_aspects=root.aspects,
        max_followups=root.max_followups,
        safety_critical=root.safety_critical,
    )
    with pytest.raises(QuestionContractIntegrityError, match="exactly equal"):
        evaluate_contract_coverage([root, expanded], [first_event])


def test_fold_allows_full_repeat_followup_without_parent_event() -> None:
    """R9-B: a follow-up may appear without an event on its parent only when it
    repeats the parent's *full* aspect set (nothing was answered, so the parent
    still awaits every aspect).  This lets the graph re-ask a still-awaiting
    question after a social/abstained reply without abandoning the contract."""
    root = _root()
    reask = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="请再说明一下咳嗽和痰的情况？",
        parent_contract=root,
        residual_aspects=root.aspects,
    )
    projection = evaluate_contract_coverage([root, reask], [])
    assert projection.disposition is ContractCoverageDisposition.OPEN
    assert projection.latest_revision == 2
    assert tuple(item.criterion for item in projection.residual_aspects) == (
        "咳嗽性质",
        "痰液颜色",
        "痰液量",
    )


def test_fold_rejects_partial_repeat_followup_without_parent_event() -> None:
    """Without a parent event the residual is definitionally the parent's full
    aspect set: a follow-up that silently drops any target is still rejected."""
    root = _root()
    dropped = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="剩下两项能确认吗？",
        parent_contract=root,
        residual_aspects=tuple(root.aspects[1:]),
    )
    with pytest.raises(QuestionContractIntegrityError, match="only the latest contract may await"):
        evaluate_contract_coverage([root, dropped], [])


def test_fold_unanswered_reask_chain_reaches_cap() -> None:
    """Re-asked follow-ups without answers advance the revision budget: after
    ``max_followups`` re-asks the chain exhausts instead of looping forever."""
    root = _root(max_followups=1)
    reask = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="请再说明一下？",
        parent_contract=root,
        residual_aspects=root.aspects,
    )
    projection = evaluate_contract_coverage([root, reask], [])
    assert projection.disposition is ContractCoverageDisposition.EXHAUSTED_PARTIAL
    assert projection.followups_used == 1
    assert projection.event_count == 0


@pytest.mark.parametrize(
    ("safety_critical", "expected"),
    [
        (False, ContractCoverageDisposition.EXHAUSTED_PARTIAL),
        (True, ContractCoverageDisposition.MANUAL_REQUIRED),
    ],
)
def test_cap_is_generic_but_safety_contract_fails_closed(
    safety_critical: bool,
    expected: ContractCoverageDisposition,
) -> None:
    root = _root(safety_critical=safety_critical, max_followups=1)
    _, first_event = _partial_first_answer(root)
    partial = evaluate_contract_coverage([root], [first_event])
    followup = build_question_contract(
        session_id=root.session_id,
        question_message_id=uuid4(),
        question_text="剩余信息目前能确认吗？",
        parent_contract=root,
        residual_aspects=partial.residual_aspects,
        max_followups=1,
        safety_critical=safety_critical,
    )
    answer_id = uuid4()
    candidate = QuestionCoverageCandidate(
        contract_id=followup.contract_id,
        answer_message_id=answer_id,
        items=tuple(
            CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered")
            for aspect in followup.aspects
        ),
    )
    event = build_coverage_event(contract=followup, candidate=candidate, message_contents={answer_id: "不知道"})
    projection = evaluate_contract_coverage([root, followup], [first_event, event])
    assert projection.disposition is expected


def test_orm_and_migration_are_two_quote_free_append_only_tables() -> None:
    contract_table = QuestionContractRecord.__table__
    event_table = QuestionCoverageEventRecord.__table__
    assert contract_table.name == "question_contracts"
    assert event_table.name == "question_coverage_events"
    assert set(contract_table.columns) >= {
        contract_table.c.id,
        contract_table.c.aspects,
        contract_table.c.contract_digest,
    }
    assert "quote" not in event_table.columns
    assert "content" not in event_table.columns
    assert {constraint.name for constraint in event_table.constraints} >= {
        "uq_question_coverage_events_contract",
        "uq_question_coverage_events_answer_message",
    }
    assert {index.name for index in contract_table.indexes} >= {
        "idx_question_contracts_session_created",
        "idx_question_contracts_root_revision",
    }

    migration = import_module(MIGRATION_MODULE)
    assert migration.revision == "20260811_0017"
    assert migration.down_revision == "20260729_0016"
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_current_head() == "20260813_0018"
