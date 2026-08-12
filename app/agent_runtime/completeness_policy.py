"""Pure deterministic L3-3 completeness and stagnation policy."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent_runtime.intake_dimension_mapping import (
    derived_coverage_for_fact_keys,
    dimension_acquired_key_count,
    slot_threshold_for,
)
from app.agent_runtime.observation_projection import project_current_observations
from app.schemas.completeness import (
    COMPLETENESS_GATE_NAME,
    COMPLETENESS_POLICY_VERSION,
    ApplicabilityStatus,
    ComplaintCategory,
    CompletenessApplicabilityResult,
    CompletenessConflict,
    CompletenessDimensionRule,
    CompletenessDisposition,
    CompletenessGateDetails,
    CompletenessGateResult,
    CompletenessObservationFact,
    CompletenessPolicyInput,
    CompletenessPolicyResult,
    CompletenessProgress,
    CompletenessRuleOutcome,
    CompletenessStagnationResult,
    InquiryDimension,
    StagnationReasonCode,
)
from app.schemas.domain import CollectionStatus, GateDecision, GateResultSchema
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION, TriageDisposition


class CompletenessPolicyFailureCode(StrEnum):
    INPUT_SCHEMA_INVALID = "COMPLETENESS_INPUT_SCHEMA_INVALID"
    INPUT_AUTHORITY_FIELD_FORBIDDEN = "COMPLETENESS_INPUT_AUTHORITY_FIELD_FORBIDDEN"
    TRIAGE_GATE_MISMATCH = "COMPLETENESS_TRIAGE_GATE_MISMATCH"


class CompletenessPolicyInputError(ValueError):
    """Fixed-code rejection from completeness input canonical reconstruction."""

    def __init__(self, code: CompletenessPolicyFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


class CompletenessPolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = COMPLETENESS_POLICY_VERSION
    no_new_facts_round_threshold: int = Field(default=2, ge=1, le=20)
    # The largest supported intake can require 12 distinct dimensions before
    # any clarification retries.  Keep a separate high hard-stop for runaway
    # dialogue; ordinary progress must not be blocked after only six questions.
    max_followup_rounds: int = Field(default=20, ge=1, le=50)


class ComplaintTenQuestionRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    complaint_category: ComplaintCategory
    rule_id: str = Field(min_length=1, max_length=96)
    dimensions: tuple[InquiryDimension, ...] = Field(min_length=1)


class CompletenessConflictRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1, max_length=96)
    dimension: InquiryDimension
    fact_keys: tuple[str, ...] = Field(min_length=1)


class FrozenCompletenessRuleRegistry(Mapping[InquiryDimension, CompletenessDimensionRule]):
    """Immutable dimension rule registry backed only by a tuple of frozen rules."""

    __slots__ = ("_rules",)
    _rules: tuple[CompletenessDimensionRule, ...]

    def __init__(self, rules: tuple[CompletenessDimensionRule, ...]) -> None:
        dimensions = tuple(rule.dimension for rule in rules)
        if len(dimensions) != len(frozenset(dimensions)):
            raise ValueError("completeness rule dimensions must be unique")
        object.__setattr__(self, "_rules", rules)

    def __getitem__(self, dimension: InquiryDimension) -> CompletenessDimensionRule:
        for rule in self._rules:
            if rule.dimension is dimension:
                return rule
        raise KeyError(dimension)

    def __iter__(self) -> Iterator[InquiryDimension]:
        return (rule.dimension for rule in self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("completeness rule registry is immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("completeness rule registry is immutable")


COMPLETENESS_POLICY_CONFIG = CompletenessPolicyConfig()

COMPLETENESS_DIMENSION_RULES: Mapping[InquiryDimension, CompletenessDimensionRule] = (
    FrozenCompletenessRuleRegistry(
        (
            CompletenessDimensionRule(
                rule_id="completeness.chief_complaint.symptom.v1",
                dimension=InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
                fact_keys=("chief_complaint.symptom",),
                required_by_default=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.chief_complaint.course.v1",
                dimension=InquiryDimension.BASIC_COURSE,
                fact_keys=("chief_complaint.course", "chief_complaint.duration", "onset.duration"),
                required_by_default=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.present_illness.change.v1",
                dimension=InquiryDimension.PRESENT_ILLNESS_CHANGE,
                fact_keys=("present_illness.change", "present_illness.associated_symptom"),
                required_by_default=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.cold_heat.v1",
                dimension=InquiryDimension.TEN_COLD_HEAT,
                fact_keys=("ten_questions.cold_heat",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.sweat.v1",
                dimension=InquiryDimension.TEN_SWEAT,
                fact_keys=("ten_questions.sweat",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.head_body.v1",
                dimension=InquiryDimension.TEN_HEAD_BODY,
                fact_keys=("ten_questions.head_body",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.stool_urine.v1",
                dimension=InquiryDimension.TEN_STOOL_URINE,
                fact_keys=("ten_questions.stool_urine", "ten_questions.stool", "ten_questions.urine"),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.diet.v1",
                dimension=InquiryDimension.TEN_DIET,
                fact_keys=("ten_questions.diet", "ten_questions.appetite"),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.chest_abdomen.v1",
                dimension=InquiryDimension.TEN_CHEST_ABDOMEN,
                fact_keys=("ten_questions.chest_abdomen", "ten_questions.abdomen"),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.thirst.v1",
                dimension=InquiryDimension.TEN_THIRST,
                fact_keys=("ten_questions.thirst",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.sleep.v1",
                dimension=InquiryDimension.TEN_SLEEP,
                fact_keys=("ten_questions.sleep",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.menses_leukorrhea.v1",
                dimension=InquiryDimension.TEN_MENSES_LEUKORRHEA,
                fact_keys=("ten_questions.menses_leukorrhea", "ten_questions.menses"),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.pain.v1",
                dimension=InquiryDimension.TEN_PAIN,
                fact_keys=("ten_questions.pain",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.ten_questions.respiratory.v1",
                dimension=InquiryDimension.TEN_RESPIRATORY,
                fact_keys=("ten_questions.respiratory",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.safety.allergy_status.v1",
                dimension=InquiryDimension.ALLERGY_STATUS,
                required_by_default=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.safety.medication_status.v1",
                dimension=InquiryDimension.MEDICATION_STATUS,
                required_by_default=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.safety.major_condition_status.v1",
                dimension=InquiryDimension.MAJOR_CONDITION_STATUS,
                required_by_default=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.safety.pregnancy_status.v1",
                dimension=InquiryDimension.PREGNANCY_STATUS,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.safety.lactation_status.v1",
                dimension=InquiryDimension.LACTATION_STATUS,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.past_history.v1",
                dimension=InquiryDimension.PAST_HISTORY,
                fact_keys=("past_history",),
                optional_report=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.four_diagnosis.v1",
                dimension=InquiryDimension.FOUR_DIAGNOSIS,
                fact_keys=("four_diagnosis.inspection", "four_diagnosis.palpation"),
                required_by_default=True,
            ),
            CompletenessDimensionRule(
                rule_id="completeness.patient.sex.v1",
                dimension=InquiryDimension.PATIENT_SEX,
                fact_keys=("patient.sex", "patient.gender"),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.patient.age.v1",
                dimension=InquiryDimension.PATIENT_AGE,
                fact_keys=("patient.age",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.patient.menopause_status.v1",
                dimension=InquiryDimension.MENOPAUSE_STATUS,
                fact_keys=("patient.menopause", "patient.menopause_status"),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.patient.pregnancy_applicability.v1",
                dimension=InquiryDimension.PREGNANCY_APPLICABILITY_FLAG,
                fact_keys=("patient.pregnancy_applicable",),
            ),
            CompletenessDimensionRule(
                rule_id="completeness.patient.lactation_applicability.v1",
                dimension=InquiryDimension.LACTATION_APPLICABILITY_FLAG,
                fact_keys=("patient.lactation_applicable",),
            ),
        )
    )
)

# 十问核心基座：所有主诉大类都至少采集寒热、汗出、头身、二便、饮食、胸腹、口渴、睡眠。
# 分类专属维度（呼吸/疼痛/月经带下）在基座之上按大类追加。
# 真实问诊复盘（session 69862530-...）：旧表仅激活 2-4 项十问维度，8 轮即 READY，
# 汗出/头身/口渴/胸腹等十问要素全部漏采；此处按传统十问歌补全核心采集集。
_TEN_QUESTION_BASE_CORE: tuple[InquiryDimension, ...] = (
    InquiryDimension.TEN_COLD_HEAT,
    InquiryDimension.TEN_SWEAT,
    InquiryDimension.TEN_HEAD_BODY,
    InquiryDimension.TEN_STOOL_URINE,
    InquiryDimension.TEN_DIET,
    InquiryDimension.TEN_CHEST_ABDOMEN,
    InquiryDimension.TEN_THIRST,
    InquiryDimension.TEN_SLEEP,
)

COMPLETENESS_COMPLAINT_TEN_QUESTION_RULES: tuple[ComplaintTenQuestionRule, ...] = (
    ComplaintTenQuestionRule(
        complaint_category=ComplaintCategory.RESPIRATORY,
        rule_id="completeness.dynamic_ten_questions.respiratory.v1",
        dimensions=(*_TEN_QUESTION_BASE_CORE, InquiryDimension.TEN_RESPIRATORY),
    ),
    ComplaintTenQuestionRule(
        complaint_category=ComplaintCategory.DIGESTIVE,
        rule_id="completeness.dynamic_ten_questions.digestive.v1",
        dimensions=_TEN_QUESTION_BASE_CORE,
    ),
    ComplaintTenQuestionRule(
        complaint_category=ComplaintCategory.PAIN,
        rule_id="completeness.dynamic_ten_questions.pain.v1",
        dimensions=(*_TEN_QUESTION_BASE_CORE, InquiryDimension.TEN_PAIN),
    ),
    ComplaintTenQuestionRule(
        complaint_category=ComplaintCategory.GYNECOLOGIC,
        rule_id="completeness.dynamic_ten_questions.gynecologic.v1",
        dimensions=(*_TEN_QUESTION_BASE_CORE, InquiryDimension.TEN_MENSES_LEUKORRHEA),
    ),
    ComplaintTenQuestionRule(
        complaint_category=ComplaintCategory.URINARY,
        rule_id="completeness.dynamic_ten_questions.urinary.v1",
        dimensions=_TEN_QUESTION_BASE_CORE,
    ),
    ComplaintTenQuestionRule(
        complaint_category=ComplaintCategory.GENERAL,
        rule_id="completeness.dynamic_ten_questions.general.v1",
        dimensions=_TEN_QUESTION_BASE_CORE,
    ),
)

COMPLETENESS_CONFLICT_RULES: tuple[CompletenessConflictRule, ...] = (
    CompletenessConflictRule(
        rule_id="completeness.conflict.chief_complaint.category.v1",
        dimension=InquiryDimension.CHIEF_COMPLAINT_CATEGORY,
        fact_keys=("chief_complaint.category",),
    ),
    CompletenessConflictRule(
        rule_id="completeness.conflict.chief_complaint.symptom.v1",
        dimension=InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
        fact_keys=("chief_complaint.symptom",),
    ),
    CompletenessConflictRule(
        rule_id="completeness.conflict.chief_complaint.course_aliases.v1",
        dimension=InquiryDimension.BASIC_COURSE,
        fact_keys=("chief_complaint.course", "chief_complaint.duration", "onset.duration"),
    ),
    CompletenessConflictRule(
        rule_id="completeness.conflict.patient.sex_aliases.v1",
        dimension=InquiryDimension.PATIENT_SEX,
        fact_keys=("patient.sex", "patient.gender"),
    ),
    CompletenessConflictRule(
        rule_id="completeness.conflict.patient.menopause_aliases.v1",
        dimension=InquiryDimension.MENOPAUSE_STATUS,
        fact_keys=("patient.menopause", "patient.menopause_status"),
    ),
    CompletenessConflictRule(
        rule_id="completeness.conflict.patient.pregnancy_applicability.v1",
        dimension=InquiryDimension.PREGNANCY_APPLICABILITY_FLAG,
        fact_keys=("patient.pregnancy_applicable",),
    ),
    CompletenessConflictRule(
        rule_id="completeness.conflict.patient.lactation_applicability.v1",
        dimension=InquiryDimension.LACTATION_APPLICABILITY_FLAG,
        fact_keys=("patient.lactation_applicable",),
    ),
)
COMPLETENESS_SAME_FACT_KEY_CONFLICT_RULE_ID = "completeness.conflict.same_canonical_fact_key.v1"
COMPLETENESS_AUXILIARY_FACT_DIMENSIONS: tuple[tuple[str, InquiryDimension], ...] = (
    ("chief_complaint.category", InquiryDimension.CHIEF_COMPLAINT_CATEGORY),
)


def canonicalize_completeness_input(input_payload: object) -> CompletenessPolicyInput:
    """Rebuild the exact base DTO and reject hidden or constructed fields."""

    try:
        candidate = CompletenessPolicyInput.model_validate(input_payload)
        canonical_json = CompletenessPolicyInput.__pydantic_serializer__.to_json(
            candidate,
            warnings=False,
        )
        canonical = CompletenessPolicyInput.model_validate_json(canonical_json)
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        raise CompletenessPolicyInputError(CompletenessPolicyFailureCode.INPUT_SCHEMA_INVALID) from exc
    if _has_undeclared_fields(input_payload, canonical):
        raise CompletenessPolicyInputError(CompletenessPolicyFailureCode.INPUT_AUTHORITY_FIELD_FORBIDDEN)
    if (
        canonical.triage_gate.gate_name != TRIAGE_GATE_NAME
        or canonical.triage_gate.policy_version != TRIAGE_POLICY_VERSION
        or canonical.triage_gate.input_state_version != canonical.input_state_version
        or not _triage_gate_is_internally_consistent(canonical.triage_gate)
    ):
        raise CompletenessPolicyInputError(CompletenessPolicyFailureCode.TRIAGE_GATE_MISMATCH)
    return canonical


def evaluate_completeness_policy(input_payload: object) -> CompletenessPolicyResult:
    """Return a deterministic authoritative completeness GateResult."""

    policy_input = canonicalize_completeness_input(input_payload)
    current_facts = _current_facts(policy_input.domain_snapshot.observations)
    facts_by_dimension = _facts_by_dimension(current_facts)
    conflicts = _conflicts_by_dimension(current_facts)
    covered = _covered_dimensions(current_facts, facts_by_dimension, policy_input)
    applicability = _applicability(facts_by_dimension)
    required = _required_dimensions(policy_input, facts_by_dimension, current_facts, applicability)
    missing_required = tuple(sorted((dim for dim in required if dim not in covered), key=lambda item: item.value))
    missing_optional = _missing_optional_dimensions(covered)
    # 2d(决策 11): cap 到分流——缺安全维度 → 转人工(铁律 9),否则落 partial 推进。
    safety_missing = any(_is_safety_dimension(dim) for dim in missing_required)
    stagnation = _stagnation(
        policy_input.progress,
        safety_missing=safety_missing,
        missing_required=missing_required,
    )
    rule_outcomes = _rule_outcomes(required, covered, conflicts)
    disposition = _disposition(
        policy_input,
        stagnation,
        conflicts,
        missing_required,
        contract_partial=bool(policy_input.contract_partial_dimensions),
    )
    decision = _decision_for_disposition(disposition)
    details = CompletenessGateDetails(
        disposition=disposition,
        covered_dimensions=covered,
        missing_required=missing_required,
        missing_optional=missing_optional,
        conflicting_dimensions=conflicts,
        rule_ids=tuple(outcome.rule_id for outcome in rule_outcomes),
        rule_outcomes=rule_outcomes,
        stagnation=stagnation,
        applicability=applicability,
        triage_disposition=_triage_disposition(policy_input),
        contract_open_dimensions=policy_input.contract_open_dimensions,
        contract_resolved_dimensions=policy_input.contract_resolved_dimensions,
        contract_partial_dimensions=policy_input.contract_partial_dimensions,
    )
    gate_result = CompletenessGateResult(
        gate_name=COMPLETENESS_GATE_NAME,
        policy_version=COMPLETENESS_POLICY_VERSION,
        input_state_version=policy_input.input_state_version,
        decision=decision,
        details=details,
    )
    return CompletenessPolicyResult(
        disposition=disposition,
        input_state_version=policy_input.input_state_version,
        covered_dimensions=covered,
        missing_required=missing_required,
        missing_optional=missing_optional,
        conflicting_dimensions=conflicts,
        stagnation=stagnation,
        gate_result=gate_result,
        rule_outcomes=rule_outcomes,
        contract_open_dimensions=policy_input.contract_open_dimensions,
        contract_resolved_dimensions=policy_input.contract_resolved_dimensions,
        contract_partial_dimensions=policy_input.contract_partial_dimensions,
    )


def completeness_gate_result(input_payload: object) -> CompletenessGateResult:
    """Stable L3-5 integration point for graph adapters."""

    return evaluate_completeness_policy(input_payload).gate_result


def completeness_to_gate_result_schema(
    result: CompletenessPolicyResult | CompletenessGateResult,
) -> GateResultSchema:
    """Create an explicit mutable compatibility DTO from immutable authority."""

    gate = result.gate_result if isinstance(result, CompletenessPolicyResult) else result
    details = gate.details
    return GateResultSchema(
        gate_name=gate.gate_name,
        policy_version=gate.policy_version,
        input_state_version=gate.input_state_version,
        decision=gate.decision,
        details={
            "disposition": details.disposition.value,
            "covered_dimensions": [item.value for item in details.covered_dimensions],
            "missing_required": [item.value for item in details.missing_required],
            "missing_optional": [item.value for item in details.missing_optional],
            "conflicting_dimensions": [
                {
                    "dimension": item.dimension.value,
                    "rule_id": item.rule_id,
                    "current_value_count": item.current_value_count,
                }
                for item in details.conflicting_dimensions
            ],
            "rule_ids": list(details.rule_ids),
            "stagnation": details.stagnation.model_dump(mode="json"),
            "applicability": details.applicability.model_dump(mode="json"),
            "triage_disposition": details.triage_disposition,
            "contract_open_dimensions": [
                item.value for item in details.contract_open_dimensions
            ],
            "contract_resolved_dimensions": [
                item.value for item in details.contract_resolved_dimensions
            ],
            "contract_partial_dimensions": [
                item.value for item in details.contract_partial_dimensions
            ],
        },
    )


def _current_facts(
    observations: tuple[CompletenessObservationFact, ...],
) -> tuple[CompletenessObservationFact, ...]:
    # R2-B1: current semantic chain heads (CORRECTED successors count, superseded
    # targets and RETRACTED heads do not) come from the single shared projection.
    return tuple(project_current_observations(observations))


def _facts_by_dimension(
    facts: tuple[CompletenessObservationFact, ...],
) -> tuple[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]], ...]:
    grouped: list[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]]] = []
    for dimension in sorted(COMPLETENESS_DIMENSION_RULES, key=lambda item: item.value):
        rule = COMPLETENESS_DIMENSION_RULES[dimension]
        matching = tuple(item for item in facts if item.fact_key in rule.fact_keys)
        if matching:
            grouped.append((dimension, matching))
    return tuple(grouped)


def _dimension_facts(
    facts_by_dimension: tuple[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]], ...],
    dimension: InquiryDimension,
) -> tuple[CompletenessObservationFact, ...]:
    for item_dimension, facts in facts_by_dimension:
        if item_dimension is dimension:
            return facts
    return ()


def _conflicts_by_dimension(
    current_facts: tuple[CompletenessObservationFact, ...],
) -> tuple[CompletenessConflict, ...]:
    conflict_by_dimension: dict[InquiryDimension, tuple[str, int]] = {}
    for fact_key in sorted(frozenset(item.fact_key for item in current_facts)):
        dimension = _conflict_dimension_for_fact_key(fact_key)
        if dimension is None:
            continue
        value_count = len(frozenset(_value_key(item) for item in current_facts if item.fact_key == fact_key))
        if value_count >= 2:
            conflict_by_dimension[dimension] = (
                COMPLETENESS_SAME_FACT_KEY_CONFLICT_RULE_ID,
                max(value_count, conflict_by_dimension.get(dimension, ("", 0))[1]),
            )
    for rule in COMPLETENESS_CONFLICT_RULES:
        facts = tuple(item for item in current_facts if item.fact_key in rule.fact_keys)
        value_count = len(frozenset(_value_key(item) for item in facts))
        if value_count >= 2:
            previous = conflict_by_dimension.get(rule.dimension, ("", 0))
            conflict_by_dimension[rule.dimension] = (
                rule.rule_id if value_count >= previous[1] else previous[0],
                max(value_count, previous[1]),
            )
    return tuple(
        CompletenessConflict(
            dimension=dimension,
            rule_id=rule_id,
            current_value_count=value_count,
        )
        for dimension, (rule_id, value_count) in sorted(
            conflict_by_dimension.items(),
            key=lambda item: item[0].value,
        )
    )


def _conflict_dimension_for_fact_key(fact_key: str) -> InquiryDimension | None:
    for key, dimension in COMPLETENESS_AUXILIARY_FACT_DIMENSIONS:
        if fact_key == key:
            return dimension
    for rule in COMPLETENESS_DIMENSION_RULES.values():
        if fact_key in rule.fact_keys:
            return rule.dimension
    return None


# 2d(决策 11): 安全三项 + 妊娠/哺乳——cap 到仍转人工(铁律 9),不进 partial 推进。
_SAFETY_DIMENSIONS = frozenset(
    {
        InquiryDimension.ALLERGY_STATUS,
        InquiryDimension.PREGNANCY_STATUS,
        InquiryDimension.LACTATION_STATUS,
        InquiryDimension.MEDICATION_STATUS,
        InquiryDimension.MAJOR_CONDITION_STATUS,
    }
)


def _is_safety_dimension(dimension: InquiryDimension) -> bool:
    return dimension in _SAFETY_DIMENSIONS


def _covered_dimensions(
    current_facts: tuple[CompletenessObservationFact, ...],
    facts_by_dimension: tuple[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]], ...],
    policy_input: CompletenessPolicyInput,
) -> tuple[InquiryDimension, ...]:
    active_fact_keys = frozenset(item.fact_key for item in current_facts)
    if policy_input.slot_based:
        # 2c 槽位口径(灰度): covered 认「粗槽位齐」——两种满足方式:
        # ① canonical 整维键命中(如 ten_questions.cold_heat 是模型对寒热的整维回答,
        #    含多语义,按现状「任一命中即 covered」判定,避免合键漏判);
        # ② 无整维键时,keyset 内细分键(如 present_illness.chills/fever)已采数 ≥ 阈值
        #    (单一真源 MATURITY_KEY_THRESHOLDS,无阈值默认 1)。
        # 修复问题 6「一键即过」:细分键只采 1 项(如只有怕冷)不再 covered,
        # 需怕冷+发热两语义项齐才算;同时不误伤模型合键输出的整维回答。
        covered = set()
        for dimension in COMPLETENESS_DIMENSION_RULES:
            canonical_keys = COMPLETENESS_DIMENSION_RULES[dimension].fact_keys
            if any(key in active_fact_keys for key in canonical_keys) or (
                dimension_acquired_key_count(dimension, active_fact_keys) >= slot_threshold_for(dimension)
            ):
                covered.add(dimension)
    else:
        covered = {dimension for dimension, facts in facts_by_dimension if facts}
        # 维度覆盖键桥（D1 厚真源）：抽取层对同一临床语义会产出多种 fact_key（如寒热落
        # present_illness.chills/fever、现病落 present_illness.cough/symptom.* 等），而 canonical
        # ``COMPLETENESS_DIMENSION_RULES[dimension].fact_keys`` 只认窄键集。键不命中 → 维度永远
        # missing → gap_selector 永远选同一维度 → 命中写死模板 → 问诊死循环（trigger session
        # 63e78741 寒热、d8ba36ae 现病变化）。
        # 此处用 ``DIMENSION_KEYSETS`` 真源对当前 active 事实做覆盖派生：任一 keyset 键命中即该
        # 维度 covered。仅对已 active（非 retracted/corrected-out）事实做派生；安全维度不参与
        # （仍走下方 safety 分支）。覆盖 ≠ 成熟：覆盖派生只解"答到没"，B maturity 在 D2 闸门再判
        # "答得够不够（关键键齐全）"，两层分离防放水。
        for dimension in derived_coverage_for_fact_keys(active_fact_keys):
            covered.add(dimension)
    safety = policy_input.domain_snapshot.safety_profile
    if safety is not None:
        if _collection_complete(safety.allergy_collection_status):
            covered.add(InquiryDimension.ALLERGY_STATUS)
        if _collection_complete(safety.medications_collection_status):
            covered.add(InquiryDimension.MEDICATION_STATUS)
        if _collection_complete(safety.major_conditions_collection_status):
            covered.add(InquiryDimension.MAJOR_CONDITION_STATUS)
        if _collection_complete(safety.pregnancy_collection_status):
            covered.add(InquiryDimension.PREGNANCY_STATUS)
        if _collection_complete(safety.lactation_collection_status):
            covered.add(InquiryDimension.LACTATION_STATUS)
    # R9 contract projection is applied last so an open contract cannot be
    # accidentally closed by a coarse canonical fact from the first partial
    # answer.  All safety contract projections are rejected by the input schema.
    covered.update(policy_input.contract_resolved_dimensions)
    covered.difference_update(policy_input.contract_open_dimensions)
    return tuple(sorted(covered, key=lambda item: item.value))


def _required_dimensions(
    policy_input: CompletenessPolicyInput,
    facts_by_dimension: tuple[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]], ...],
    current_facts: tuple[CompletenessObservationFact, ...],
    applicability: CompletenessApplicabilityResult,
) -> tuple[InquiryDimension, ...]:
    required = {
        rule.dimension for rule in COMPLETENESS_DIMENSION_RULES.values() if rule.required_by_default
    }
    required.update(_dynamic_ten_question_dimensions(current_facts))
    if applicability.pregnancy is not ApplicabilityStatus.NOT_APPLICABLE:
        required.add(InquiryDimension.PREGNANCY_STATUS)
    if applicability.lactation is not ApplicabilityStatus.NOT_APPLICABLE:
        required.add(InquiryDimension.LACTATION_STATUS)
    if policy_input.domain_snapshot.safety_profile is None:
        required.update(
            {
                InquiryDimension.ALLERGY_STATUS,
                InquiryDimension.MEDICATION_STATUS,
                InquiryDimension.MAJOR_CONDITION_STATUS,
            }
        )
    required.update(policy_input.contract_open_dimensions)
    return tuple(sorted(required, key=lambda item: item.value))


def _dynamic_ten_question_dimensions(
    current_facts: tuple[CompletenessObservationFact, ...],
) -> tuple[InquiryDimension, ...]:
    category = _complaint_category(current_facts)
    for rule in COMPLETENESS_COMPLAINT_TEN_QUESTION_RULES:
        if rule.complaint_category == category:
            return rule.dimensions
    return COMPLETENESS_COMPLAINT_TEN_QUESTION_RULES[-1].dimensions


def _complaint_category(
    current_facts: tuple[CompletenessObservationFact, ...],
) -> ComplaintCategory:
    category_facts = tuple(
        item
        for item in current_facts
        if item.fact_key == "chief_complaint.category" and item.normalized_code is not None
    )
    if not category_facts:
        return ComplaintCategory.GENERAL
    categories = tuple(sorted(frozenset(item.normalized_code for item in category_facts if item.normalized_code)))
    if len(categories) != 1:
        return ComplaintCategory.GENERAL
    category = categories[0]
    if category in {rule.complaint_category.value for rule in COMPLETENESS_COMPLAINT_TEN_QUESTION_RULES}:
        try:
            return ComplaintCategory(category)
        except ValueError:
            return ComplaintCategory.GENERAL
    return ComplaintCategory.GENERAL


def _missing_optional_dimensions(covered: tuple[InquiryDimension, ...]) -> tuple[InquiryDimension, ...]:
    covered_set = frozenset(covered)
    return tuple(
        sorted(
            (
                rule.dimension
                for rule in COMPLETENESS_DIMENSION_RULES.values()
                if rule.optional_report and rule.dimension not in covered_set
            ),
            key=lambda item: item.value,
        )
    )


def _stagnation(
    progress: CompletenessProgress,
    *,
    safety_missing: bool = False,
    missing_required: tuple[InquiryDimension, ...] = (),
) -> CompletenessStagnationResult:
    reasons: list[StagnationReasonCode] = []
    if progress.no_new_facts_rounds >= COMPLETENESS_POLICY_CONFIG.no_new_facts_round_threshold:
        reasons.append(StagnationReasonCode.NO_NEW_FACTS_THRESHOLD)
    if progress.followup_rounds >= COMPLETENESS_POLICY_CONFIG.max_followup_rounds:
        reasons.append(StagnationReasonCode.MAX_FOLLOWUP_ROUNDS)
    # 2.8 增强（REAL-SESSION d5df99f0）：停滞只对"仍缺必需维度但追问无进展"
    # 生效。若所有必需维度已 covered（missing_required 为空），后续轮次属于
    # 已覆盖维度的确认/细化（如"总体倾向是怕冷"确认已记录的怕冷），无新事实
    # 不构成空转——不应触发 manual_required 阻断。防呆仍保留：还有缺失维度且
    # 连续无进展时照常转人工。
    if reasons and not missing_required:
        reasons = []
    stagnated = bool(reasons)
    # 2d(决策 11): cap 到后的分流——缺安全维度 → 转人工(铁律 9);
    # 缺非安全维度 → 落 partial 推进(A 为主)。
    manual_handoff_required = stagnated and safety_missing
    partial_required = stagnated and not safety_missing
    return CompletenessStagnationResult(
        stagnated=stagnated,
        manual_handoff_required=manual_handoff_required,
        partial_required=partial_required,
        no_new_facts_rounds=progress.no_new_facts_rounds,
        followup_rounds=progress.followup_rounds,
        reason_codes=tuple(reasons),
    )


def _rule_outcomes(
    required: tuple[InquiryDimension, ...],
    covered: tuple[InquiryDimension, ...],
    conflicts: tuple[CompletenessConflict, ...],
) -> tuple[CompletenessRuleOutcome, ...]:
    required_set = frozenset(required)
    covered_set = frozenset(covered)
    conflict_by_dimension = {item.dimension: item.current_value_count for item in conflicts}
    return tuple(
        CompletenessRuleOutcome(
            rule_id=COMPLETENESS_DIMENSION_RULES[dimension].rule_id,
            dimension=dimension,
            required=dimension in required_set,
            covered=dimension in covered_set,
            conflicting_value_count=conflict_by_dimension.get(dimension, 0),
        )
        for dimension in sorted(COMPLETENESS_DIMENSION_RULES, key=lambda item: item.value)
        if dimension in required_set
        or dimension in covered_set
        or COMPLETENESS_DIMENSION_RULES[dimension].optional_report
        or dimension in conflict_by_dimension
    )


def _disposition(
    policy_input: CompletenessPolicyInput,
    stagnation: CompletenessStagnationResult,
    conflicts: tuple[CompletenessConflict, ...],
    missing_required: tuple[InquiryDimension, ...],
    *,
    contract_partial: bool = False,
) -> CompletenessDisposition:
    if not _triage_allows_ready(policy_input):
        return CompletenessDisposition.TRIAGE_BLOCKED
    if stagnation.stagnated:
        # 2d(决策 11): 安全项 cap 到 → STAGNATED 转人工(B 兜底);
        # 非安全缺口 cap 到 → PARTIAL 落库推进(A 为主)。
        if stagnation.manual_handoff_required:
            return CompletenessDisposition.STAGNATED
        if stagnation.partial_required:
            return CompletenessDisposition.PARTIAL
        return CompletenessDisposition.STAGNATED
    if conflicts:
        return CompletenessDisposition.CONFLICT
    if missing_required:
        return CompletenessDisposition.INCOMPLETE
    if contract_partial:
        return CompletenessDisposition.PARTIAL
    return CompletenessDisposition.READY


def _decision_for_disposition(disposition: CompletenessDisposition) -> GateDecision:
    if disposition is CompletenessDisposition.READY:
        return GateDecision.PASSED
    # 2d(决策 11): PARTIAL 落库推进不阻断(与 READY 同权)。
    if disposition is CompletenessDisposition.PARTIAL:
        return GateDecision.PASSED
    if disposition in {CompletenessDisposition.INCOMPLETE, CompletenessDisposition.CONFLICT}:
        return GateDecision.FAILED
    return GateDecision.BLOCKED


def _triage_allows_ready(policy_input: CompletenessPolicyInput) -> bool:
    details = policy_input.triage_gate.details
    return (
        policy_input.triage_gate.decision is GateDecision.PASSED
        and details.disposition is TriageDisposition.CONTINUE
    )


def _triage_disposition(policy_input: CompletenessPolicyInput) -> str | None:
    return policy_input.triage_gate.details.disposition.value


def _applicability(
    facts_by_dimension: tuple[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]], ...],
) -> CompletenessApplicabilityResult:
    pregnancy_flag = _boolean_code(
        _single_code(facts_by_dimension, InquiryDimension.PREGNANCY_APPLICABILITY_FLAG)
    )
    lactation_flag = _boolean_code(
        _single_code(facts_by_dimension, InquiryDimension.LACTATION_APPLICABILITY_FLAG)
    )
    return CompletenessApplicabilityResult(
        pregnancy=_explicit_or_demographic_applicability(pregnancy_flag, facts_by_dimension),
        lactation=_explicit_or_demographic_applicability(lactation_flag, facts_by_dimension),
    )


def _explicit_or_demographic_applicability(
    explicit_flag: bool | None,
    facts_by_dimension: tuple[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]], ...],
) -> ApplicabilityStatus:
    if explicit_flag is True:
        return ApplicabilityStatus.APPLICABLE
    if explicit_flag is False:
        return ApplicabilityStatus.NOT_APPLICABLE
    sex = _single_code(facts_by_dimension, InquiryDimension.PATIENT_SEX)
    age = _integer_code(_single_code(facts_by_dimension, InquiryDimension.PATIENT_AGE))
    menopause = _boolean_code(_single_code(facts_by_dimension, InquiryDimension.MENOPAUSE_STATUS))
    if sex in {"male", "m", "other_non_applicable"}:
        return ApplicabilityStatus.NOT_APPLICABLE
    if sex not in {"female", "f"}:
        return ApplicabilityStatus.UNKNOWN
    if menopause is True:
        return ApplicabilityStatus.NOT_APPLICABLE
    if age is None:
        return ApplicabilityStatus.UNKNOWN
    if age < 12 or age >= 60:
        return ApplicabilityStatus.NOT_APPLICABLE
    if menopause is False:
        return ApplicabilityStatus.APPLICABLE
    return ApplicabilityStatus.UNKNOWN


def _triage_gate_is_internally_consistent(gate: Any) -> bool:
    details = gate.details
    rules_candidate_count = sum(rule.candidate_count for rule in details.rules)
    category_candidate_count = sum(item.candidate_count for item in details.category_counts)
    source_message_ids = tuple(sorted({source for rule in details.rules for source in rule.source_message_ids}))
    rule_ids = tuple(rule.rule_id for rule in details.rules)
    counts_match = (
        details.candidate_count == rules_candidate_count
        and details.candidate_count == category_candidate_count
        and details.rule_ids == rule_ids
        and details.source_message_ids == source_message_ids
    )
    if details.disposition is TriageDisposition.CONTINUE or gate.decision is GateDecision.PASSED:
        # 0d-3：RISK_NOTE 也是 PASSED（不阻断），但允许有候选（留痕继续问诊）。
        if details.disposition is TriageDisposition.RISK_NOTE:
            return (
                gate.decision is GateDecision.PASSED
                and details.candidate_count >= 1
                and bool(details.category_counts)
                and bool(details.rule_ids)
                and bool(details.rules)
                and bool(details.source_message_ids)
                and counts_match
                and details.risk_level == "noted"
            )
        return (
            gate.decision is GateDecision.PASSED
            and details.disposition is TriageDisposition.CONTINUE
            and details.candidate_count == 0
            and details.category_counts == ()
            and details.rule_ids == ()
            and details.rules == ()
            and details.source_message_ids == ()
            and details.risk_level == "none"
        )
    return (
        gate.decision is GateDecision.BLOCKED
        and details.disposition in {TriageDisposition.EMERGENCY_REFERRAL, TriageDisposition.MANUAL_REVIEW}
        and details.candidate_count >= 1
        and bool(details.category_counts)
        and bool(details.rule_ids)
        and bool(details.rules)
        and bool(details.source_message_ids)
        and counts_match
        and details.risk_level == "emergency"
    )


def _single_code(
    facts_by_dimension: tuple[tuple[InquiryDimension, tuple[CompletenessObservationFact, ...]], ...],
    dimension: InquiryDimension,
) -> str | None:
    facts = _dimension_facts(facts_by_dimension, dimension)
    codes = tuple(item.normalized_code for item in facts if item.normalized_code is not None)
    unique_codes = tuple(sorted(frozenset(codes)))
    if len(unique_codes) != 1:
        return None
    return unique_codes[0].lower()


def _boolean_code(code: str | None) -> bool | None:
    if code in {"true", "yes", "y", "1", "applicable"}:
        return True
    if code in {"false", "no", "n", "0", "not_applicable"}:
        return False
    return None


def _integer_code(code: str | None) -> int | None:
    if code is None or not code.isdigit():
        return None
    return int(code)


def _collection_complete(status: CollectionStatus) -> bool:
    return status in {CollectionStatus.COLLECTED, CollectionStatus.EXPLICITLY_NONE}


def _value_key(fact: CompletenessObservationFact) -> str:
    return fact.normalized_code or fact.value_fingerprint


def _has_undeclared_fields(raw: Any, canonical: Any) -> bool:
    if isinstance(canonical, BaseModel):
        allowed = set(type(canonical).model_fields)
        if isinstance(raw, BaseModel):
            raw_keys = set(raw.__dict__)
            extra = getattr(raw, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                raw_keys.update(extra)
            if raw_keys - allowed:
                return True
            return any(
                _has_undeclared_fields(getattr(raw, name, None), getattr(canonical, name))
                for name in allowed
            )
        if isinstance(raw, dict):
            if set(raw) - allowed:
                return True
            return any(
                _has_undeclared_fields(raw.get(name), getattr(canonical, name))
                for name in allowed
            )
        return True
    if isinstance(canonical, list | tuple):
        if not isinstance(raw, list | tuple) or len(raw) != len(canonical):
            return True
        return any(_has_undeclared_fields(raw_item, item) for raw_item, item in zip(raw, canonical, strict=True))
    if isinstance(canonical, dict):
        return not isinstance(raw, dict)
    return isinstance(raw, BaseModel | dict | list | tuple)
