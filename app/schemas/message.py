"""消息 API 的 Pydantic Schema。

覆盖 P3-2 两个接口：
- POST /api/v1/consult/sessions/{session_id}/messages
- GET  /api/v1/consult/sessions/{session_id}/messages

P8-6 扩展 MessageCreateResponse 以支持 Agent 回复 + 完备性报告。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MessageCreateRequest(BaseModel):
    """提交问诊消息请求体。

    与接口设计文档 §4.2.1 保持一致：
    - content: 必填，1-5000 字符
    - role: doctor 或 patient_proxy
    """

    content: str = Field(..., min_length=1, max_length=5000, description="消息正文")
    role: Literal["doctor", "patient_proxy"] = Field(..., description="消息来源角色")
    reply_to_message_id: UUID | None = Field(
        default=None,
        description="当前回答所对应的结构化 Agent 问题；旧客户端可由服务端安全推断",
    )


class AgentMessageItem(BaseModel):
    """Agent 回复消息（嵌入 MessageCreateResponse）。"""

    message_id: str
    role: str = "agent"
    agent_name: str | None = None
    stage: str
    content: str
    agent_run_id: str | None = None
    created_at: datetime | None = None


class SufficiencyMissingItemData(BaseModel):
    """One clinician-facing required item still missing from the intake."""

    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=160)
    suggested_question: str = Field(min_length=1, max_length=240)


class SufficiencyReportData(BaseModel):
    """完备性报告（嵌入 MessageCreateResponse）。"""

    sufficient: bool
    covered: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    missing_items: list[SufficiencyMissingItemData] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class MessageCreateResponse(BaseModel):
    """提交消息响应 data。

    字段对齐接口设计文档 §4.2.1 成功响应。
    P8-6: 新增 agent_message / sufficiency_report 可选字段。
    """

    message_id: str
    session_id: str
    role: str
    stage: str
    content: str
    current_stage: str
    state_version: int
    created_at: datetime
    # P8-6: Agent 回复与完备性报告
    agent_message: AgentMessageItem | None = None
    sufficiency_report: SufficiencyReportData | None = None


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


class MessageRollbackRequest(BaseModel):
    """回退到指定消息的请求体。

    - reason: 回退原因（医师备注，可选，写入审计）
    """

    reason: str | None = Field(default=None, max_length=500, description="回退原因")


class MessageRollbackData(BaseModel):
    """消息回退响应 data。

    - rolled_back_message_ids: 被删除（含目标消息）的消息 id 列表
    - kept_last_message_id: 回退后保留的最后一条消息 id（可为 None）
    - state_version: 回退后的会话版本号
    """

    session_id: str
    current_stage: str
    state_version: int
    rolled_back_message_ids: list[str]
    kept_last_message_id: str | None = None
