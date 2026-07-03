"""P4-1 Agent / Supervisor 共用核心 Pydantic Schema。

本模块只定义结构化数据契约，不包含 BaseAgent、Supervisor、业务流程、
RAG 调用或模型调用实现。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rag.schemas import Evidence
from app.schemas.session import PatientInfo
from app.schemas.types import (
    Gender,
    MenopauseStatus,
    ModificationAction,
    PregnancyStatus,
    RecoveryStatus,
    ReviewAction,
    RollbackTarget,
    SafetyIssueType,
    Severity,
    Stage,
    is_pregnancy_risk_status,
)


class MenstruationInfo(BaseModel):
    """月经史结构化信息。"""

    model_config = ConfigDict(use_enum_values=True)

    cycle: str | None = None
    volume: str | None = None
    color: str | None = None
    texture: str | None = None
    pain: str | None = None
    last_menstrual_period: date | None = None
    menopause_status: MenopauseStatus = Field(default=MenopauseStatus.UNKNOWN, validate_default=True)


class TenQuestions(BaseModel):
    """十问歌结构化采集结果。"""

    cold_heat: str | None = None
    sweat: str | None = None
    head_body: str | None = None
    stool_urine: str | None = None
    diet: str | None = None
    chest_abdomen: str | None = None
    hearing: str | None = None
    thirst: str | None = None
    sleep: str | None = None
    menstruation: str | None = None
    menstruation_detail: MenstruationInfo | None = None


class FourDiagnosis(BaseModel):
    """望闻问切四诊摘要。"""

    inspection: str | None = None
    auscultation_olfaction: str | None = None
    inquiry: str | None = None
    palpation: str | None = None


class InquiryState(BaseModel):
    """问诊阶段可结构化维护的状态片段。"""

    patient_info: PatientInfo = Field(default_factory=PatientInfo)
    chief_complaint: str | None = None
    present_illness: str | None = None
    past_history: str | None = None
    personal_family_history: str | None = None
    ten_questions: TenQuestions = Field(default_factory=TenQuestions)
    four_diagnosis: FourDiagnosis = Field(default_factory=FourDiagnosis)
    inquiry_messages: list[dict[str, Any]] = Field(default_factory=list)


class SufficiencyReport(BaseModel):
    """问诊信息完备性判断输出。"""

    covered: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    sufficient: bool
    suggestions: list[str] = Field(default_factory=list)
    next_question: str | None = None


class SyndromeResult(BaseModel):
    """辨证立法输出。"""

    syndrome: str = Field(min_length=1)
    syndrome_basis: list[str] = Field(default_factory=list)
    differential: list[str] = Field(default_factory=list)
    treatment_principle: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class HerbDose(BaseModel):
    """单味药剂量。"""

    herb: str = Field(min_length=1)
    dose: float | None = Field(default=None, gt=0.0)
    unit: str = Field(default="g", min_length=1)
    note: str | None = None


class FormulaResult(BaseModel):
    """基础方或成方结果。"""

    name: str = Field(min_length=1)
    composition: list[HerbDose] = Field(min_length=1)
    source: str | None = None
    rationale: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)


class ModificationItem(BaseModel):
    """加减方单项修改。"""

    model_config = ConfigDict(use_enum_values=True)

    action: ModificationAction
    herb: str = Field(min_length=1)
    dose: float | None = Field(default=None, gt=0.0)
    unit: str = Field(default="g", min_length=1)
    reason: str = Field(min_length=1)


class ModifiedFormulaResult(BaseModel):
    """加减后处方结果。"""

    formula: FormulaResult
    modifications: list[ModificationItem] = Field(default_factory=list)


class SafetyIssue(BaseModel):
    """安全审核发现的问题。"""

    model_config = ConfigDict(use_enum_values=True)

    type: SafetyIssueType
    severity: Severity
    herbs: list[str] = Field(default_factory=list)
    rule_source: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)


class SafetyRuleResult(BaseModel):
    """确定性安全规则引擎输出。

    与 `docs/安全审核规则设计文档.md` §12.1 对齐：包含规则版本与执行顺序，
    便于 ``safety_rule_runs`` 审计复盘与版本回滚。
    """

    passed: bool
    issues: list[SafetyIssue] = Field(default_factory=list)
    normalized_formula: FormulaResult
    warnings: list[str] = Field(default_factory=list)
    rule_version: str = Field(default="v1.0.0", min_length=1)
    """本次审核使用的规则版本（写入 ``safety_rule_runs.rule_version``）。"""

    execution_order: list[str] = Field(default_factory=list)
    """规则执行顺序（便于复盘）。如 ["normalize", "convert_dose", ...]。"""


class SafetyReview(BaseModel):
    """面向 Supervisor 路由的安全审核摘要。"""

    model_config = ConfigDict(use_enum_values=True)

    passed: bool
    issues: list[SafetyIssue] = Field(default_factory=list)
    rollback_target: RollbackTarget = Field(default=RollbackTarget.NONE, validate_default=True)
    summary: str = Field(min_length=1)


class InquiryAgentOutput(BaseModel):
    """问诊 Agent 结构化输出。

    包含从本轮对话中抽取的结构化问诊增量以及下一条补问。
    字段均为可选：仅当本轮对话确实提供了对应维度的新信息时才写入非空值。
    """

    chief_complaint: str | None = None
    present_illness: str | None = None
    past_history: str | None = None
    personal_family_history: str | None = None
    ten_questions_delta: TenQuestions | None = None
    four_diagnosis_delta: FourDiagnosis | None = None

    next_question: str = Field(min_length=1)
    asked_dimension: str = Field(min_length=1)

    safety_info_requested: list[str] = Field(default_factory=list)
    safety_notes: str | None = None

    @field_validator("next_question")
    @classmethod
    def validate_next_question(cls, v: str) -> str:
        """校验 next_question 只包含一个核心问题。"""
        question_count = v.count("？") + v.count("?")
        if question_count > 1:
            raise ValueError(
                f"next_question 包含 {question_count} 个问句，"
                "一次只能问一个核心问题"
            )
        parallel_markers = [
            "另外", "此外", "还有", "同时请问", "另外请问",
            "顺便问", "再问一下", "另外问", "还想问",
        ]
        if any(marker in v for marker in parallel_markers):
            raise ValueError(
                "next_question 包含并列追问标记，一次只能问一个核心问题"
            )
        return v


class MedicalRecord(BaseModel):
    """病历生成结果。"""

    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(min_length=1)
    record_json: dict[str, Any] = Field(default_factory=dict, alias="json")
    disclaimer: str = Field(min_length=1)
    doctor_review: dict[str, Any] = Field(default_factory=dict)


class XuanhuState(BaseModel):
    """Agent / Supervisor 共享 State。

    使用 Pydantic BaseModel 便于 schema 校验；后续 Supervisor 可用
    `model_copy(update={...})` 做局部更新。
    """

    model_config = ConfigDict(use_enum_values=True, validate_assignment=True)

    session_id: str
    patient_info: PatientInfo = Field(default_factory=PatientInfo)
    chief_complaint: str | None = None
    present_illness: str | None = None
    past_history: str | None = None
    personal_family_history: str | None = None
    ten_questions: TenQuestions = Field(default_factory=TenQuestions)
    four_diagnosis: FourDiagnosis = Field(default_factory=FourDiagnosis)
    inquiry_messages: list[dict[str, Any]] = Field(default_factory=list)

    evidences: list[Evidence] = Field(default_factory=list)
    sufficiency_report: SufficiencyReport | None = None
    syndrome_result: SyndromeResult | None = None
    base_formula: FormulaResult | None = None
    modified_formula: ModifiedFormulaResult | None = None
    safety_rule_result: SafetyRuleResult | None = None
    safety_review: SafetyReview | None = None
    doctor_review: dict[str, Any] | None = None
    medical_record: MedicalRecord | None = None

    current_stage: Stage = Field(default=Stage.INQUIRY, validate_default=True)
    pending_review: bool = False
    rollback_counts: dict[str, int] = Field(default_factory=dict)
    blocked_reason: str | None = None
    state_version: int = Field(default=1, ge=1)
    recovery_status: RecoveryStatus = Field(default=RecoveryStatus.NORMAL, validate_default=True)
    trace_id: str | None = None

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """返回经过完整 schema 校验的 State 副本。

        Pydantic v2 默认 `model_copy(update=...)` 不校验 update 数据。
        XuanhuState 是状态机边界对象，局部更新必须重新校验 Stage、state_version
        等约束，避免 Supervisor 后续绕过 schema。
        """
        if update is None:
            return super().model_copy(update=None, deep=deep)

        base = self.model_dump(mode="python")
        base.update(update)
        return type(self).model_validate(base)


__all__ = [
    "Evidence",
    "FormulaResult",
    "FourDiagnosis",
    "Gender",
    "HerbDose",
    "InquiryAgentOutput",
    "InquiryState",
    "MedicalRecord",
    "MenopauseStatus",
    "MenstruationInfo",
    "ModificationAction",
    "ModificationItem",
    "ModifiedFormulaResult",
    "PatientInfo",
    "PregnancyStatus",
    "RecoveryStatus",
    "ReviewAction",
    "RollbackTarget",
    "SafetyIssue",
    "SafetyIssueType",
    "SafetyReview",
    "SafetyRuleResult",
    "Severity",
    "Stage",
    "SufficiencyReport",
    "SyndromeResult",
    "TenQuestions",
    "XuanhuState",
    "is_pregnancy_risk_status",
]
