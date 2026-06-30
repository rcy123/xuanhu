"""加减方 Agent —— 基于基础方、辨证结论与患者信息输出加减后处方。

职责：
- 在开方完成后，基于 XuanhuState（含 base_formula、syndrome_result、四诊信息）
  和 RAG 检索到的方剂/中药证据，输出 ModifiedFormulaResult（加减后完整处方
  / 加减项列表）。
- 通过覆写 `_retrieve_evidence` 调用 P2-4 `RAGRetriever`，不绕过既有检索层。
- 缺少 base_formula 时进入可诊断失败（BASE_FORMULA_MISSING），不静默编造处方。
- 不做安全审核，不输出 safety_review、safety_rule_result、医师确认或病历。

通过 BaseAgentImpl 统一调用模型网关 chat_structured 并处理重试/审计。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from app.agents.base import BaseAgentImpl
from app.agents.errors import AgentRunError
from app.agents.inquiry import _format_conversation_history, build_state_summary
from app.agents.prescription import (
    _merge_evidences_to_state,
    build_syndrome_summary,
    format_evidence_summary,
)
from app.rag.schemas import Evidence
from app.schemas.agent import (
    FormulaResult,
    ModificationItem,
    ModifiedFormulaResult,
    XuanhuState,
)
from app.schemas.types import Stage

logger = logging.getLogger("xuanhu.modification")

# 加减方 Agent 主查库：方剂/中药（详见详细设计文档 §7.7）
_MODIFICATION_PRIMARY_SOURCES: Sequence[str] = ("formula", "herb")

# 加减理由占位符黑名单——schema 已要求 min_length=1，此处进一步拦截无意义理由。
_REASON_PLACEHOLDERS: tuple[str, ...] = (
    "n/a",
    "na",
    "none",
    "无",
    "略",
    "同上",
    "同前",
    "省略",
    "暂无",
)


class ModificationRetriever(Protocol):
    """ModificationAgent 依赖的检索器最小协议，方便测试注入 fake retriever。

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


def build_modification_query(state: XuanhuState) -> str:
    """从 XuanhuState 构造用于 RAG 检索的查询文本。

    以基础方名、证型、治法为主，辅以主诉与现病史，拼成自然语言检索串。
    无 base_formula 或无其他信息时返回空串（调用方负责处理缺 base_formula
    的诊断失败）。
    """
    parts: list[str] = []
    base_formula = state.base_formula
    if base_formula is not None and base_formula.name:
        parts.append(base_formula.name)
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


def format_base_formula_summary(formula: FormulaResult | None) -> str:
    """将基础方格式化为 prompt 可读文本。"""
    if formula is None:
        return "（无基础方信息）"
    lines: list[str] = [f"- 方名：{formula.name}"]
    if formula.source:
        lines.append(f"- 出处：{formula.source}")
    lines.append(f"- 方义：{formula.rationale}")
    if formula.composition:
        comp_lines = []
        for herb in formula.composition:
            dose_text = f"{herb.dose}{herb.unit}" if herb.dose is not None else herb.unit
            note = f"（{herb.note}）" if herb.note else ""
            comp_lines.append(f"  - {herb.herb} {dose_text}{note}")
        lines.append("- 组成：\n" + "\n".join(comp_lines))
    return "\n".join(lines)


# 无证据时 rationale 必须包含的缺证提示标记（与 prompt §行为规则 4 一致）。
_EVIDENCE_MISSING_MARKERS: tuple[str, ...] = ("缺证", "未检索")


def _validate_citations(
    citations: list[str],
    evidence_ids: set[str],
) -> None:
    """校验 citations 的可追溯约束（针对 ModifiedFormulaResult.formula.citations）。

    - 无证据（evidence_ids 为空）时 citations 必须为空。
    - 有证据时 citations 必须非空，且为可用 Evidence.evidence_id 的子集。

    失败时抛出 pydantic ValidationError，使其与 BaseAgentImpl 的
    `_call_with_retries` 重试异常归一化路径一致，最终转为
    `AGENT_SCHEMA_INVALID` 而非 `AGENT_FAILED`。
    """
    from pydantic import ValidationError

    def _fail(message: str, input_value: Any) -> None:
        raise ValidationError.from_exception_data(
            "ModifiedFormulaResult.formula.citations",
            [
                {
                    "type": "value_error",
                    "loc": ("formula", "citations"),
                    "input": input_value,
                    "ctx": {"error": ValueError(message)},
                }
            ],
        )

    if not evidence_ids:
        if citations:
            _fail("无可用 Evidence，citations 必须为空", citations)
        return

    if not citations:
        _fail("有可用 Evidence，citations 不得为空，必须至少引用一条 Evidence", citations)

    invalid = set(citations) - evidence_ids
    if invalid:
        _fail(
            f"citations 包含非可用 Evidence 的 evidence_id: {sorted(invalid)}",
            citations,
        )


def _validate_rationale_traceability(
    rationale: str,
    evidence_ids: set[str],
    citations: list[str],
) -> None:
    """校验加减后方义的可追溯性（针对 formula.rationale）。

    - 无证据或未引用时：方剂结论缺乏可追溯来源，rationale 必须包含缺证提示
      （"缺证"/"未检索" 等标记），明确告知医师结论主要基于模型内知识。
    - 有证据且已引用时：无需额外校验（citations 非空已保证可追溯）。

    失败时抛出 pydantic ValidationError，归一化为 `AGENT_SCHEMA_INVALID`。
    """
    from pydantic import ValidationError

    if evidence_ids and citations:
        return

    if not any(marker in rationale for marker in _EVIDENCE_MISSING_MARKERS):
        raise ValidationError.from_exception_data(
            "ModifiedFormulaResult.formula.rationale",
            [
                {
                    "type": "value_error",
                    "loc": ("formula", "rationale"),
                    "input": rationale,
                    "ctx": {
                        "error": ValueError(
                            "无可用证据或未引用证据时，rationale 必须包含"
                            "缺证提示（如「缺证」「未检索」），"
                            "明确结论缺乏可追溯证据"
                        ),
                    },
                }
            ],
        )


def _validate_modification_reasons(modifications: list[ModificationItem]) -> None:
    """校验每条加减理由不是占位符。

    schema 已要求 reason 非空（min_length=1），此处进一步拦截"N/A""无"
    "略""同上"等无意义占位，确保每条理由都能追溯与患者/证型/治法/证据的关联。
    """
    from pydantic import ValidationError

    for idx, mod in enumerate(modifications):
        normalized = (mod.reason or "").strip().lower()
        if normalized in _REASON_PLACEHOLDERS:
            raise ValidationError.from_exception_data(
                "ModifiedFormulaResult.modifications",
                [
                    {
                        "type": "value_error",
                        "loc": ("modifications", idx, "reason"),
                        "input": mod.reason,
                        "ctx": {
                            "error": ValueError(
                                "modification.reason 不得为占位表述"
                                "（如「无」「略」「同上」），必须说明"
                                "与患者/证型/治法/证据的关联"
                            ),
                        },
                    }
                ],
            )


def merge_modified_formula_result_to_state(
    state: XuanhuState,
    result: ModifiedFormulaResult,
    *,
    evidences: list[Evidence] | None = None,
) -> dict[str, Any]:
    """将 ModifiedFormulaResult 合并为 XuanhuState 的 update dict。

    写入 `modified_formula` 字段，并将本轮 RAG Evidence 合并到
    `state.evidences`（去重保留已有证据）。

    不修改 `base_formula`——加减只体现在 `modified_formula`。

    当 `evidences` 参数为 None 时（兼容旧调用），仅写入
    `modified_formula`，不合并 Evidence。
    """
    updates: dict[str, Any] = {"modified_formula": result}
    if evidences is not None:
        updates["evidences"] = _merge_evidences_to_state(state, evidences)
    return updates


class ModificationAgent(BaseAgentImpl):
    """加减方 Agent。

    在开方完成后基于 XuanhuState（base_formula、syndrome_result）与 RAG
    Evidence 输出 ModifiedFormulaResult。通过 `_retrieve_evidence` 调用
    注入的检索器（默认 `RAGRetriever`），不绕过既有检索层；测试可注入
    fake retriever / fake evidence。

    缺少 base_formula 时抛 `BASE_FORMULA_MISSING`，进入可诊断失败，
    不静默编造处方。

    模型输出后强制校验 citations 子集：每条 citation 必须属于本轮
    Evidence.evidence_id 或既有 state.evidences，无证据时 citations
    必须为空。
    """

    name: str = "modification"
    stage: Stage = Stage.MODIFICATION
    primary_sources: Sequence[str] = _MODIFICATION_PRIMARY_SOURCES
    allow_cross_source: bool = True
    output_schema: type[ModifiedFormulaResult] = ModifiedFormulaResult
    next_stage: Stage | None = Stage.SAFETY

    def __init__(
        self,
        *,
        gateway: Any = None,
        db: Any = None,
        prompt_loader: Any = None,
        max_retries: int | None = None,
        model_name: str | None = None,
        retriever: ModificationRetriever | None = None,
        top_k: int = 8,
    ) -> None:
        super().__init__(
            gateway=gateway,
            db=db,
            prompt_loader=prompt_loader,
            max_retries=max_retries,
            model_name=model_name,
        )
        self._retriever: ModificationRetriever | None = retriever
        self._top_k = top_k
        self._current_evidences: list[Evidence] = []
        self._current_state: XuanhuState | None = None

    def _get_retriever(self) -> ModificationRetriever:
        """延迟创建默认 RAGRetriever，便于测试注入。"""
        if self._retriever is None:
            from app.rag.retriever import RAGRetriever

            self._retriever = RAGRetriever()
        return self._retriever

    async def _retrieve_evidence(
        self, state: XuanhuState, trace_id: str
    ) -> list[Evidence]:
        """基于基础方/辨证/治法检索方剂/中药证据。

        前置检查：缺少 base_formula 时抛 BASE_FORMULA_MISSING，进入可诊断
        失败——不静默编造完整处方。

        RAG 完全不可用（PG 异常）时向上抛出 RAGUnavailableError，
        由 BaseAgentImpl 的错误归一化转为系统错误，最终由 Supervisor
        进入 blocked。

        检索结果同时缓存到 `self._current_evidences`，`state` 缓存到
        `self._current_state`，供 `_validate_output` 在模型输出后校验
        citations 子集（含既有 state.evidences）。
        """
        del trace_id  # 检索器内部自行生成 trace_id

        # 缺 base_formula 诊断失败——不进 RAG，不进模型调用
        if state.base_formula is None:
            raise AgentRunError(
                "缺少 base_formula，无法执行加减方",
                code="BASE_FORMULA_MISSING",
                retryable=False,
                detail="state.base_formula is None",
            )

        self._current_state = state

        query = build_modification_query(state)
        if not query:
            logger.warning("加减方查询为空，跳过 RAG 检索")
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

        系统消息来自 prompt 模板，含辨证摘要、基础方摘要、状态摘要、
        对话历史和 RAG 证据摘要。
        """
        template = self.prompt_template.content
        syndrome_summary = build_syndrome_summary(state)
        base_formula_summary = format_base_formula_summary(state.base_formula)
        state_summary = build_state_summary(state)
        conversation_history = _format_conversation_history(state.inquiry_messages)
        evidence_summary = format_evidence_summary(evidences)

        system_content = (
            template.replace("{syndrome_summary}", syndrome_summary)
            .replace("{base_formula_summary}", base_formula_summary)
            .replace("{state_summary}", state_summary)
            .replace("{conversation_history}", conversation_history)
            .replace("{evidence_summary}", evidence_summary)
        )

        return [
            {"role": "system", "content": system_content},
        ]

    def _validate_output(self, raw: BaseModel | dict[str, Any]) -> BaseModel:
        """先校验 schema，再校验 citations 子集、rationale 可追溯、理由非占位。"""
        output = super()._validate_output(raw)
        if isinstance(output, ModifiedFormulaResult):
            # 可用证据 = 本轮 RAG ∪ 既有 state.evidences
            existing_ids: set[str] = set()
            if self._current_state is not None:
                existing_ids = {ev.evidence_id for ev in self._current_state.evidences}
            all_evidence_ids = {
                ev.evidence_id for ev in self._current_evidences
            } | existing_ids

            _validate_citations(output.formula.citations, all_evidence_ids)
            _validate_rationale_traceability(
                output.formula.rationale, all_evidence_ids, output.formula.citations
            )
            _validate_modification_reasons(output.modifications)
        return output


__all__ = [
    "ModificationAgent",
    "ModificationRetriever",
    "build_modification_query",
    "format_base_formula_summary",
    "merge_modified_formula_result_to_state",
    "_validate_citations",
    "_validate_rationale_traceability",
    "_validate_modification_reasons",
]
