"""会话管理 API 的 Pydantic Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 枚举 / 子结构
# ---------------------------------------------------------------------------


class PatientInfo(BaseModel):
    """患者基础信息。

    与详细设计文档 §5.2 及接口设计文档 §4.1.1 保持一致。
    """

    name: str | None = Field(default=None, description="患者姓名或脱敏标识")
    patient_ref: str | None = Field(default=None, description="患者门诊号/临时编号")
    gender: Literal["male", "female", "unknown"] = Field(default="unknown")
    age: int | None = Field(default=None, ge=0, le=130)
    allergies: list[str] = Field(default_factory=list)
    pregnancy_status: Literal["unknown", "no", "pregnant", "possible", "lactating"] = Field(
        default="unknown"
    )
    menstruation_summary: str | None = Field(default=None)


class SessionListItem(BaseModel):
    """会话列表项。"""

    session_id: str
    patient_info: PatientInfo
    chief_complaint: str | None
    current_stage: str
    status: str
    pending_review: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class SessionCreateRequest(BaseModel):
    """创建会话请求体。"""

    patient_info: PatientInfo = Field(default_factory=PatientInfo)
    chief_complaint: str | None = Field(default=None, max_length=2000)


class SessionCreateResponse(BaseModel):
    """创建会话响应 data。"""

    session_id: str
    current_stage: str
    status: str
    patient_info: PatientInfo
    created_at: datetime


class SessionDetailResponse(BaseModel):
    """会话详情响应 data。

    P3-1 阶段仅填充基础字段与阶段占位；Agent/RAG/病历相关字段保留字段名，
    值为 null，为 P3-2/P4 阶段预留。
    """

    session_id: str
    status: str
    current_stage: str
    pending_review: bool
    todo: dict[str, Any] | None = None
    recovery_status: str
    blocked_reason: str | None
    rollback_counts: dict[str, Any]
    state_version: int
    patient_info: PatientInfo
    chief_complaint: str | None
    present_illness: str | None = None
    past_history: str | None = None
    ten_questions: dict[str, Any] | None = None
    sufficiency_report: dict[str, Any] | None = None
    syndrome_result: dict[str, Any] | None = None
    base_formula: dict[str, Any] | None = None
    modified_formula: dict[str, Any] | None = None
    modifications: list[dict[str, Any]] | None = None
    safety_review: dict[str, Any] | None = None
    medical_record: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    """会话列表分页响应 data。"""

    items: list[SessionListItem]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)


class SessionTerminateRequest(BaseModel):
    """终止会话请求体。"""

    reason: str | None = Field(default=None, max_length=500)


class SessionTerminateResponse(BaseModel):
    """终止会话响应 data。"""

    session_id: str
    status: str
    current_stage: str
    blocked_reason: str
    updated_at: datetime
