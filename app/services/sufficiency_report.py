"""Presentation-safe copy for the completeness report.

The completeness policy remains the authority for whether a dimension is
required or covered.  This module only translates its stable dimension keys
into clinician-facing copy for API consumers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypedDict


class MissingItemPayload(TypedDict):
    """Structured, clinician-facing explanation of one required gap."""

    key: str
    label: str
    reason: str
    suggested_question: str


@dataclass(frozen=True)
class _MissingItemCopy:
    label: str
    reason: str
    suggested_question: str


_MISSING_ITEM_COPY: dict[str, _MissingItemCopy] = {
    "chief_complaint.symptom": _MissingItemCopy(
        label="主要不适",
        reason="尚未明确最主要的不适表现。",
        suggested_question="请描述目前最主要的不适是什么。",
    ),
    "chief_complaint.course": _MissingItemCopy(
        label="病程",
        reason="主要不适的起病或持续时间尚未明确。",
        suggested_question="这些不适大约从什么时候开始，持续多久了？",
    ),
    "present_illness.change": _MissingItemCopy(
        label="病情变化",
        reason="症状近期的变化情况尚未完整。",
        suggested_question="最近症状是加重、减轻，还是没有明显变化？",
    ),
    "ten_questions.cold_heat": _MissingItemCopy(
        label="寒热情况",
        reason="怕冷、发热等寒热表现尚未了解。",
        suggested_question="近期有怕冷、发热或体温异常的感觉吗？",
    ),
    "ten_questions.sweat": _MissingItemCopy(
        label="出汗情况",
        reason="出汗的多少及特点尚未了解。",
        suggested_question="平时出汗是否正常，有没有自汗、盗汗？",
    ),
    "ten_questions.head_body": _MissingItemCopy(
        label="头身情况",
        reason="头部及全身感受尚未完整。",
        suggested_question="有没有头晕、头痛、乏力或身体酸痛等情况？",
    ),
    "ten_questions.stool_urine": _MissingItemCopy(
        label="二便情况",
        reason="大便和小便情况尚未了解。",
        suggested_question="近期大便和小便情况怎么样？",
    ),
    "ten_questions.diet": _MissingItemCopy(
        label="饮食情况",
        reason="食欲、口味及进食情况尚未了解。",
        suggested_question="最近食欲、饮食量和口味有什么变化吗？",
    ),
    "ten_questions.chest_abdomen": _MissingItemCopy(
        label="胸腹情况",
        reason="胸腹部不适及感受尚未了解。",
        suggested_question="胸腹部有没有胀满、疼痛或不适？",
    ),
    "ten_questions.thirst": _MissingItemCopy(
        label="口渴情况",
        reason="口渴及饮水情况尚未了解。",
        suggested_question="平时口渴吗，饮水量有没有变化？",
    ),
    "ten_questions.sleep": _MissingItemCopy(
        label="睡眠情况",
        reason="入睡、易醒及睡眠质量尚未了解。",
        suggested_question="近来的入睡、夜醒和睡眠质量怎么样？",
    ),
    "ten_questions.menses_leukorrhea": _MissingItemCopy(
        label="月经带下",
        reason="月经或带下情况尚未了解。",
        suggested_question="月经或带下情况近期是否有变化？",
    ),
    "ten_questions.pain": _MissingItemCopy(
        label="疼痛情况",
        reason="疼痛的部位、性质或程度尚未了解。",
        suggested_question="有没有疼痛？如果有，具体部位和感觉如何？",
    ),
    "ten_questions.respiratory": _MissingItemCopy(
        label="呼吸情况",
        reason="咳嗽、气促等呼吸情况尚未了解。",
        suggested_question="有没有咳嗽、气短、喘或胸闷？",
    ),
    "safety.allergy_status": _MissingItemCopy(
        label="过敏史",
        reason="药物或食物过敏史尚未确认。",
        suggested_question="是否有药物、食物或其他过敏史？",
    ),
    "safety.medication_status": _MissingItemCopy(
        label="当前用药",
        reason="正在使用的药物情况尚未确认。",
        suggested_question="目前是否正在服用中药、西药或保健品？",
    ),
    "safety.major_condition_status": _MissingItemCopy(
        label="重要疾病史",
        reason="重要既往疾病情况尚未确认。",
        suggested_question="是否有高血压、糖尿病、心脑血管等重要疾病史？",
    ),
    "safety.pregnancy_status": _MissingItemCopy(
        label="妊娠情况",
        reason="是否处于妊娠期尚未确认。",
        suggested_question="目前是否怀孕或有怀孕可能？",
    ),
    "safety.lactation_status": _MissingItemCopy(
        label="哺乳情况",
        reason="是否处于哺乳期尚未确认。",
        suggested_question="目前是否处于哺乳期？",
    ),
    "past_history": _MissingItemCopy(
        label="既往病史",
        reason="既往疾病及重要治疗史尚未了解。",
        suggested_question="以前有过哪些重要疾病、手术或长期治疗吗？",
    ),
    "four_diagnosis": _MissingItemCopy(
        label="四诊信息",
        reason="望、闻、问、切相关信息尚未完整。",
        suggested_question="请补充舌象、面色、声音或脉象等四诊信息。",
    ),
    "patient.sex": _MissingItemCopy(
        label="性别",
        reason="患者性别尚未确认。",
        suggested_question="请确认患者性别。",
    ),
    "patient.age": _MissingItemCopy(
        label="年龄",
        reason="患者年龄尚未确认。",
        suggested_question="请确认患者年龄。",
    ),
    "patient.menopause_status": _MissingItemCopy(
        label="绝经情况",
        reason="绝经情况尚未确认。",
        suggested_question="目前是否已经绝经？",
    ),
    "patient.pregnancy_applicability": _MissingItemCopy(
        label="妊娠问诊适用性",
        reason="是否需要进行妊娠相关问诊尚未确认。",
        suggested_question="请确认是否需要进一步了解妊娠情况。",
    ),
    "patient.lactation_applicability": _MissingItemCopy(
        label="哺乳问诊适用性",
        reason="是否需要进行哺乳相关问诊尚未确认。",
        suggested_question="请确认是否需要进一步了解哺乳情况。",
    ),
}

_FALLBACK_COPY = _MissingItemCopy(
    label="待补充信息",
    reason="该项问诊信息尚未完整。",
    suggested_question="请补充与当前症状相关的信息。",
)


def missing_item_payloads(dimensions: Iterable[object]) -> list[MissingItemPayload]:
    """Turn missing completeness dimensions into stable API payloads.

    Unknown future dimensions deliberately receive generic copy so a policy
    deployment never leaks a technical key into the clinician-facing UI.
    """

    payloads: list[MissingItemPayload] = []
    seen: set[str] = set()
    for dimension in dimensions:
        raw_key = getattr(dimension, "value", dimension)
        key = str(raw_key).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        copy = _MISSING_ITEM_COPY.get(key, _FALLBACK_COPY)
        payloads.append(
            {
                "key": key,
                "label": copy.label,
                "reason": copy.reason,
                "suggested_question": copy.suggested_question,
            }
        )
    return payloads
