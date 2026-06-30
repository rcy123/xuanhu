"""辨证 Agent —— 基于四诊信息与 RAG 证据输出证型、依据、治法。

职责：
- 在完备性通过后，基于 XuanhuState（含已采集四诊信息）和 RAG 检索到的
  理论/医案证据，输出 SyndromeResult（证型 / 辨证依据 / 鉴别诊断 /
  治法 / citations / confidence）。
- 通过覆写 `_retrieve_evidence` 调用 P2-4 `RAGRetriever`，不绕过既有检索层。
- 不输出最终诊断、处方、剂量、安全审核结论。

通过 BaseAgentImpl 统一调用模型网关 chat_structured 并处理重试/审计。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from app.agents.base import BaseAgentImpl
from app.agents.inquiry import build_state_summary
from app.rag.schemas import Evidence
from app.schemas.agent import SyndromeResult, XuanhuState
from app.schemas.types import Stage

logger = logging.getLogger("xuanhu.syndrome")

# 辨证 Agent 主查库：理论与医案（详见详细设计文档 §7.5）
_SYNDROME_PRIMARY_SOURCES: Sequence[str] = ("theory", "case")
_BLANK_EVIDENCE = "（RAG 未检索到相关理论/医案证据）"
_BLANK_CONVERSATION = "（尚无对话记录）"


class SyndromeRetriever(Protocol):
    """SyndromeAgent 依赖的检索器最小协议，方便测试注入 fake retriever。

    与 P2-4 `RAGRetriever.retrieve` 签名一致。
    """

    async def retrieve(
        self,
        query: str,
        primary_sources: list[str],
        *,
        allow_cross_source: bool = True,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """检索证据，返回 Evidence 列表。"""
        ...


def build_syndrome_query(state: XuanhuState) -> str:
    """从 XuanhuState 构造用于 RAG 检索的查询文本。

    以主诉、现病史和十问歌关键维度为主，拼成自然语言检索串。
    """
    parts: list[str] = []
    if state.chief_complaint:
        parts.append(state.chief_complaint)
    if state.present_illness:
        parts.append(state.present_illness)

    tq = state.ten_questions
    for field, label in (
        ("cold_heat", "寒热"),
        ("sweat", "汗出"),
        ("head_body", "头身"),
        ("stool_urine", "二便"),
        ("diet", "饮食"),
        ("sleep", "睡眠"),
    ):
        val = getattr(tq, field, None)
        if val:
            parts.append(f"{label}：{val}")

    if not parts:
        return state.chief_complaint or ""

    return " ".join(parts)


def format_evidence_summary(evidences: list[Evidence]) -> str:
    """将 Evidence 列表格式化为 prompt 可读文本，保留 evidence_id 便于引用。"""
    if not evidences:
        return _BLANK_EVIDENCE

    lines: list[str] = []
    for ev in evidences:
        lines.append(
            f"- evidence_id={ev.evidence_id} | source_type={ev.source_type} | "
            f"title={ev.title} | content={ev.content_snippet}"
        )
    return "\n".join(lines)


def merge_syndrome_result_to_state(
    state: XuanhuState,
    result: SyndromeResult,
    *,
    evidences: list[Evidence] | None = None,
) -> dict[str, Any]:
    """将 SyndromeResult 合并为 XuanhuState 的 update dict。

    写入 `syndrome_result` 字段，并将本轮 RAG Evidence 合并到
    `state.evidences`（去重保留已有证据，供下游 P6-1 复用）。

    当 `evidences` 参数为 None 时（兼容旧调用），仅写入
    `syndrome_result`，不合并 Evidence。
    """
    updates: dict[str, Any] = {"syndrome_result": result}
    if evidences is not None:
        updates["evidences"] = _merge_evidences_to_state(state, evidences)
    return updates


def _validate_citations(
    citations: list[str],
    evidence_ids: set[str],
) -> None:
    """校验 citations 必须是本轮 Evidence.evidence_id 的子集。

    无证据（evidence_ids 为空）时 citations 必须为空。

    失败时抛出 pydantic ValidationError，使其与 BaseAgentImpl 的
    `_call_with_retries` 重试异常归一化路径一致，最终转为
    `AGENT_SCHEMA_INVALID` 而非 `AGENT_FAILED`。
    """
    from pydantic import ValidationError

    if not evidence_ids:
        if citations:
            raise ValidationError.from_exception_data(
                "SyndromeResult.citations",
                [
                    {
                        "type": "value_error",
                        "loc": ("citations",),
                        "input": citations,
                        "ctx": {
                            "error": ValueError(
                                "无 RAG 证据，citations 必须为空"
                            ),
                        },
                    }
                ],
            )
        return

    invalid = set(citations) - evidence_ids
    if invalid:
        raise ValidationError.from_exception_data(
            "SyndromeResult.citations",
            [
                {
                    "type": "value_error",
                    "loc": ("citations",),
                    "input": citations,
                    "ctx": {
                        "error": ValueError(
                            f"citations 包含非本轮 Evidence 的 evidence_id: {sorted(invalid)}"
                        ),
                    },
                }
            ],
        )


def _merge_evidences_to_state(
    state: XuanhuState,
    new_evidences: list[Evidence],
) -> list[Evidence]:
    """将新证据合并进 state.evidences，按 evidence_id 去重，保持稳定顺序。"""
    existing_ids = {ev.evidence_id for ev in state.evidences}
    merged = list(state.evidences)
    for ev in new_evidences:
        if ev.evidence_id not in existing_ids:
            merged.append(ev)
            existing_ids.add(ev.evidence_id)
    return merged


class SyndromeAgent(BaseAgentImpl):
    """辨证 Agent。

    在完备性通过后基于 XuanhuState 与 RAG Evidence 输出 SyndromeResult。
    通过 `_retrieve_evidence` 调用注入的检索器（默认 `RAGRetriever`），
    不绕过既有检索层；测试可注入 fake retriever / fake evidence。

    模型输出后强制校验 citations 子集：每条 citation 必须属于本轮
    Evidence.evidence_id，无证据时 citations 必须为空。
    """

    name: str = "syndrome"
    stage: Stage = Stage.SYNDROME
    primary_sources: Sequence[str] = _SYNDROME_PRIMARY_SOURCES
    allow_cross_source: bool = True
    output_schema: type[SyndromeResult] = SyndromeResult
    next_stage: Stage | None = Stage.PRESCRIPTION

    def __init__(
        self,
        *,
        gateway: Any = None,
        db: Any = None,
        prompt_loader: Any = None,
        max_retries: int | None = None,
        model_name: str | None = None,
        retriever: SyndromeRetriever | None = None,
        top_k: int = 8,
    ) -> None:
        super().__init__(
            gateway=gateway,
            db=db,
            prompt_loader=prompt_loader,
            max_retries=max_retries,
            model_name=model_name,
        )
        self._retriever: SyndromeRetriever | None = retriever
        self._top_k = top_k
        self._current_evidences: list[Evidence] = []

    def _get_retriever(self) -> SyndromeRetriever:
        """延迟创建默认 RAGRetriever，便于测试注入。"""
        if self._retriever is None:
            from app.rag.retriever import RAGRetriever

            self._retriever = RAGRetriever()
        return self._retriever

    async def _retrieve_evidence(
        self, state: XuanhuState, trace_id: str
    ) -> list[Evidence]:
        """基于问诊摘要检索理论/方证/相关知识。

        RAG 完全不可用（PG 异常）时向上抛出 RAGUnavailableError，
        由 BaseAgentImpl 的错误归一化转为系统错误，最终由 Supervisor
        进入 blocked——不在此静默吞掉，也不编造证据。

        检索结果同时缓存到 `self._current_evidences`，供 `_validate_output`
        在模型输出后校验 citations 子集。
        """
        del trace_id  # 检索器内部自行生成 trace_id

        query = build_syndrome_query(state)
        if not query:
            logger.warning("辨证查询为空，跳过 RAG 检索")
            self._current_evidences = []
            return []

        retriever = self._get_retriever()
        evidences = await retriever.retrieve(
            query,
            list(self.primary_sources),
            allow_cross_source=self.allow_cross_source,
            top_k=self._top_k,
        )
        self._current_evidences = evidences
        return evidences

    async def _build_prompt(
        self,
        state: XuanhuState,
        evidences: list[Evidence],
    ) -> list[dict[str, Any]]:
        """构造 OpenAI chat messages。

        系统消息来自 prompt 模板，含状态摘要、对话历史和 RAG 证据摘要。
        """
        from app.agents.inquiry import _format_conversation_history

        template = self.prompt_template.content
        state_summary = build_state_summary(state)
        conversation_history = _format_conversation_history(state.inquiry_messages)
        evidence_summary = format_evidence_summary(evidences)

        system_content = (
            template.replace("{state_summary}", state_summary)
            .replace("{conversation_history}", conversation_history)
            .replace("{evidence_summary}", evidence_summary)
        )

        return [
            {"role": "system", "content": system_content},
        ]

    def _validate_output(self, raw: BaseModel | dict[str, Any]) -> BaseModel:
        """先校验 schema，再校验 citations 子集约束。"""
        output = super()._validate_output(raw)
        if isinstance(output, SyndromeResult):
            evidence_ids = {ev.evidence_id for ev in self._current_evidences}
            _validate_citations(output.citations, evidence_ids)
        return output


__all__ = [
    "SyndromeAgent",
    "SyndromeRetriever",
    "build_syndrome_query",
    "format_evidence_summary",
    "merge_syndrome_result_to_state",
    "_validate_citations",
]
