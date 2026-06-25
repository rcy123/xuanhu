"""RAG 检索数据结构 — Evidence、检索中间结果与异常定义。

Evidence 字段与 agent_evidences 表对齐，供后续 Agent 层持久化。
所有分数归一化到 [0, 1] 区间，便于重排加权计算。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 合法 source_type 常量
# ---------------------------------------------------------------------------

VALID_SOURCE_TYPES = {"formula", "herb", "acupoint", "theory", "case"}


# ---------------------------------------------------------------------------
# Evidence — 最终返回给 Agent 的证据结构
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """RAG 检索返回的证据条目。

    字段与 agent_evidences 表对齐，便于 Agent 层后续持久化。
    """

    evidence_id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="运行内 evidence 唯一标识")
    source_type: str = Field(description="来源类型: formula / herb / acupoint / theory / case")
    source_id: str = Field(description="原始知识条目 ID")
    chunk_id: str | None = Field(default=None, description="knowledge_chunks.id")
    title: str = Field(description="证据标题")
    content_snippet: str = Field(description="引用片段，截断保存")
    score: float = Field(ge=0.0, le=1.0, description="最终重排得分")
    rank: int = Field(ge=1, description="本次检索排序，从 1 开始")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="调试用原始得分: vector_score / fulltext_score / source_priority",
    )


# ---------------------------------------------------------------------------
# 检索中间结果 — 向量/全文命中记录
# ---------------------------------------------------------------------------


class VectorHit(BaseModel):
    """Milvus 向量检索命中结果。"""

    chunk_id: str = Field(description="knowledge_chunks.id")
    source_type: str = Field(description="来源类型")
    source_id: str = Field(description="原始知识条目 ID")
    title: str = Field(description="标题")
    content_hash: str = Field(default="", description="内容 hash")
    vector_score: float = Field(ge=0.0, le=1.0, description="向量相似度得分")


class FulltextHit(BaseModel):
    """PostgreSQL 全文检索命中结果。"""

    chunk_id: str = Field(description="knowledge_chunks.id")
    source_type: str = Field(description="来源类型")
    source_id: str = Field(description="原始知识条目 ID")
    title: str = Field(description="标题")
    content: str = Field(description="chunk 完整内容")
    fulltext_score: float = Field(ge=0.0, le=1.0, description="全文检索得分")


class MergedHit(BaseModel):
    """合并去重后的中间结果，用于重排输入。"""

    chunk_id: str = Field(description="knowledge_chunks.id")
    source_type: str = Field(description="来源类型")
    source_id: str = Field(description="原始知识条目 ID")
    title: str = Field(description="标题")
    content_snippet: str = Field(description="引用片段")
    vector_score: float = Field(default=0.0, ge=0.0, le=1.0, description="向量得分，未命中为 0")
    fulltext_score: float = Field(default=0.0, ge=0.0, le=1.0, description="全文得分，未命中为 0")
    is_primary: bool = Field(default=True, description="是否命中 primary_sources")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class RAGError(Exception):
    """RAG 检索错误基类。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class RAGUnavailableError(RAGError):
    """RAG 完全不可用 — PG 不可用时抛出，阻塞需要证据的 Agent。

    不泄露 API key、prompt 原文或完整外部异常响应。
    """

    def __init__(
        self,
        message: str = "RAG 检索不可用：数据库连接失败",
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message, retryable=retryable)
