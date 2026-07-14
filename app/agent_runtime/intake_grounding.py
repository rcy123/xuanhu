"""Deterministic raw-message grounding for high-risk L3-1 candidates.

The model may propose safety facts and red flags, but it cannot establish that
the proposal is present in the patient's text.  This module proves that every
high-risk proposal has an exact, current-message span and that the span supports
the proposed value in a conservative, locally checkable context.

This is deliberately a pure pre-reducer boundary: it performs no persistence,
model calls, routing, or clinical approval.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from uuid import UUID

from app.schemas.domain import CollectionStatus, LactationValue, PregnancyValue
from app.schemas.intake import (
    EvidenceSpan,
    IntakeExtractionInput,
    IntakeExtractionOutput,
    LactationDelta,
    PregnancyDelta,
    RedFlagCandidate,
    RedFlagCategory,
    SafetyListDelta,
)


class IntakeGroundingFailureKind(StrEnum):
    """Patient-data-free failure classes mapped to public fixed codes."""

    SPAN_INVALID = "span_invalid"
    VALUE_MISMATCH = "value_mismatch"
    CONTEXT_UNSAFE = "context_unsafe"


_CONTRAST_PATTERN = re.compile(
    r"(?:但是|但(?!是)|不过|然而|可是|却|反而|而是|只有|\bbut\b|\bhowever\b|\byet\b)",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_PATTERN = re.compile(
    r"[，。；;,.!?！？\n]|(?:但是|但(?!是)|不过|然而|可是|却|反而|而是|只有|\bbut\b|\bhowever\b|\byet\b)",
    re.IGNORECASE,
)
_NEGATION_PATTERN = re.compile(
    r"(?:没有|没(?!准|关系)|否认|未见|未出现|未曾|"
    r"未(?=孕|怀孕|妊娠|哺乳|母乳|服用|用药|吃药|过敏|患病|患有)|"
    r"不伴|不(?:在|是|处于)?(?=孕|怀孕|妊娠|哺乳|母乳|服用|用药|吃药|过敏)|"
    r"非(?=孕|妊娠|哺乳)|停止(?:哺乳|母乳|喂奶)|并无|从未|"
    r"无(?!力|法)|\bnot\b|\bno\b|\bwithout\b|\bden(?:y|ies|ied)\b)",
    re.IGNORECASE,
)
_UNCERTAINTY_PATTERN = re.compile(
    r"(?:不确定|说不清|不清楚|可能|也许|好像|似乎|疑似|大概|或许|是否|"
    r"\buncertain\b|\bmaybe\b|\bpossibly\b|\bperhaps\b|\bunsure\b)",
    re.IGNORECASE,
)
_HISTORICAL_PATTERN = re.compile(
    r"(?:曾经|以前|过去|既往|此前|小时候|多年前|\bhistory\b|\bpreviously\b|\bused to\b)",
    re.IGNORECASE,
)
_HYPOTHETICAL_PATTERN = re.compile(
    r"(?:如果|假如|万一|要是|假设|\bif\b|\bwhat if\b|\bin case\b)",
    re.IGNORECASE,
)
_RESOLVED_PATTERN = re.compile(
    r"(?:已缓解|已经缓解|已经好了|现已消失|现在好了|已经停用|已停药|"
    r"\bresolved\b|\bwent away\b|\bhas stopped\b|\bstopped taking\b)",
    re.IGNORECASE,
)
_THIRD_PERSON_PATTERN = re.compile(
    r"(?:我家人|家里人|朋友|同事|别人|患者家属|\bmy (?:friend|family|colleague)\b|"
    r"\bsomeone else\b)",
    re.IGNORECASE,
)
_HIGH_TEMPERATURE_PATTERN = re.compile(
    r"(?<!\d)(?:(?:39(?:\.\d+)?|4[0-5](?:\.\d+)?)\s*(?:℃|°\s*c|摄氏度|度)|"
    r"(?:10[3-9](?:\.\d+)?|11[0-3](?:\.\d+)?)\s*°?\s*f)",
    re.IGNORECASE,
)
_LOW_OXYGEN_PATTERN = re.compile(
    r"(?:(?:血氧(?:饱和度)?|spo2|oxygen saturation)\D{0,8}(?:[5-8]\d(?:\.\d+)?)\s*%|"
    r"(?:[5-8]\d(?:\.\d+)?)\s*%\D{0,8}(?:血氧(?:饱和度)?|spo2|oxygen saturation))",
    re.IGNORECASE,
)

_RED_FLAG_TERMS: dict[RedFlagCategory, tuple[str, ...]] = {
    RedFlagCategory.SEVERE_PAIN: (
        "胸痛",
        "心前区痛",
        "剧烈疼痛",
        "撕裂样痛",
        "刀割样痛",
        "压榨样痛",
        "severe pain",
        "chest pain",
        "crushing pain",
    ),
    RedFlagCategory.BREATHING_DIFFICULTY: (
        "呼吸困难",
        "喘不上气",
        "喘不过气",
        "无法呼吸",
        "气促",
        "窒息",
        "cannot breathe",
        "can't breathe",
        "shortness of breath",
        "difficulty breathing",
        "dyspnea",
    ),
    RedFlagCategory.ALTERED_CONSCIOUSNESS: (
        "意识不清",
        "意识模糊",
        "失去意识",
        "昏迷",
        "叫不醒",
        "晕厥",
        "unconscious",
        "loss of consciousness",
        "cannot wake",
        "fainted",
    ),
    RedFlagCategory.SEVERE_BLEEDING: (
        "大出血",
        "严重出血",
        "血流不止",
        "止不住血",
        "大量呕血",
        "大量咯血",
        "大量便血",
        "massive bleeding",
        "severe bleeding",
        "bleeding won't stop",
        "vomiting blood",
    ),
    RedFlagCategory.NEUROLOGIC_DEFICIT: (
        "口角歪斜",
        "一侧肢体无力",
        "单侧肢体无力",
        "半边身子无力",
        "偏瘫",
        "言语不清",
        "突然失语",
        "face drooping",
        "one-sided weakness",
        "slurred speech",
        "sudden paralysis",
    ),
    RedFlagCategory.HIGH_FEVER: ("高热", "高烧", "超高热", "high fever"),
    RedFlagCategory.OTHER: (
        "休克",
        "急救",
        "危及生命",
        "生命危险",
        "快不行",
        "shock",
        "emergency",
        "life-threatening",
    ),
}

_LIST_FIELD_TERMS: dict[str, tuple[str, ...]] = {
    "allergy": ("过敏", "allerg"),
    "medications": ("用药", "服药", "吃药", "药物", "medication", "medicine", "drug", "pill"),
    "major_conditions": (
        "重大疾病",
        "基础病",
        "慢性病",
        "疾病",
        "病史",
        "高血压",
        "糖尿病",
        "condition",
        "disease",
    ),
    "contraindications": ("禁忌", "不宜", "contraindication"),
}
_LIST_EXPLICIT_NONE_PATTERNS: dict[str, re.Pattern[str]] = {
    "allergy": re.compile(
        r"(?:(?:无|没有|否认)(?:任何|已知|药物|食物)?过敏(?:史)?|"
        r"\bno\s+(?:known\s+|drug\s+|food\s+)?allerg(?:y|ies)\b)",
        re.IGNORECASE,
    ),
    "medications": re.compile(
        r"(?:(?:无|没有|未|不)(?:在|再)?(?:服用|使用|吃|服)(?:任何|当前|目前)?(?:药|药物)?|"
        r"(?:无|没有)(?:当前|目前)?用药|"
        r"\b(?:no|not taking)\s+(?:current\s+|any\s+)?(?:medications?|medicines?|drugs?|pills?)\b)",
        re.IGNORECASE,
    ),
    "major_conditions": re.compile(
        r"(?:(?:无|没有|否认)(?:重大|基础|慢性)?(?:疾病|病史)|"
        r"\bno\s+(?:major\s+|chronic\s+)?(?:conditions?|diseases?|medical history)\b)",
        re.IGNORECASE,
    ),
    "contraindications": re.compile(
        r"(?:(?:无|没有|否认)(?:任何|已知)?禁忌|"
        r"\bno\s+(?:known\s+|any\s+)?contraindications?\b)",
        re.IGNORECASE,
    ),
}
_PREGNANCY_TERMS = ("怀孕", "妊娠", "孕期", "有孕", "未孕", "非孕", "pregnan")
_LACTATION_TERMS = ("哺乳", "母乳", "喂奶", "泌乳", "lactat", "breastfeed", "nursing")


def verify_intake_grounding(
    output: IntakeExtractionOutput,
    input_payload: IntakeExtractionInput,
) -> IntakeGroundingFailureKind | None:
    """Return the first deterministic grounding failure, if any."""

    messages = {item.message_id: item.content for item in input_payload.current_messages}

    for candidate in output.red_flag_candidates:
        failure = _verify_red_flag(candidate, messages)
        if failure is not None:
            return failure

    safety = output.patient_safety_delta
    for field_name, delta in (
        ("allergy", safety.allergy),
        ("medications", safety.medications),
        ("major_conditions", safety.major_conditions),
        ("contraindications", safety.contraindications),
    ):
        failure = _verify_list_safety(field_name, delta, messages)
        if failure is not None:
            return failure

    failure = _verify_pregnancy(safety.pregnancy, messages)
    if failure is not None:
        return failure
    return _verify_lactation(safety.lactation, messages)


def normalize_grounded_text(value: str) -> str:
    """Conservative NFKC/case/punctuation normalization used for matching."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def _verify_red_flag(
    candidate: RedFlagCandidate,
    messages: dict[UUID, str],
) -> IntakeGroundingFailureKind | None:
    message = _verified_message(candidate.span, candidate.source_message_id, messages)
    if message is None:
        return IntakeGroundingFailureKind.SPAN_INVALID
    if not _red_flag_quote_supports(candidate.category, candidate.span.quote):
        return IntakeGroundingFailureKind.VALUE_MISMATCH
    if not _affirmed_current_context(message, candidate.span, allow_history=False):
        return IntakeGroundingFailureKind.CONTEXT_UNSAFE
    return None


def _verify_list_safety(
    field_name: str,
    delta: SafetyListDelta,
    messages: dict[UUID, str],
) -> IntakeGroundingFailureKind | None:
    if delta.status is CollectionStatus.UNKNOWN:
        return None
    if delta.status is CollectionStatus.COLLECTED:
        assert delta.values is not None and delta.value_spans is not None
        assert delta.source_message_id is not None
        for value, span in zip(delta.values, delta.value_spans, strict=True):
            message = _verified_message(span, delta.source_message_id, messages)
            if message is None:
                return IntakeGroundingFailureKind.SPAN_INVALID
            if not _value_is_in_quote(value, span.quote):
                return IntakeGroundingFailureKind.VALUE_MISMATCH
            allow_history = field_name in {"allergy", "major_conditions"}
            if not _affirmed_current_context(message, span, allow_history=allow_history):
                return IntakeGroundingFailureKind.CONTEXT_UNSAFE
        return None

    assert delta.negation_span is not None and delta.source_message_id is not None
    span = delta.negation_span
    message = _verified_message(span, delta.source_message_id, messages)
    if message is None:
        return IntakeGroundingFailureKind.SPAN_INVALID
    if _LIST_EXPLICIT_NONE_PATTERNS[field_name].search(
        unicodedata.normalize("NFKC", span.quote)
    ) is None:
        return IntakeGroundingFailureKind.VALUE_MISMATCH
    allow_history = field_name in {"allergy", "major_conditions"}
    if not _explicit_negative_context(message, span, allow_history=allow_history):
        return IntakeGroundingFailureKind.CONTEXT_UNSAFE
    if _has_contrasting_affirmation(message, span, _LIST_FIELD_TERMS[field_name]):
        return IntakeGroundingFailureKind.CONTEXT_UNSAFE
    return None


def _verify_pregnancy(
    delta: PregnancyDelta,
    messages: dict[UUID, str],
) -> IntakeGroundingFailureKind | None:
    if delta.status is CollectionStatus.UNKNOWN:
        return None
    assert delta.span is not None and delta.source_message_id is not None
    message = _verified_message(delta.span, delta.source_message_id, messages)
    if message is None:
        return IntakeGroundingFailureKind.SPAN_INVALID
    if not _contains_any(delta.span.quote, _PREGNANCY_TERMS):
        return IntakeGroundingFailureKind.VALUE_MISMATCH

    if delta.status is CollectionStatus.EXPLICITLY_NONE or delta.value is PregnancyValue.NOT_PREGNANT:
        if not _explicit_negative_context(message, delta.span, allow_history=False):
            return IntakeGroundingFailureKind.CONTEXT_UNSAFE
        if _has_contrasting_affirmation(message, delta.span, _PREGNANCY_TERMS):
            return IntakeGroundingFailureKind.CONTEXT_UNSAFE
        return None
    if delta.value is PregnancyValue.POSSIBLE:
        return _verify_possible_context(message, delta.span)
    if not _affirmed_current_context(message, delta.span, allow_history=False):
        return IntakeGroundingFailureKind.CONTEXT_UNSAFE
    return None


def _verify_lactation(
    delta: LactationDelta,
    messages: dict[UUID, str],
) -> IntakeGroundingFailureKind | None:
    if delta.status is CollectionStatus.UNKNOWN:
        return None
    assert delta.span is not None and delta.source_message_id is not None
    message = _verified_message(delta.span, delta.source_message_id, messages)
    if message is None:
        return IntakeGroundingFailureKind.SPAN_INVALID
    if not _contains_any(delta.span.quote, _LACTATION_TERMS):
        return IntakeGroundingFailureKind.VALUE_MISMATCH
    if delta.status is CollectionStatus.EXPLICITLY_NONE or delta.value is LactationValue.NOT_LACTATING:
        if not _explicit_negative_context(message, delta.span, allow_history=False):
            return IntakeGroundingFailureKind.CONTEXT_UNSAFE
        if _has_contrasting_affirmation(message, delta.span, _LACTATION_TERMS):
            return IntakeGroundingFailureKind.CONTEXT_UNSAFE
        return None
    if not _affirmed_current_context(message, delta.span, allow_history=False):
        return IntakeGroundingFailureKind.CONTEXT_UNSAFE
    return None


def _verified_message(
    span: EvidenceSpan,
    expected_source: UUID,
    messages: dict[UUID, str],
) -> str | None:
    if span.source_message_id != expected_source:
        return None
    message = messages.get(span.source_message_id)
    if message is None or span.start_char < 0 or span.end_char > len(message):
        return None
    if span.start_char >= span.end_char or message[span.start_char : span.end_char] != span.quote:
        return None
    return message


def _value_is_in_quote(value: str, quote: str) -> bool:
    normalized_value = normalize_grounded_text(value)
    normalized_quote = normalize_grounded_text(quote)
    return bool(normalized_value) and normalized_value in normalized_quote


def _red_flag_quote_supports(category: RedFlagCategory, quote: str) -> bool:
    if _contains_any(quote, _RED_FLAG_TERMS[category]):
        return True
    if category is RedFlagCategory.HIGH_FEVER:
        return _HIGH_TEMPERATURE_PATTERN.search(unicodedata.normalize("NFKC", quote)) is not None
    if category is RedFlagCategory.BREATHING_DIFFICULTY:
        return _LOW_OXYGEN_PATTERN.search(unicodedata.normalize("NFKC", quote)) is not None
    return False


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    normalized = normalize_grounded_text(text)
    return any(normalize_grounded_text(term) in normalized for term in terms)


def _clause_for_span(message: str, span: EvidenceSpan) -> str:
    start = 0
    end = len(message)
    for boundary in _CLAUSE_BOUNDARY_PATTERN.finditer(message):
        if boundary.end() <= span.start_char:
            start = boundary.end()
            continue
        if boundary.start() >= span.end_char:
            end = boundary.start()
            break
    return message[start:end]


def _affirmed_current_context(
    message: str,
    span: EvidenceSpan,
    *,
    allow_history: bool,
) -> bool:
    clause = _clause_for_span(message, span)
    if (
        _HYPOTHETICAL_PATTERN.search(clause)
        or _UNCERTAINTY_PATTERN.search(clause)
        or _THIRD_PERSON_PATTERN.search(clause)
        or _NEGATION_PATTERN.search(clause)
    ):
        return False
    return allow_history or not bool(
        _HISTORICAL_PATTERN.search(clause) or _RESOLVED_PATTERN.search(clause)
    )


def _explicit_negative_context(
    message: str,
    span: EvidenceSpan,
    *,
    allow_history: bool,
) -> bool:
    clause = _clause_for_span(message, span)
    if (
        _NEGATION_PATTERN.search(clause) is None
        or _HYPOTHETICAL_PATTERN.search(clause)
        or _UNCERTAINTY_PATTERN.search(clause)
        or _THIRD_PERSON_PATTERN.search(clause)
    ):
        return False
    return allow_history or not bool(
        _HISTORICAL_PATTERN.search(clause) or _RESOLVED_PATTERN.search(clause)
    )


def _verify_possible_context(
    message: str,
    span: EvidenceSpan,
) -> IntakeGroundingFailureKind | None:
    clause = _clause_for_span(message, span)
    if (
        _UNCERTAINTY_PATTERN.search(clause) is None
        or _NEGATION_PATTERN.search(clause)
        or _HISTORICAL_PATTERN.search(clause)
        or _HYPOTHETICAL_PATTERN.search(clause)
        or _RESOLVED_PATTERN.search(clause)
        or _THIRD_PERSON_PATTERN.search(clause)
    ):
        return IntakeGroundingFailureKind.CONTEXT_UNSAFE
    return None


def _has_contrasting_affirmation(
    message: str,
    span: EvidenceSpan,
    terms: tuple[str, ...],
) -> bool:
    if _CONTRAST_PATTERN.search(message) is None:
        return False
    segment_start = 0
    boundaries = (*_CLAUSE_BOUNDARY_PATTERN.finditer(message), None)
    for boundary in boundaries:
        segment_end = boundary.start() if boundary is not None else len(message)
        segment = message[segment_start:segment_end]
        if segment_start <= span.start_char and span.end_char <= segment_end:
            pass
        elif _contains_any(segment, terms) and _NEGATION_PATTERN.search(segment) is None:
            return True
        segment_start = boundary.end() if boundary is not None else len(message)
    return False
