"""P8-6 阶段推进 API 的 Pydantic Schema。

接口设计文档 §4.3.1：
- POST /api/v1/consult/sessions/{session_id}/advance
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdvanceRequest(BaseModel):
    """阶段推进请求体。

    - target_stage: 指定目标阶段，不传则由 Supervisor 自动判断
    - force: 是否强制推进（仅允许在完备性阶段 sufficient=false 时使用）
    - alternative_index: 医师选择的基础方方案索引（0-3），用于多方案选方阶段
    """

    target_stage: str | None = Field(
        default=None,
        description="指定目标阶段，不传则由 Supervisor 自动判断",
    )
    force: bool = Field(
        default=False,
        description="是否强制推进（仅完备性不足时可用）",
    )
    alternative_index: int | None = Field(
        default=None,
        ge=0,
        le=3,
        description="医师选择的基础方方案索引（0-based，对应 alternatives 列表顺序）",
    )
