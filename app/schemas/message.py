"""消息 API 的 Pydantic Schema。

覆盖 P3-2 两个接口：
- POST /api/v1/consult/sessions/{session_id}/messages
- GET  /api/v1/consult/sessions/{session_id}/messages
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageCreateRequest(BaseModel):
    """提交问诊消息请求体。

    与接口设计文档 §4.2.1 保持一致：
    - content: 必填，1-5000 字符
    - role: doctor 或 patient_proxy
    """

    content: str = Field(..., min_length=1, max_length=5000, description="消息正文")
    role: Literal["doctor", "patient_proxy"] = Field(..., description="消息来源角色")


class MessageCreateResponse(BaseModel):
    """提交消息响应 data。

    字段对齐接口设计文档 §4.2.1 成功响应。
    """

    message_id: str
    session_id: str
    role: str
    stage: str
    content: str
    current_stage: str
    state_version: int
    created_at: datetime


class MessageItem(BaseModel):
    """消息历史列表项。

    字段对齐接口设计文档 §4.2.2 成功响应中的 items 项。
    """

    id: str
    session_id: str
    role: str
    agent_name: str | None = None
    stage: str
    content: str
    structured_delta: dict[str, Any] | None = None
    agent_run_id: str | None = None
    created_at: datetime


class MessageListResponse(BaseModel):
    """消息历史游标分页响应 data。

    字段对齐接口设计文档 §4.2.2 成功响应。
    """

    items: list[MessageItem]
    has_more: bool
    next_cursor: str | None = None
