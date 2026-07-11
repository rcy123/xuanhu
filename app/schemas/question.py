"""Strict L3-4 question composition contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.completeness import InquiryDimension

GAP_SELECTION_INPUT_SCHEMA_VERSION: Literal["gap-selection-input.v1"] = "gap-selection-input.v1"
GAP_SELECTION_RESULT_SCHEMA_VERSION: Literal["gap-selection-result.v1"] = "gap-selection-result.v1"
GAP_SELECTOR_POLICY_VERSION: Literal["gap-selector-policy.v1"] = "gap-selector-policy.v1"
QUESTION_MODEL_OUTPUT_SCHEMA_VERSION: Literal["question-composer-model-output.v1"] = (
    "question-composer-model-output.v1"
)
QUESTION_RESULT_SCHEMA_VERSION: Literal["question-composer-result.v1"] = "question-composer-result.v1"
QUESTION_COMPOSER_AGENT_NAME: Literal["question_composer"] = "question_composer"
QUESTION_COMPOSER_AGENT_VERSION: Literal["question-composer-agent.v1"] = "question-composer-agent.v1"
QUESTION_COMPOSER_PROMPT_VERSION: Literal["question_composer_v1.jinja2"] = "question_composer_v1.jinja2"
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

    @model_validator(mode="after")
    def selection_is_consistent(self) -> GapSelectionResult:
        if self.disposition is GapSelectionDisposition.SELECTED:
            if self.selected_dimension is None:
                raise ValueError("selected gap requires a dimension")
            if self.selection_kind is GapSelectionKind.NONE or self.priority_rule_id is None:
                raise ValueError("selected gap requires kind and priority rule")
        else:
            if (
                self.selected_dimension is not None
                or self.selection_kind is not GapSelectionKind.NONE
                or self.priority_rule_id is not None
            ):
                raise ValueError("no selection must not carry a dimension or rule")
        return self


class QuestionComposerModelInput(_QuestionModel):
    schema_version: Literal["question-composer-model-input.v1"] = "question-composer-model-input.v1"
    selected_dimension: InquiryDimension
    selection_kind: GapSelectionKind
    safety_instruction: str = Field(min_length=1, max_length=400)

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
        if self.source is QuestionSource.MODEL and (
            self.prompt_version is None or self.template_version is not None
        ):
            raise ValueError("model result requires only prompt_version")
        return self


class QuestionCompositionOutcome(_QuestionModel):
    status: QuestionCompositionStatus
    result: QuestionComposerResult | None = None
    failure_code: QuestionComposerFailureCode | None = None

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> QuestionCompositionOutcome:
        if self.status is QuestionCompositionStatus.SUCCEEDED:
            if self.result is None or self.failure_code is not None:
                raise ValueError("successful composition requires a result and no failure code")
        elif self.result is not None:
            raise ValueError("non-successful composition must not carry a result")
        elif self.status is QuestionCompositionStatus.FAILED and self.failure_code is None:
            raise ValueError("failed composition requires a fixed failure code")
        elif self.status is QuestionCompositionStatus.NO_QUESTION and self.failure_code is not None:
            raise ValueError("no-question composition must not carry a failure code")
        return self
