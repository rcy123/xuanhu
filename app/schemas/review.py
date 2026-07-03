"""P7-1 医师确认 API 的 Pydantic Schema。

与接口设计文档 §4.4 保持一致：
- POST /api/v1/consult/sessions/{session_id}/review
- 支持 confirm / modify / reject 三条路径
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ReviewAction = Literal["confirm", "modify", "reject"]


class HerbOverrideItem(BaseModel):
    """医师修改处方中的单味药。

    与 Agent 输出的 ``HerbDose`` 格式保持一致。
    """

    herb: str = Field(..., min_length=1, description="药名（标准名或常用名）")
    dose: float | None = Field(default=None, gt=0.0, description="剂量数值")
    unit: str = Field(default="g", min_length=1, description="剂量单位")
    note: str | None = Field(default=None, description="炮制或先煎后下说明")


class FormulaOverride(BaseModel):
    """医师修改后的完整处方。

    ``composition`` 必填且至少一味药。MVP 不要求医师重填 ``name``/``source``/
    ``rationale`` 等元数据，这些由服务端补默认值。
    """

    name: str | None = Field(default=None, description="方名（可选）")
    composition: list[HerbOverrideItem] = Field(..., min_length=1, description="药味组成")
    source: str | None = Field(default=None, description="出处（可选）")
    rationale: str | None = Field(default=None, description="方义（可选）")


class ReviewRequest(BaseModel):
    """医师确认请求体。

    - ``action=confirm``：仅传 action。
    - ``action=modify``：必填 ``formula_override``，可选 ``feedback``。
    - ``action=reject``：建议填 ``feedback``。
    """

    action: ReviewAction = Field(..., description="医师确认动作")
    formula_override: FormulaOverride | None = Field(
        default=None, description="action=modify 时必填的修改后处方"
    )
    feedback: str | None = Field(default=None, max_length=2000, description="修改或否决原因")


class ReviewResponse(BaseModel):
    """医师确认响应 data。

    字段对齐接口设计文档 §4.4 成功响应。
    P7-1 不生成病历，``medical_record`` 始终为 None，留给 P7-2 填充。
    """

    session_id: str
    action: str
    current_stage: str
    status: str
    pending_review: bool
    review_id: str
    state_version: int
    original_formula: dict[str, Any] | None = None
    formula_override: dict[str, Any] | None = None
    feedback: str | None = None
    safety_recheck: dict[str, Any] | None = None
    medical_record: dict[str, Any] | None = None
    updated_at: datetime
