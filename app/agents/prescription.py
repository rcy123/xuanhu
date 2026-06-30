"""开方 Agent —— 基于辨证结论与治法检索方剂/中药知识输出基础方。

职责：
- 在辨证完成后，基于 XuanhuState（含 syndrome_result：证型、治法、辨证依据）
  和 RAG 检索到的方剂/中药证据，输出 FormulaResult（基础方名称 / 组成 /
  出处 / 方义 / citations）。
- 通过覆写 `_retrieve_evidence` 调用 P2-4 `RAGRetriever`，不绕过既有检索层。
- 不做加减，不输出 modified_formula、安全审核、医师确认或病历。

通过 BaseAgentImpl 统一调用模型网关 chat_structured 并处理重试/审计。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from app.agents.base import BaseAgentImpl
from app.agents.inquiry import _format_conversation_history, build_state_summary
from app.rag.schemas import Evidence
from app.schemas.agent import FormulaResult, XuanhuState
from app.schemas.types import Stage

logger = logging.getLogger("xuanhu.prescription")

# 开方 Agent 主查库：方剂/中药（详见详细设计文档 §7.6）
_PRESCRIPTION_PRIMARY_SOURCES: Sequence[str] = ("formula", "herb")
_BLANK_EVIDENCE = "（RAG 未检索到相关方剂/中药证据）"


class PrescriptionRetriever(Protocol):
    """PrescriptionAgent 依赖的检索器最小协议，方便测试注入 fake retriever。

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


def build_prescription_query(state: XuanhuState) -> str:
    """从 XuanhuState 构造用于 RAG 检索的查询文本。

    以证型、治法为主，辅以主诉与现病史，拼成自然语言检索串。
    无辨证结论时回退到主诉/现病史。
    """
    parts: list[str] = []
    syndrome_result = state.syndrome_result
    if syndrome_result is not None:
        if syndrome_result.syndrome:
            parts.append(syndrome_result.syndrome)
        if syndrome_result.treatment_principle:
            parts.append(syndrome_result.treatment_principle)
    if state.chief_complaint:
        parts.append(state.chief_complaint)
    if state.present_illness:
        parts.append(state.present_illness)

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


def build_syndrome_summary(state: XuanhuState) -> str:
    """从 state.syndrome_result 构造辨证/治法摘要，供 prompt 使用。"""
    syndrome_result = state.syndrome_result
    if syndrome_result is None:
        return "（尚无辨证结论，开方仅供参考）"
    lines: list[str] = [
        f"- 证型：{syndrome_result.syndrome}",
        f"- 治法：{syndrome_result.treatment_principle}",
    ]
    if syndrome_result.syndrome_basis:
        lines.append("- 辨证依据：" + "；".join(syndrome_result.syndrome_basis))
    if syndrome_result.differential:
        lines.append("- 鉴别诊断：" + "；".join(syndrome_result.differential))
    lines.append(f"- 置信度：{syndrome_result.confidence}")
    return "\n".join(lines)


def merge_formula_result_to_state(
    state: XuanhuState,
    result: FormulaResult,
    *,
    evidences: list[Evidence] | None = None,
) -> dict[str, Any]:
    """将 FormulaResult 合并为 XuanhuState 的 update dict。

    写入 `base_formula` 字段，并将本轮 RAG Evidence 合并到
    `state.evidences`（去重保留已有证据，供下游 P6-2 复用）。

    当 `evidences` 参数为 None 时（兼容旧调用），仅写入
    `base_formula`，不合并 Evidence。
    """
    updates: dict[str, Any] = {"base_formula": result}
    if evidences is not None:
        updates["evidences"] = _merge_evidences_to_state(state, evidences)
    return updates


# 无证据时 rationale 必须包含的缺证提示标记（与 prompt §行为规则 4 一致）。
_EVIDENCE_MISSING_MARKERS: tuple[str, ...] = ("缺证", "未检索")


def _validate_citations(
    citations: list[str],
    evidence_ids: set[str],
) -> None:
    """校验 citations 的可追溯约束。

    - 无证据（evidence_ids 为空）时 citations 必须为空。
    - 有证据时 citations 必须非空，且为本轮 Evidence.evidence_id 的子集。

    失败时抛出 pydantic ValidationError，使其与 BaseAgentImpl 的
    `_call_with_retries` 重试异常归一化路径一致，最终转为
    `AGENT_SCHEMA_INVALID` 而非 `AGENT_FAILED`。
    """
    from pydantic import ValidationError

    def _fail(message: str, input_value: Any) -> None:
        raise ValidationError.from_exception_data(
            "FormulaResult.citations",
            [
                {
                    "type": "value_error",
                    "loc": ("citations",),
                    "input": input_value,
                    "ctx": {"error": ValueError(message)},
                }
            ],
        )

    if not evidence_ids:
        if citations:
            _fail("无 RAG 证据，citations 必须为空", citations)
        return

    if not citations:
        _fail("有 RAG 证据，citations 不得为空，必须至少引用一条本轮 Evidence", citations)

    invalid = set(citations) - evidence_ids
    if invalid:
        _fail(
            f"citations 包含非本轮 Evidence 的 evidence_id: {sorted(invalid)}",
            citations,
        )


def _validate_rationale_traceability(
    rationale: str,
    evidence_ids: set[str],
    citations: list[str],
) -> None:
    """校验方义的可追溯性，补强 prompt 要求的代码防线。

    - 无证据时：方剂结论缺乏可追溯来源，rationale 必须包含缺证提示
      （"缺证"/"未检索" 等标记），明确告知医师结论主要基于模型内知识。
    - 有证据且已引用时：无需额外校验（citations 非空已保证可追溯）。

    失败时抛出 pydantic ValidationError，归一化为 `AGENT_SCHEMA_INVALID`，
    与 `_validate_citations` 路径一致，避免无证据结论静默通过。
    """
    from pydantic import ValidationError

    if evidence_ids and citations:
        return

    if not any(marker in rationale for marker in _EVIDENCE_MISSING_MARKERS):
        raise ValidationError.from_exception_data(
            "FormulaResult.rationale",
            [
                {
                    "type": "value_error",
                    "loc": ("rationale",),
                    "input": rationale,
                    "ctx": {
                        "error": ValueError(
                            "RAG 无证据或未引用证据时，rationale 必须包含"
                            "缺证提示（如「缺证」「未检索」），"
                            "明确结论缺乏可追溯证据"
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


class PrescriptionAgent(BaseAgentImpl):
    """开方 Agent。

    在辨证完成后基于 XuanhuState（syndrome_result）与 RAG Evidence
    输出 FormulaResult。通过 `_retrieve_evidence` 调用注入的检索器
    （默认 `RAGRetriever`），不绕过既有检索层；测试可注入 fake
    retriever / fake evidence。

    模型输出后强制校验 citations 子集：每条 citation 必须属于本轮
    Evidence.evidence_id，无证据时 citations 必须为空。
    """

    name: str = "prescription"
    stage: Stage = Stage.PRESCRIPTION
    primary_sources: Sequence[str] = _PRESCRIPTION_PRIMARY_SOURCES
    allow_cross_source: bool = True
    output_schema: type[FormulaResult] = FormulaResult
    next_stage: Stage | None = Stage.MODIFICATION

    def __init__(
        self,
        *,
        gateway: Any = None,
        db: Any = None,
        prompt_loader: Any = None,
        max_retries: int | None = None,
        model_name: str | None = None,
        retriever: PrescriptionRetriever | None = None,
        top_k: int = 8,
    ) -> None:
        super().__init__(
            gateway=gateway,
            db=db,
            prompt_loader=prompt_loader,
            max_retries=max_retries,
            model_name=model_name,
        )
        self._retriever: PrescriptionRetriever | None = retriever
        self._top_k = top_k
        self._current_evidences: list[Evidence] = []

    def _get_retriever(self) -> PrescriptionRetriever:
        """延迟创建默认 RAGRetriever，便于测试注入。"""
        if self._retriever is None:
            from app.rag.retriever import RAGRetriever

            self._retriever = RAGRetriever()
        return self._retriever

    async def _retrieve_evidence(
        self, state: XuanhuState, trace_id: str
    ) -> list[Evidence]:
        """基于辨证/治法检索方剂/中药证据。

        RAG 完全不可用（PG 异常）时向上抛出 RAGUnavailableError，
        由 BaseAgentImpl 的错误归一化转为系统错误，最终由 Supervisor
        进入 blocked——不在此静默吞掉，也不编造证据。

        检索结果同时缓存到 `self._current_evidences`，供 `_validate_output`
        在模型输出后校验 citations 子集。
        """
        del trace_id  # 检索器内部自行生成 trace_id

        query = build_prescription_query(state)
        if not query:
            logger.warning("开方查询为空，跳过 RAG 检索")
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

        系统消息来自 prompt 模板，含辨证摘要、状态摘要、对话历史和
        RAG 证据摘要。
        """
        template = self.prompt_template.content
        syndrome_summary = build_syndrome_summary(state)
        state_summary = build_state_summary(state)
        conversation_history = _format_conversation_history(state.inquiry_messages)
        evidence_summary = format_evidence_summary(evidences)

        system_content = (
            template.replace("{syndrome_summary}", syndrome_summary)
            .replace("{state_summary}", state_summary)
            .replace("{conversation_history}", conversation_history)
            .replace("{evidence_summary}", evidence_summary)
        )

        return [
            {"role": "system", "content": system_content},
        ]

    def _validate_output(self, raw: BaseModel | dict[str, Any]) -> BaseModel:
        """先校验 schema，再校验 citations 子集约束。"""
        output = super()._validate_output(raw)
        if isinstance(output, FormulaResult):
            evidence_ids = {ev.evidence_id for ev in self._current_evidences}
            _validate_citations(output.citations, evidence_ids)
            _validate_rationale_traceability(
                output.rationale, evidence_ids, output.citations
            )
        return output


__all__ = [
    "PrescriptionAgent",
    "PrescriptionRetriever",
    "build_prescription_query",
    "build_syndrome_summary",
    "format_evidence_summary",
    "merge_formula_result_to_state",
    "_validate_citations",
    "_validate_rationale_traceability",
]
