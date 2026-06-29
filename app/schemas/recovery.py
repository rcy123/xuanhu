"""P3-4 会话恢复 API 的 Pydantic Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

RecoveryAction = Literal[
    "resume_from_pg_snapshot",
    "retry_current_stage",
    "rollback_to_stage",
    "terminate",
]

# rollback_to_stage 合法的目标阶段（排除 terminal 状态的 done 和 blocked）
VALID_ROLLBACK_TARGETS: frozenset[str] = frozenset({
    "inquiry",
    "sufficiency",
    "syndrome",
    "prescription",
    "modification",
    "safety",
    "review",
    "record",
})


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


class RecoveryRequest(BaseModel):
    """会话恢复请求体。

    与接口设计文档 §4.3.2 保持一致：
    - action: 恢复动作
    - target_stage: action=rollback_to_stage 时必填
    - reason: 人工恢复或终止原因（可选）
    """

    action: RecoveryAction = Field(
        ...,
        description="恢复动作",
    )
    target_stage: str | None = Field(
        default=None,
        description="action=rollback_to_stage 时的目标阶段",
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="人工恢复或终止原因",
    )


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------


class RecoveryResponse(BaseModel):
    """会话恢复响应 data。"""

    session_id: str
    current_stage: str
    status: str
    recovery_status: str
    action: str
    updated_at: datetime
