"""Safety regressions for deterministic raw-text triage precheck."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent_runtime.triage_precheck import (
    TRIAGE_PRECHECK_RULES,
    PrecheckDisposition,
    TriagePrecheckResult,
    evaluate_raw_text_triage_precheck,
    merge_red_flag_candidates,
)
from app.schemas.intake import CandidateSeverity, EvidenceSpan, RedFlagCandidate, RedFlagCategory


@pytest.mark.parametrize(
    "text,category",
    [
        ("突然胸痛，像被压住一样", RedFlagCategory.SEVERE_PAIN),
        ("现在喘不过气", RedFlagCategory.BREATHING_DIFFICULTY),
        ("患者意识不清，叫不醒", RedFlagCategory.ALTERED_CONSCIOUSNESS),
        ("伤口大出血，血流不止", RedFlagCategory.SEVERE_BLEEDING),
        ("突然口角歪斜并且言语不清", RedFlagCategory.NEUROLOGIC_DEFICIT),
        ("体温 40.1℃", RedFlagCategory.HIGH_FEVER),
        ("体温104°F", RedFlagCategory.HIGH_FEVER),
        ("血氧饱和度85%", RedFlagCategory.BREATHING_DIFFICULTY),
        ("I have shortness of breath", RedFlagCategory.BREATHING_DIFFICULTY),
    ],
)
def test_each_high_risk_category_is_detected_without_a_model(text: str, category: RedFlagCategory) -> None:
    result = evaluate_raw_text_triage_precheck(uuid4(), text)
    assert result.disposition is PrecheckDisposition.RED_FLAG
    assert category in {item.category for item in result.candidates}
    assert all(item.confidence == 1.0 for item in result.candidates)


@pytest.mark.parametrize("text", ["否认胸痛和呼吸困难", "没有高热，体温37.2度", "no chest pain"])
def test_explicit_negation_does_not_create_a_false_red_flag(text: str) -> None:
    result = evaluate_raw_text_triage_precheck(uuid4(), text)
    assert result.disposition is PrecheckDisposition.CLEAR
    assert result.candidates == ()


def test_uncertain_negation_fails_closed_to_manual_review() -> None:
    result = evaluate_raw_text_triage_precheck(uuid4(), "不确定是否有呼吸困难")
    assert result.disposition is PrecheckDisposition.MANUAL_REVIEW
    assert result.candidates[0].category is RedFlagCategory.OTHER


@pytest.mark.parametrize(
    "text",
    [
        "如果胸痛怎么办",
        "昨天胸痛今天已缓解",
        "我家人胸痛，我本人没事",
        "既往胸痛目前无胸痛",
    ],
)
def test_non_current_context_fails_closed_to_manual_review(text: str) -> None:
    result = evaluate_raw_text_triage_precheck(uuid4(), text)
    assert result.disposition is PrecheckDisposition.MANUAL_REVIEW
    assert result.matches
    assert all(match.quote == text[match.start_char : match.end_char] for match in result.matches)


def test_postposed_negation_is_clear_but_adversative_current_symptom_blocks() -> None:
    assert evaluate_raw_text_triage_precheck(uuid4(), "胸痛没有").disposition is PrecheckDisposition.CLEAR
    assert (
        evaluate_raw_text_triage_precheck(uuid4(), "否认胸痛，但现在呼吸困难").disposition
        is PrecheckDisposition.RED_FLAG
    )


def test_result_constructor_rejects_inconsistent_disposition() -> None:
    with pytest.raises(ValidationError):
        TriagePrecheckResult(disposition=PrecheckDisposition.RED_FLAG)


def test_model_cannot_remove_or_downgrade_deterministic_candidate() -> None:
    message_id = uuid4()
    deterministic = evaluate_raw_text_triage_precheck(message_id, "呼吸困难").candidates
    model_candidate = RedFlagCandidate(
        category=RedFlagCategory.BREATHING_DIFFICULTY,
        source_message_id=message_id,
        span=EvidenceSpan(
            source_message_id=message_id,
            start_char=0,
            end_char=4,
            quote="呼吸困难",
        ),
        severity=CandidateSeverity.LOW,
        evidence="model said low",
        confidence=0.1,
    )
    merged = merge_red_flag_candidates(deterministic, (model_candidate,))
    assert len(merged) == 1
    assert merged[0].severity is CandidateSeverity.HIGH
    assert merged[0].evidence.startswith("deterministic:")


def test_exported_rule_tuple_cannot_change_evaluator_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent_runtime.triage_precheck as module

    assert isinstance(TRIAGE_PRECHECK_RULES, tuple)
    monkeypatch.setattr(module, "TRIAGE_PRECHECK_RULES", ())
    result = module.evaluate_raw_text_triage_precheck(uuid4(), "胸痛")
    assert result.disposition is PrecheckDisposition.RED_FLAG


def test_internal_evaluator_failure_fails_closed_without_exposing_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent_runtime.triage_precheck as module

    def fail(_: object, __: object) -> object:
        raise RuntimeError("patient text must not escape")

    monkeypatch.setattr(module, "_EVALUATE_RULES", fail)
    result = module.evaluate_raw_text_triage_precheck(uuid4(), "ordinary input")
    assert result.disposition is PrecheckDisposition.MANUAL_REVIEW
    assert result.candidates[0].category is RedFlagCategory.OTHER
    assert "patient text" not in result.candidates[0].evidence
