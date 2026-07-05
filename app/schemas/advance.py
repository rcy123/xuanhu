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
    """

    target_stage: str | None = Field(
        default=None,
        description="指定目标阶段，不传则由 Supervisor 自动判断",
    )
    force: bool = Field(
        default=False,
        description="是否强制推进（仅完备性不足时可用）",
    )
