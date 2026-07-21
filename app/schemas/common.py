"""通用 Schema 与响应 envelope。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ApiResponse[T](BaseModel):
    """标准成功响应 envelope。

    与接口设计文档 §1.4 保持一致：code / message / data / trace_id。
    """

    code: str = Field(default="SUCCESS", description="业务状态码")
    message: str = Field(default="ok", description="面向用户的简短描述")
    data: T | None = Field(default=None, description="业务数据")
    trace_id: str = Field(..., description="请求链路 ID")


class ApiError(BaseModel):
    """标准错误响应 envelope。

    与接口设计文档 §1.4 保持一致：code / message / detail / retryable /
    stage / trace_id。
    """

    code: str = Field(..., description="业务错误码")
    message: str = Field(..., description="面向用户的简短中文描述")
    detail: str | None = Field(default=None, description="面向开发者的调试信息")
    retryable: bool = Field(..., description="是否可重试")
    stage: str | None = Field(default=None, description="当前会话阶段（如相关）")
    trace_id: str = Field(..., description="请求链路 ID")


class PaginationResponse[T](BaseModel):
    """标准分页数据容器。

    与接口设计文档 §1.4 分页响应中的 data 对象保持一致。
    """

    items: list[T] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)


def success_response(data: Any, trace_id: str, message: str = "ok") -> dict[str, Any]:
    """构造标准成功响应字典（供 API 层直接返回）。"""
    return {
        "code": "SUCCESS",
        "message": message,
        "data": data,
        "trace_id": trace_id,
    }
