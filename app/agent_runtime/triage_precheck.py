"""Versioned deterministic raw-text red-flag precheck.

This module is deliberately model-free and side-effect free.  It is a high-
recall safety net before intake extraction: model candidates may add signal, but
they can never remove or downgrade a candidate emitted here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.intake import CandidateSeverity, EvidenceSpan, RedFlagCandidate, RedFlagCategory

TRIAGE_PRECHECK_VERSION = "triage-raw-text-precheck.v1"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: ClassVar[str] = TRIAGE_PRECHECK_VERSION


class PrecheckDisposition(StrEnum):
    CLEAR = "clear"
    RED_FLAG = "red_flag"
    MANUAL_REVIEW = "manual_review"


class PrecheckAssertion(StrEnum):
    AFFIRMED = "affirmed"
    UNCERTAIN = "uncertain"


class PrecheckContext(StrEnum):
    CURRENT = "current"
    UNCERTAIN = "uncertain"
    HISTORICAL = "historical"
    HYPOTHETICAL = "hypothetical"
    RESOLVED = "resolved"
    THIRD_PERSON = "third_person"
    SYSTEM_ERROR = "system_error"


class TriagePrecheckRule(_FrozenModel):
    rule_id: str = Field(min_length=1, max_length=96)
    category: RedFlagCategory
    phrases: tuple[str, ...] = Field(min_length=1)


class TriagePrecheckMatch(_FrozenModel):
    rule_id: str = Field(min_length=1, max_length=96)
    category: RedFlagCategory
    source_message_id: UUID
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    quote: str = Field(max_length=240)
    assertion: PrecheckAssertion
    context: PrecheckContext

    @model_validator(mode="after")
    def valid_span(self) -> TriagePrecheckMatch:
        if self.end_char < self.start_char:
            raise ValueError("precheck evidence span is reversed")
        if self.context is not PrecheckContext.SYSTEM_ERROR and self.end_char == self.start_char:
            raise ValueError("precheck evidence span must not be empty")
        return self


class TriagePrecheckResult(_FrozenModel):
    disposition: PrecheckDisposition
    candidates: tuple[RedFlagCandidate, ...] = ()
    matched_rule_ids: tuple[str, ...] = ()
    matches: tuple[TriagePrecheckMatch, ...] = ()

    @model_validator(mode="after")
    def disposition_matches_evidence(self) -> TriagePrecheckResult:
        expected_rule_ids = tuple(sorted({item.rule_id for item in self.matches}))
        if self.matched_rule_ids != expected_rule_ids:
            raise ValueError("precheck rule ids must be derived from evidence matches")
        if self.disposition is PrecheckDisposition.CLEAR:
            if self.candidates or self.matches:
                raise ValueError("clear precheck cannot carry candidates or matches")
        elif self.disposition is PrecheckDisposition.RED_FLAG:
            if not self.candidates or not any(
                item.assertion is PrecheckAssertion.AFFIRMED for item in self.matches
            ):
                raise ValueError("red-flag precheck requires affirmed evidence")
        elif not self.candidates or any(item.category is not RedFlagCategory.OTHER for item in self.candidates):
            raise ValueError("manual-review precheck requires an OTHER candidate")
        return self


_NEGATION_PATTERN = re.compile(
    r"(?:没有|没|无|否认|未见|未出现|不伴|并无|从未|\bnot\b|\bno\b|\bwithout\b|\bden(?:y|ies|ied)\b)",
    re.IGNORECASE,
)
_UNCERTAINTY_PATTERN = re.compile(
    r"(?:不确定|说不清|不清楚|可能|也许|好像|似乎|是否|有没有|uncertain|maybe|possibly|unsure)",
    re.IGNORECASE,
)
_TEMPERATURE_PATTERN = re.compile(
    r"(?<!\d)(?:(3[5-9](?:\.\d)?|4[0-5](?:\.\d)?)\s*(?:°\s*c|℃|摄氏度|度)|"
    r"(9[5-9](?:\.\d)?|10\d(?:\.\d)?|11[0-3](?:\.\d)?)\s*°?\s*f)",
    re.IGNORECASE,
)
_OXYGEN_PATTERN = re.compile(
    r"(?:(?:血氧(?:饱和度)?|spo2|oxygen saturation)\s*(?:为|是|:|=)?\s*(\d{2,3}(?:\.\d+)?)\s*%|"
    r"(\d{2,3}(?:\.\d+)?)\s*%\s*(?:血氧(?:饱和度)?|spo2|oxygen saturation))",
    re.IGNORECASE,
)
_NEGATION_SCOPE_BOUNDARY = re.compile(r"[，。；;,.!?！？]|(?:但是|但|却|不过|however|but)", re.IGNORECASE)
_POST_NEGATION_PATTERN = re.compile(
    r"^\s*(?:没有|没|无|否认|未见|未出现|不伴|并无|\bnot\b|\bno\b|\bwithout\b)",
    re.IGNORECASE,
)
_HISTORICAL_PATTERN = re.compile(r"(?:曾经|以前|过去|既往|此前|history|previously|used to)", re.IGNORECASE)
_HYPOTHETICAL_PATTERN = re.compile(r"(?:如果|假如|万一|要是|if|what if|in case)", re.IGNORECASE)
_RESOLVED_PATTERN = re.compile(
    r"(?:已缓解|已经缓解|已经好了|目前无|现在没有|现已消失|resolved|has stopped|went away)",
    re.IGNORECASE,
)
_THIRD_PERSON_PATTERN = re.compile(
    r"(?:我家人|别人|朋友|同事|旁人|someone else|my friend|my colleague)",
    re.IGNORECASE,
)


def _has_negation_scope(prefix: str) -> bool:
    clause = _NEGATION_SCOPE_BOUNDARY.split(prefix)[-1]
    return _NEGATION_PATTERN.search(clause) is not None


def _normalize_with_source_map(text: str) -> tuple[str, tuple[int, ...]]:
    normalized_parts: list[str] = []
    source_indices: list[int] = []
    for index, character in enumerate(text):
        normalized_character = unicodedata.normalize("NFKC", character).casefold()
        normalized_parts.append(normalized_character)
        source_indices.extend([index] * len(normalized_character))
    return "".join(normalized_parts), tuple(source_indices)


def _source_span(source_map: tuple[int, ...], start: int, end: int) -> tuple[int, int]:
    return source_map[start], source_map[end - 1] + 1


def _uncertain_context(prefix: str, suffix: str) -> PrecheckContext | None:
    scope = f"{prefix}{suffix[:18]}"
    if _HYPOTHETICAL_PATTERN.search(scope):
        return PrecheckContext.HYPOTHETICAL
    if _HISTORICAL_PATTERN.search(scope):
        return PrecheckContext.HISTORICAL
    if _RESOLVED_PATTERN.search(scope):
        return PrecheckContext.RESOLVED
    if _THIRD_PERSON_PATTERN.search(scope):
        return PrecheckContext.THIRD_PERSON
    if _UNCERTAINTY_PATTERN.search(scope) or "?" in suffix[:4] or "？" in suffix[:4]:
        return PrecheckContext.UNCERTAIN
    return None


def _make_rule_evaluator() -> tuple[
    tuple[TriagePrecheckRule, ...],
    Callable[[UUID, str], TriagePrecheckResult],
]:
    # The evaluator closes over the immutable tuple.  Rebinding the exported
    # registry cannot alter production decisions.
    rules = (
        TriagePrecheckRule(
            rule_id="raw_text.severe_pain.v1",
            category=RedFlagCategory.SEVERE_PAIN,
            phrases=(
                "胸痛",
                "心前区痛",
                "剧烈疼痛",
                "撕裂样痛",
                "刀割样痛",
                "severe pain",
                "chest pain",
                "crushing pain",
            ),
        ),
        TriagePrecheckRule(
            rule_id="raw_text.breathing_difficulty.v1",
            category=RedFlagCategory.BREATHING_DIFFICULTY,
            phrases=(
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
        ),
        TriagePrecheckRule(
            rule_id="raw_text.altered_consciousness.v1",
            category=RedFlagCategory.ALTERED_CONSCIOUSNESS,
            phrases=(
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
        ),
        TriagePrecheckRule(
            rule_id="raw_text.severe_bleeding.v1",
            category=RedFlagCategory.SEVERE_BLEEDING,
            phrases=(
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
        ),
        TriagePrecheckRule(
            rule_id="raw_text.neurologic_deficit.v1",
            category=RedFlagCategory.NEUROLOGIC_DEFICIT,
            phrases=(
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
        ),
        TriagePrecheckRule(
            rule_id="raw_text.high_fever.phrase.v1",
            category=RedFlagCategory.HIGH_FEVER,
            phrases=("高热", "高烧", "超高热", "high fever"),
        ),
    )

    def evaluate(message_id: UUID, text: str) -> TriagePrecheckResult:
        normalized, source_map = _normalize_with_source_map(text)
        definitive: dict[RedFlagCategory, str] = {}
        matches: list[TriagePrecheckMatch] = []

        for rule in rules:
            for phrase in rule.phrases:
                start = normalized.find(phrase.casefold())
                while start >= 0:
                    prefix = normalized[max(0, start - 18) : start]
                    suffix = normalized[start + len(phrase) : start + len(phrase) + 18]
                    context = _uncertain_context(prefix, suffix)
                    if context is not None:
                        source_start, source_end = _source_span(source_map, start, start + len(phrase))
                        matches.append(
                            TriagePrecheckMatch(
                                rule_id=rule.rule_id,
                                category=rule.category,
                                source_message_id=message_id,
                                start_char=source_start,
                                end_char=source_end,
                                quote=text[source_start:source_end],
                                assertion=PrecheckAssertion.UNCERTAIN,
                                context=context,
                            )
                        )
                    elif not _has_negation_scope(prefix) and not _POST_NEGATION_PATTERN.search(suffix):
                        definitive.setdefault(rule.category, rule.rule_id)
                        source_start, source_end = _source_span(source_map, start, start + len(phrase))
                        matches.append(
                            TriagePrecheckMatch(
                                rule_id=rule.rule_id,
                                category=rule.category,
                                source_message_id=message_id,
                                start_char=source_start,
                                end_char=source_end,
                                quote=text[source_start:source_end],
                                assertion=PrecheckAssertion.AFFIRMED,
                                context=PrecheckContext.CURRENT,
                            )
                        )
                    start = normalized.find(phrase.casefold(), start + len(phrase))

        for match in _TEMPERATURE_PATTERN.finditer(normalized):
            celsius_group, fahrenheit_group = match.group(1), match.group(2)
            value = float(celsius_group) if celsius_group is not None else (float(fahrenheit_group) - 32) * 5 / 9
            prefix = normalized[max(0, match.start() - 18) : match.start()]
            suffix = normalized[match.end() : match.end() + 18]
            if value >= 39.5:
                rule_id = "raw_text.high_fever.temperature.v1"
                context = _uncertain_context(prefix, suffix)
                if context is not None:
                    assertion = PrecheckAssertion.UNCERTAIN
                elif _has_negation_scope(prefix) or _POST_NEGATION_PATTERN.search(suffix):
                    continue
                else:
                    assertion = PrecheckAssertion.AFFIRMED
                    definitive.setdefault(RedFlagCategory.HIGH_FEVER, rule_id)
                source_start, source_end = _source_span(source_map, match.start(), match.end())
                matches.append(
                    TriagePrecheckMatch(
                        rule_id=rule_id,
                        category=RedFlagCategory.HIGH_FEVER,
                        source_message_id=message_id,
                        start_char=source_start,
                        end_char=source_end,
                        quote=text[source_start:source_end],
                        assertion=assertion,
                        context=context or PrecheckContext.CURRENT,
                    )
                )

        for match in _OXYGEN_PATTERN.finditer(normalized):
            raw_value = match.group(1) or match.group(2)
            value = float(raw_value)
            if not 50 <= value < 90:
                continue
            rule_id = "raw_text.low_oxygen_saturation.v1"
            prefix = normalized[max(0, match.start() - 18) : match.start()]
            suffix = normalized[match.end() : match.end() + 18]
            context = _uncertain_context(prefix, suffix)
            if context is not None:
                assertion = PrecheckAssertion.UNCERTAIN
            elif _has_negation_scope(prefix) or _POST_NEGATION_PATTERN.search(suffix):
                continue
            else:
                assertion = PrecheckAssertion.AFFIRMED
                definitive.setdefault(RedFlagCategory.BREATHING_DIFFICULTY, rule_id)
            source_start, source_end = _source_span(source_map, match.start(), match.end())
            matches.append(
                TriagePrecheckMatch(
                    rule_id=rule_id,
                    category=RedFlagCategory.BREATHING_DIFFICULTY,
                    source_message_id=message_id,
                    start_char=source_start,
                    end_char=source_end,
                    quote=text[source_start:source_end],
                    assertion=assertion,
                    context=context or PrecheckContext.CURRENT,
                )
            )

        evidence_matches = tuple(matches)
        candidates = tuple(
            RedFlagCandidate(
                category=category,
                source_message_id=message_id,
                span=EvidenceSpan(
                    source_message_id=message_id,
                    start_char=matched.start_char,
                    end_char=matched.end_char,
                    quote=matched.quote,
                ),
                severity=CandidateSeverity.HIGH,
                evidence=f"deterministic:{rule_id}",
                confidence=1.0,
            )
            for category, rule_id in sorted(definitive.items(), key=lambda item: item[0].value)
            for matched in (
                next(
                    item
                    for item in evidence_matches
                    if item.category is category and item.assertion is PrecheckAssertion.AFFIRMED
                ),
            )
        )
        matched_ids = tuple(sorted({item.rule_id for item in evidence_matches}))
        if candidates:
            return TriagePrecheckResult(
                disposition=PrecheckDisposition.RED_FLAG,
                candidates=candidates,
                matched_rule_ids=matched_ids,
                matches=evidence_matches,
            )
        if evidence_matches:
            matched = evidence_matches[0]
            manual_candidate = RedFlagCandidate(
                category=RedFlagCategory.OTHER,
                source_message_id=message_id,
                span=EvidenceSpan(
                    source_message_id=message_id,
                    start_char=matched.start_char,
                    end_char=matched.end_char,
                    quote=matched.quote,
                ),
                severity=CandidateSeverity.MEDIUM,
                evidence="deterministic:raw_text.uncertain_red_flag.v1",
                confidence=1.0,
            )
            return TriagePrecheckResult(
                disposition=PrecheckDisposition.MANUAL_REVIEW,
                candidates=(manual_candidate,),
                matched_rule_ids=matched_ids,
                matches=evidence_matches,
            )
        return TriagePrecheckResult(disposition=PrecheckDisposition.CLEAR)

    return rules, evaluate


TRIAGE_PRECHECK_RULES, _EVALUATE_RULES = _make_rule_evaluator()


def evaluate_raw_text_triage_precheck(message_id: UUID, text: str) -> TriagePrecheckResult:
    """Evaluate one raw patient message without a model or external service."""

    if not text:
        raise ValueError("triage precheck text must not be empty")
    try:
        return _EVALUATE_RULES(message_id, text)
    except Exception:
        # Any rule/evaluator fault is itself an uncertain safety result.  The
        # public boundary must never fail open or strand a running claim.
        rule_id = "raw_text.precheck_internal_error.v1"
        error_match = TriagePrecheckMatch(
            rule_id=rule_id,
            category=RedFlagCategory.OTHER,
            source_message_id=message_id,
            start_char=0,
            end_char=0,
            quote="",
            assertion=PrecheckAssertion.UNCERTAIN,
            context=PrecheckContext.SYSTEM_ERROR,
        )
        return TriagePrecheckResult(
            disposition=PrecheckDisposition.MANUAL_REVIEW,
            candidates=(
                RedFlagCandidate(
                    category=RedFlagCategory.OTHER,
                    source_message_id=message_id,
                    span=EvidenceSpan(
                        source_message_id=message_id,
                        start_char=0,
                        end_char=len(text),
                        quote=text,
                    ),
                    severity=CandidateSeverity.MEDIUM,
                    evidence=f"deterministic:{rule_id}",
                    confidence=1.0,
                ),
            ),
            matched_rule_ids=(rule_id,),
            matches=(error_match,),
        )


def merge_red_flag_candidates(
    deterministic: tuple[RedFlagCandidate, ...],
    model_candidates: tuple[RedFlagCandidate, ...],
) -> tuple[RedFlagCandidate, ...]:
    """Merge candidates while making deterministic findings authoritative."""

    merged: dict[tuple[RedFlagCategory, UUID], RedFlagCandidate] = {
        (item.category, item.source_message_id): item for item in deterministic
    }
    for item in model_candidates:
        key = (item.category, item.source_message_id)
        if key not in merged:
            merged[key] = item
    return tuple(sorted(merged.values(), key=lambda item: (item.category.value, str(item.source_message_id))))
