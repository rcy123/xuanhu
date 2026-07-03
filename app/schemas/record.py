"""病历 API 请求/响应 Pydantic Schema。

P7-3 新增：
- RecordResponse: GET /record 响应 data
- RecordUpdateRequest: PUT /record 请求体
- RecordUpdateResponse: PUT /record 响应 data
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RecordResponse(BaseModel):
    """GET /record 响应 data。

    与接口设计文档 §4.5.1 对齐。
    """

    id: str = Field(..., description="病历记录 ID")
    session_id: str = Field(..., description="会话 ID")
    version: int = Field(..., ge=1, description="病历版本号")
    record_text: str = Field(..., description="可读病历文本")
    record_json: dict[str, Any] = Field(..., description="结构化病历 JSON")
    disclaimer: str = Field(..., description="免责声明")
    edited_by_doctor: bool = Field(..., description="是否被医师编辑过")
    doctor_review_id: str | None = Field(default=None, description="对应医师确认记录 ID")
    diff_from_previous: dict[str, Any] | None = Field(default=None, description="与上一版本差异摘要")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="最近更新时间")


class RecordUpdateRequest(BaseModel):
    """PUT /record 请求体。

    医师对已生成的病历进行修改，至少提供 record_text 或 record_json 之一。
    """

    record_text: str | None = Field(default=None, min_length=1, description="修改后的完整病历文本")
    record_json: dict[str, Any] | None = Field(default=None, description="修改后的结构化病历")

    def model_post_init(self, __context: Any) -> None:
        """校验至少提供 record_text 或 record_json 之一。"""
        if self.record_text is None and self.record_json is None:
            raise ValueError("至少提供 record_text 或 record_json 之一")


class RecordUpdateResponse(BaseModel):
    """PUT /record 响应 data。

    与接口设计文档 §4.5.2 对齐。
    """

    id: str = Field(..., description="新病历记录 ID")
    session_id: str = Field(..., description="会话 ID")
    version: int = Field(..., ge=2, description="新版本号")
    diff_from_previous: dict[str, Any] | None = Field(default=None, description="与上一版本的差异摘要")
    edited_by_doctor: bool = Field(default=True, description="是否被医师编辑过")
    doctor_review_id: str | None = Field(default=None, description="对应医师确认记录 ID")
    updated_at: datetime = Field(..., description="更新时间")
