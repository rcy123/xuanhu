"""Strict L3-4 question composition contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.completeness import InquiryDimension

GAP_SELECTION_INPUT_SCHEMA_VERSION: Literal["gap-selection-input.v1"] = "gap-selection-input.v1"
GAP_SELECTION_RESULT_SCHEMA_VERSION: Literal["gap-selection-result.v1"] = "gap-selection-result.v1"
GAP_SELECTOR_POLICY_VERSION: Literal["gap-selector-policy.v1"] = "gap-selector-policy.v1"
QUESTION_MODEL_OUTPUT_SCHEMA_VERSION: Literal["question-composer-model-output.v1"] = "question-composer-model-output.v1"
QUESTION_MODEL_INPUT_SCHEMA_VERSION: Literal["question-composer-model-input.v2"] = "question-composer-model-input.v2"
QUESTION_RESULT_SCHEMA_VERSION: Literal["question-composer-result.v1"] = "question-composer-result.v1"
QUESTION_COMPOSER_AGENT_NAME: Literal["question_composer"] = "question_composer"
QUESTION_COMPOSER_AGENT_VERSION: Literal["question-composer-agent.v2"] = "question-composer-agent.v2"
QUESTION_COMPOSER_PROMPT_VERSION: Literal["question_composer_v2.jinja2"] = "question_composer_v2.jinja2"
QUESTION_TEMPLATE_REGISTRY_VERSION: Literal["question-template-registry.v1"] = "question-template-registry.v1"


class _QuestionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GapSelectionDisposition(StrEnum):
    SELECTED = "selected"
    NO_SELECTION = "no_selection"


class GapSelectionKind(StrEnum):
    REQUIRED = "required"
    CONFLICT = "conflict"
    NONE = "none"


class QuestionSource(StrEnum):
    TEMPLATE = "template"
    MODEL = "model"


class QuestionCompositionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NO_QUESTION = "no_question"
    FAILED = "failed"


class QuestionComposerFailureCode(StrEnum):
    INPUT_SCHEMA_INVALID = "QUESTION_INPUT_SCHEMA_INVALID"
    PROMPT_CONTRACT_MISMATCH = "QUESTION_PROMPT_CONTRACT_MISMATCH"
    CONTEXT_BUILD_FAILED = "QUESTION_CONTEXT_BUILD_FAILED"
    SELECTION_REQUIRED = "QUESTION_SELECTION_REQUIRED"
    SELECTION_INPUT_INVALID = "QUESTION_SELECTION_INPUT_INVALID"
    SELECTION_AUTHORITY_FIELD_FORBIDDEN = "QUESTION_SELECTION_AUTHORITY_FIELD_FORBIDDEN"
    SELECTION_AUTHORITY_MISMATCH = "QUESTION_SELECTION_AUTHORITY_MISMATCH"
    TEMPLATE_CONTRACT_MISMATCH = "QUESTION_TEMPLATE_CONTRACT_MISMATCH"
    RUNTIME_CONTRACT_MISMATCH = "QUESTION_RUNTIME_CONTRACT_MISMATCH"
    MODEL_UNAVAILABLE = "QUESTION_MODEL_UNAVAILABLE"
    MODEL_OUTPUT_INVALID = "QUESTION_MODEL_OUTPUT_INVALID"
    SINGLE_QUESTION_INVALID = "QUESTION_SINGLE_QUESTION_INVALID"


class GapSelectionInput(_QuestionModel):
    schema_version: Literal["gap-selection-input.v1"] = GAP_SELECTION_INPUT_SCHEMA_VERSION
    input_state_version: int = Field(ge=1)


class GapPriorityRule(_QuestionModel):
    rule_id: str = Field(min_length=1, max_length=96)
    dimension: InquiryDimension
    required_priority: int | None = Field(default=None, ge=1, le=10_000)
    conflict_priority: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def at_least_one_priority(self) -> GapPriorityRule:
        if self.required_priority is None and self.conflict_priority is None:
            raise ValueError("a gap priority rule must register at least one path")
        return self


class GapSelectionResult(_QuestionModel):
    schema_version: Literal["gap-selection-result.v1"] = GAP_SELECTION_RESULT_SCHEMA_VERSION
    policy_version: Literal["gap-selector-policy.v1"] = GAP_SELECTOR_POLICY_VERSION
    input_state_version: int = Field(ge=1)
    disposition: GapSelectionDisposition
    selected_dimension: InquiryDimension | None = None
    selection_kind: GapSelectionKind
    priority_rule_id: str | None = Field(default=None, max_length=96)
    source_completeness_disposition: str = Field(min_length=1, max_length=64)
    deferred_dimensions: tuple[InquiryDimension, ...] = ()

    @model_validator(mode="after")
    def selection_is_consistent(self) -> GapSelectionResult:
        safety_dimensions = {
            InquiryDimension.ALLERGY_STATUS,
            InquiryDimension.MEDICATION_STATUS,
            InquiryDimension.MAJOR_CONDITION_STATUS,
            InquiryDimension.PREGNANCY_STATUS,
            InquiryDimension.LACTATION_STATUS,
        }
        if len(self.deferred_dimensions) != len(set(self.deferred_dimensions)) or any(
            item not in safety_dimensions for item in self.deferred_dimensions
        ):
            raise ValueError("only unique pending safety dimensions may be deferred")
        if self.disposition is GapSelectionDisposition.SELECTED:
            if self.selected_dimension is None:
                raise ValueError("selected gap requires a dimension")
            if self.selection_kind is GapSelectionKind.NONE or self.priority_rule_id is None:
                raise ValueError("selected gap requires kind and priority rule")
            if self.selected_dimension in self.deferred_dimensions:
                raise ValueError("selected gap cannot also be deferred")
        else:
            if (
                self.selected_dimension is not None
                or self.selection_kind is not GapSelectionKind.NONE
                or self.priority_rule_id is not None
            ):
                raise ValueError("no selection must not carry a dimension or rule")
        return self


class QuestionComposerClinicalFact(_QuestionModel):
    """Bounded clinical context; identity and safety-profile fields are excluded."""

    fact_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def clinical_fact_only(self) -> QuestionComposerClinicalFact:
        allowed = (
            "chief_complaint.",
            "present_illness.",
            "ten_questions.",
            "past_history",
            "four_diagnosis",
        )
        if not self.fact_key.startswith(allowed):
            raise ValueError("question context only accepts bounded clinical facts")
        return self


class QuestionComposerTurn(_QuestionModel):
    """1b: 一轮医患对话(医生问句或患者回答的原文,做身份遮罩后传入)。

    用于让 writer 承接前文——识别已问过的问题避免原话重复、承接患者
    抗议/澄清,而不是逐字重复模板句。
    """

    role: Literal["doctor", "patient"]
    content: str = Field(min_length=1, max_length=1_000)

    @field_validator("content")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("turn content must not be blank")
        return value


class QuestionComposerModelInput(_QuestionModel):
    schema_version: Literal["question-composer-model-input.v2"] = QUESTION_MODEL_INPUT_SCHEMA_VERSION
    selected_dimension: InquiryDimension
    selection_kind: GapSelectionKind
    safety_instruction: str = Field(min_length=1, max_length=800)
    clinical_context: tuple[QuestionComposerClinicalFact, ...] = Field(default=(), max_length=24)
    # 1b: 对话历史(最近 N 轮)、主诉原文、激活维度集、缺口提示。
    # 槽位缺口暂从 completeness 的 missing_required 派生(阶段 2 换槽位对象)。
    recent_turns: tuple[QuestionComposerTurn, ...] = Field(default=(), max_length=8)
    chief_complaint: str | None = Field(default=None, min_length=1, max_length=2_000)
    activated_dimensions: tuple[str, ...] = Field(default=(), max_length=16)
    missing_slot: str | None = Field(default=None, min_length=1, max_length=240)

    @model_validator(mode="after")
    def model_input_requires_selected_kind(self) -> QuestionComposerModelInput:
        if self.selection_kind is GapSelectionKind.NONE:
            raise ValueError("model input requires a selected gap kind")
        return self


class QuestionComposerModelOutput(_QuestionModel):
    schema_version: Literal["question-composer-model-output.v1"] = QUESTION_MODEL_OUTPUT_SCHEMA_VERSION
    question: str = Field(min_length=1, max_length=160)


class QuestionComposerResult(_QuestionModel):
    schema_version: Literal["question-composer-result.v1"] = QUESTION_RESULT_SCHEMA_VERSION
    input_state_version: int = Field(ge=1)
    selected_dimension: InquiryDimension
    selection_kind: GapSelectionKind
    question: str = Field(min_length=1, max_length=160)
    source: QuestionSource
    template_version: str | None = Field(default=None, max_length=96)
    prompt_version: str | None = Field(default=None, max_length=96)

    @model_validator(mode="after")
    def source_version_is_consistent(self) -> QuestionComposerResult:
        if self.source is QuestionSource.TEMPLATE and (
            self.template_version is None or self.prompt_version is not None
        ):
            raise ValueError("template result requires only template_version")
        if self.source is QuestionSource.MODEL and (self.prompt_version is None or self.template_version is not None):
            raise ValueError("model result requires only prompt_version")
        return self


class QuestionCompositionOutcome(_QuestionModel):
    status: QuestionCompositionStatus
    result: QuestionComposerResult | None = None
    failure_code: QuestionComposerFailureCode | None = None
    # 0a 模板兜底留痕：模板成功但曾发生模型软失败（网关/输出/单问句校验等）时携带信号。
    # 退化路径下 ``degraded=True`` 且 ``last_failure_code`` 记录模型那次失败码，便于事后定位
    # "为什么这一轮回落到模板"。非退化（纯模板命中 / 模型直接成功）保持 ``degraded=False``、
    # ``failure_code=None``。
    degraded: bool = False
    last_failure_code: QuestionComposerFailureCode | None = None

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> QuestionCompositionOutcome:
        if self.status is QuestionCompositionStatus.SUCCEEDED:
            if self.result is None:
                raise ValueError("successful composition requires a result")
            if self.failure_code is not None:
                raise ValueError("successful composition must not carry a failure code")
            if self.degraded and self.last_failure_code is None:
                raise ValueError("degraded successful composition requires a last failure code")
            if not self.degraded and self.last_failure_code is not None:
                raise ValueError("non-degraded successful composition must not carry a last failure code")
        elif self.result is not None:
            raise ValueError("non-successful composition must not carry a result")
        elif self.degraded or self.last_failure_code is not None:
            raise ValueError("only successful composition may carry degraded signals")
        elif self.status is QuestionCompositionStatus.FAILED and self.failure_code is None:
            raise ValueError("failed composition requires a fixed failure code")
        elif self.status is QuestionCompositionStatus.NO_QUESTION and self.failure_code is not None:
            raise ValueError("no-question composition must not carry a failure code")
        return self
