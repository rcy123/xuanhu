"""推理链路 RAG 检索策略与降级封装。

本模块把「辨证/开方阶段如何检索、检索什么、检索失败怎么办」收敛为一个
可单测的纯策略层：

- ``stage_rag_enabled``：编排层按配置决定 stage 是否启用 RAG（policy 选配）。
- ``build_syndrome_query`` / ``build_formula_query``：从领域输入构造检索 query。
- ``retrieve_syndrome_evidence`` / ``retrieve_formula_evidence``：调用
  ``RAGRetriever`` 并实现 D3 降级——检索失败（含 ``RAGUnavailableError``）
  记 warning 并返回空列表，绝不把 503 传导给推理链路；空证据时 agent 走
  「空证据 RAG 模式」（evidence_mode=rag_retrieved、links 必空、confidence ≤0.5）。

设计决策：
- RAG 模式是策略级决策（policy_version 由编排层选配，见 langgraph_reasoning
  的 RunSpec 构造），本模块只提供 stage 级开关判断与检索执行。
- query 构造用主诉/关键症状（syndrome 阶段）或证型+治法+症状（formula 阶段）
  的摘要文本，截断到 ``rag_query_max_chars``。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

from app.core.config import get_settings
from app.rag.schemas import Evidence

logger = logging.getLogger("xuanhu.rag.reasoning")

# ---------------------------------------------------------------------------
# 检索策略参数（不放 config：这是推理语义而非部署参数）
# ---------------------------------------------------------------------------

# 辨证阶段主查库：理论 + 医案（方剂/本草在开方阶段才查）。
SYNDROME_PRIMARY_SOURCES: tuple[str, ...] = ("theory", "case")
# 开方阶段主查库：方剂 + 本草 + 医案。
FORMULA_PRIMARY_SOURCES: tuple[str, ...] = ("formula", "herb", "case")
# 加减方阶段主查库：本草 + 医案（基础方已定，不再搜 formula；聚焦药对配伍 + 加减经验）。
MODIFICATION_PRIMARY_SOURCES: tuple[str, ...] = ("herb", "case")

# 注入 context 的证据条数上限（token 预算：8 条≈500 token，SYNDROME_CONTEXT_TOKEN_LIMIT=4000 内可容）。
EVIDENCE_CONTEXT_MAX_ITEMS: int = 8
# 单条证据 snippet 注入上限（字符）。
EVIDENCE_SNIPPET_MAX_CHARS: int = 200

# 优先纳入 context 的 fact_key 白名单（主诉 + 现病史 + 关键十问 + 舌脉）。
_QUERY_PREFERRED_KEYS: tuple[str, ...] = (
    "chief_complaint.symptom",
    "chief_complaint.course",
    "chief_complaint.category",
    "present_illness.cough",
    "present_illness.sputum",
    "present_illness.rhinorrhea",
    "present_illness.nasal_congestion",
    "present_illness.sore_throat",
    "present_illness.chills",
    "present_illness.fever",
    "present_illness.body_ache",
    "present_illness.chest",
    "present_illness.abdomen",
    "present_illness.pain",
    "present_illness.distension",
    "present_illness.thirst",
    "present_illness.appetite",
    "present_illness.sleep",
    "present_illness.stool",
    "present_illness.urine",
    "ten_questions.cold_heat",
    "ten_questions.sweat",
    "ten_questions.head_body",
    "ten_questions.stool_urine",
    "ten_questions.diet",
    "ten_questions.chest_abdomen",
    "ten_questions.thirst",
    "ten_questions.sleep",
    "ten_questions.menses_leukorrhea",
    "ten_questions.pain",
    "ten_questions.respiratory",
    "four_diagnosis.inspection",
    "four_diagnosis.palpation",
)


def stage_rag_enabled(stage: str) -> bool:
    """编排层判断 stage 是否启用 RAG（总开关 + 阶段开关）。

    Args:
        stage: "syndrome" | "formula" | "base_formula" | "modification"
    """
    settings = get_settings()
    if not settings.rag_enabled:
        return False
    if stage == "syndrome":
        return settings.rag_syndrome_enabled
    if stage in ("formula", "base_formula", "modification"):
        return settings.rag_formula_enabled
    return False


def evidence_context_items(evidence: Sequence[Evidence]) -> list[dict[str, Any]]:
    """把检索证据投影为 context 注入项（截断条数与 snippet 长度控 token 预算）。

    供 syndrome_draft / formula_draft 的 build_*_context 共用；走 ContextBuilder
    的 context 层注入（gateway 传输边界 SECURITY NOTICE 包裹，untrusted）。
    """
    return [
        {
            "evidence_id": item.evidence_id,
            "title": item.title,
            "source_type": item.source_type,
            "score": round(item.score, 4),
            "rank": item.rank,
            "content_snippet": item.content_snippet[:EVIDENCE_SNIPPET_MAX_CHARS],
        }
        for item in evidence[:EVIDENCE_CONTEXT_MAX_ITEMS]
    ]


def _fact_text(value: Any) -> str:
    """把 observation 的 value（可能为 dict/list/str）压平成短文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # 普通聚合值只取语义叶子，不把 slot 元数据（来源 ID、完整度、
        # 缺口提示等）混入检索 Query。
        parts: list[str] = []
        for key, nested in value.items():
            if key in {
                "dimension",
                "slots",
                "completeness",
                "missing_slots",
                "slot_name",
                "source_message_id",
                "confidence",
            }:
                continue
            text = _fact_text(nested)
            if text:
                parts.append(text)
        return "，".join(parts)
    if isinstance(value, list | tuple):
        return "，".join(text for item in value if (text := _fact_text(item)))
    return str(value)


def _slot_query_facts(value: Any) -> list[tuple[str, Any]] | None:
    """Expand a slot snapshot back to its original fact-key/value pairs.

    The slot rollout intentionally groups active facts by completeness
    dimension for downstream prompts.  Retrieval must still use the clinical
    fact keys (for example ``present_illness.chills``), not container values
    such as ``dimension`` or ``completeness``.  ``None`` means this is not a
    slot snapshot; an empty list means it is a snapshot with no usable facts.
    """
    if not isinstance(value, Mapping) or "slots" not in value:
        return None
    slots = value.get("slots")
    if not isinstance(slots, list | tuple):
        return []
    facts: list[tuple[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        key = slot.get("slot_name")
        if isinstance(key, str) and key:
            facts.append((key, slot.get("value")))
    return facts


def _query_facts(observations: Sequence[Any]) -> list[tuple[str, Any]]:
    """Return real clinical facts, expanding slot-container observations."""
    facts: list[tuple[str, Any]] = []
    for item in observations:
        key = getattr(item, "fact_key", "")
        value = getattr(item, "value", None)
        slot_facts = _slot_query_facts(value)
        if slot_facts is not None:
            facts.extend(slot_facts)
        elif isinstance(key, str) and key:
            facts.append((key, value))
    return facts


# ---------------------------------------------------------------------------
# P2: 辨证 query LLM 改写
# ---------------------------------------------------------------------------

# 改写 prompt（轻量、模板化）
_SYNDROME_REWRITE_SYSTEM = """你是中医病历书写助手。将结构化病情信息改写为医案首段风格的自然语言描述。

规则：
1. 以"患者"开头，按主诉→现病史→舌脉的顺序组织
2. 使用中医病历常用表达（如"伴"、"无汗"、"遇X加重"）
3. 不要编造输入中没有的症状
4. 不要添加辨证结论（证型、治法）
5. 输出纯文本，不超过 400 字
6. 舌脉信息保留标准表述
"""

_SYNDROME_REWRITE_USER = """请将以下结构化病情改写为医案首段风格：

{observations_text}

仅输出改写后的病情描述文本，不要加任何前缀或说明。"""


# fact_key → 人读标签映射（仅常用键）
_FACT_KEY_LABELS: dict[str, str] = {
    "chief_complaint.symptom": "主诉症状",
    "chief_complaint.course": "病程",
    "chief_complaint.category": "主诉类别",
    "present_illness.cough": "咳嗽",
    "present_illness.sputum": "痰",
    "present_illness.rhinorrhea": "流涕",
    "present_illness.nasal_congestion": "鼻塞",
    "present_illness.sore_throat": "咽喉",
    "present_illness.chills": "恶寒",
    "present_illness.fever": "发热",
    "present_illness.body_ache": "身痛",
    "present_illness.headache": "头痛",
    "present_illness.chest": "胸",
    "present_illness.abdomen": "腹",
    "present_illness.pain": "疼痛",
    "present_illness.thirst": "口渴",
    "present_illness.appetite": "食欲",
    "present_illness.sleep": "睡眠",
    "present_illness.stool": "大便",
    "present_illness.urine": "小便",
    "ten_questions.cold_heat": "寒热",
    "ten_questions.sweat": "汗出",
    "ten_questions.head_body": "头身",
    "ten_questions.stool_urine": "二便",
    "ten_questions.diet": "饮食",
    "ten_questions.chest_abdomen": "胸腹",
    "ten_questions.thirst": "口渴",
    "ten_questions.sleep": "睡眠",
    "ten_questions.menses_leukorrhea": "经带",
    "ten_questions.pain": "疼痛",
    "ten_questions.respiratory": "呼吸",
    "four_diagnosis.inspection": "舌象",
    "four_diagnosis.palpation": "脉象",
}


def _format_observations_for_rewrite(
    observations: Sequence[Any],
) -> str:
    """把 observations 格式化为 LLM 易于理解的文本。"""
    lines: list[str] = []
    for key, raw_value in _query_facts(observations):
        value = _fact_text(raw_value)
        if not value:
            continue
        label = _FACT_KEY_LABELS.get(key, key)
        lines.append(f"  {label}: {value}")
    return "\n".join(lines)


async def rewrite_syndrome_query(
    observations: Sequence[Any],
    *,
    gateway: Any,
    max_chars: int | None = None,
    trace_id: str = "rag-rewrite",
) -> str:
    """用 LLM 将结构化 observations 改写为医案首段风格。

    改写失败时降级为原始 ``build_syndrome_query``（不阻断辨证流程）。
    由 ``rag_query_rewrite_enabled`` 配置控制开关。

    Args:
        observations: syndrome 阶段 context_observations。
        gateway: ``ModelGatewayClient`` 实例（用于非结构化 chat 调用）。
        max_chars: 改写后 query 最大长度。
        trace_id: 请求链路 ID。
    """
    settings = get_settings()

    # 总开关关闭 → 直接返回结构化 query
    if not settings.rag_query_rewrite_enabled:
        return build_syndrome_query(observations, max_chars=max_chars)

    limit = max_chars if max_chars is not None else settings.rag_query_max_chars

    observations_text = _format_observations_for_rewrite(observations)
    if not observations_text.strip():
        return build_syndrome_query(observations, max_chars=limit)

    # 选择模型：优先用专用改写模型，否则复用 chat_model
    rewrite_model = settings.rag_query_rewrite_model or settings.chat_model

    try:
        content = await gateway.chat(
            messages=[
                {"role": "system", "content": _SYNDROME_REWRITE_SYSTEM},
                {"role": "user", "content": _SYNDROME_REWRITE_USER.format(observations_text=observations_text)},
            ],
            model=rewrite_model,
            temperature=settings.rag_query_rewrite_model_temperature,
            max_tokens=settings.rag_query_rewrite_model_max_tokens,
            trace_id=trace_id,
            agent_name="syndrome_query_rewrite",
        )
        rewritten = content.strip()
        if rewritten and len(rewritten) > limit:
            rewritten = rewritten[:limit]
        return rewritten or build_syndrome_query(observations, max_chars=limit)
    except Exception:
        logger.warning("syndrome query LLM 改写失败，降级为结构化 query", exc_info=True)
        return build_syndrome_query(observations, max_chars=limit)


# ---------------------------------------------------------------------------
# query 构造
# ---------------------------------------------------------------------------


def build_syndrome_query(
    observations: Sequence[Any],
    *,
    max_chars: int | None = None,
) -> str:
    """从 context_observations 构造辨证检索 query。

    优先取白名单 fact_key 的文本值（主诉/现病史/十问/舌脉），再按原始顺序
    补充其余 active 事实，最后整体截断到 ``rag_query_max_chars``。
    """
    limit = max_chars if max_chars is not None else get_settings().rag_query_max_chars
    preferred: list[str] = []
    rest: list[str] = []
    seen: set[str] = set()
    for key, raw_value in _query_facts(observations):
        text = _fact_text(raw_value)
        if not text:
            continue
        if key in seen:
            continue
        seen.add(key)
        (preferred if key in _QUERY_PREFERRED_KEYS else rest).append(f"{key}={text}")
    ordered = preferred + rest
    query = "；".join(ordered)
    if len(query) > limit:
        query = query[:limit]
    return query or ""


def build_formula_query(
    syndrome: Any,
    observations: Sequence[Any],
    *,
    max_chars: int | None = None,
) -> str:
    """从权威 syndrome 输出 + observations 构造开方检索 query。

    以证型与治法为先导，症状作支撑。证型缺失时退化为纯症状摘要。
    """
    limit = max_chars if max_chars is not None else get_settings().rag_query_max_chars
    parts: list[str] = []
    name = getattr(syndrome, "syndrome", None)
    if name:
        parts.append(f"证型={name}")
    principle = getattr(syndrome, "treatment_principle", None)
    if principle:
        parts.append(f"治法={principle}")
    symptom_query = build_syndrome_query(observations, max_chars=limit)
    if symptom_query:
        parts.append(f"症状={symptom_query}")
    query = "；".join(parts)
    if len(query) > limit:
        query = query[:limit]
    return query or ""


async def retrieve_syndrome_evidence(
    retriever: Any,
    observations: Sequence[Any],
    *,
    top_k: int | None = None,
    query: str | None = None,
    logger_extra: dict[str, Any] | None = None,
) -> list[Evidence]:
    """辨证阶段检索。失败降级为空列表（D3），不抛出。

    Args:
        retriever: ``app.rag.retriever.RAGRetriever``（或测试 FakeRetriever）。
        observations: syndrome 阶段 context_observations。
        top_k: 覆盖配置的返回条数。
        query: 可选——预构造的检索 query（如 LLM 改写后的医案文本）。
            为 None 时由 ``build_syndrome_query`` 自动构造。
    """
    settings = get_settings()
    k = top_k or settings.rag_syndrome_top_k
    original_query = build_syndrome_query(observations)
    if query is None:
        query = original_query
    if not query:
        logger.warning("syndrome RAG: 无可检索的观察事实，跳过检索（空证据模式）")
        return []
    return await _retrieve_with_degrade(
        retriever,
        query=query,
        primary_sources=list(SYNDROME_PRIMARY_SOURCES),
        top_k=k,
        stage="syndrome",
        logger_extra=logger_extra,
        original_query=original_query,
    )


async def retrieve_formula_evidence(
    retriever: Any,
    syndrome: Any,
    observations: Sequence[Any],
    *,
    top_k: int | None = None,
    logger_extra: dict[str, Any] | None = None,
) -> list[Evidence]:
    """开方阶段检索。失败降级为空列表（D3），不抛出。"""
    settings = get_settings()
    k = top_k or settings.rag_formula_top_k
    query = build_formula_query(syndrome, observations)
    if not query:
        logger.warning("formula RAG: 无可检索的查询，跳过检索（空证据模式）")
        return []
    return await _retrieve_with_degrade(
        retriever,
        query=query,
        primary_sources=list(FORMULA_PRIMARY_SOURCES),
        top_k=k,
        stage="formula",
        logger_extra=logger_extra,
    )


def build_modification_query(
    syndrome: Any,
    observations: Sequence[Any],
    base_formula: Any,
    *,
    max_chars: int | None = None,
) -> str:
    """构造加减方检索 query。

    与 ``build_formula_query`` 的关键区别：
    - 包含基础方名称和组成，让检索聚焦该方的加减经验
    - 强调待调症状，引导检索药对配伍和单药特性
    - 不再重复证型/治法主导（base 阶段已覆盖）
    """
    limit = max_chars if max_chars is not None else get_settings().rag_query_max_chars

    parts: list[str] = []

    # 1. 基础方信息（核心差异化）
    formula_name = getattr(base_formula, "name", None)
    if formula_name:
        parts.append(f"基础方={formula_name}")

    # 2. 方剂组成（让检索找到涉及相同药味的加减医案）
    herbs = getattr(base_formula, "composition", None) or ()
    if herbs:
        herb_text = "、".join(f"{h.herb}{h.dose}{h.unit}" if hasattr(h, "dose") and h.dose else h.herb for h in herbs)
        if herb_text:
            parts.append(f"组成={herb_text}")

    # 3. 证型与治法（保留但不主导）
    name = getattr(syndrome, "syndrome", None)
    if name:
        parts.append(f"证型={name}")

    # 4. 症状摘要（加权：强调待调症状）
    symptom_query = build_syndrome_query(observations, max_chars=limit // 2)
    if symptom_query:
        parts.append(f"待调症状={symptom_query}")

    query = "；".join(parts)
    if len(query) > limit:
        query = query[:limit]
    return query or ""


async def retrieve_modification_evidence(
    retriever: Any,
    syndrome: Any,
    observations: Sequence[Any],
    base_formula: Any,
    *,
    top_k: int | None = None,
    logger_extra: dict[str, Any] | None = None,
) -> list[Evidence]:
    """加减方阶段检索。使用 herb+case 源，query 含基础方信息。

    与 ``retrieve_formula_evidence`` 的关键区别：
    - sources 仅 herb+case（基础方已定，不再搜 formula）
    - query 含基础方名称和组成，聚焦该方的加减经验
    """
    settings = get_settings()
    k = top_k or settings.rag_formula_top_k
    query = build_modification_query(syndrome, observations, base_formula)
    if not query:
        logger.warning("modification RAG: 无可检索的查询，跳过检索（空证据模式）")
        return []
    return await _retrieve_with_degrade(
        retriever,
        query=query,
        primary_sources=list(MODIFICATION_PRIMARY_SOURCES),
        top_k=k,
        stage="modification",
        logger_extra=logger_extra,
    )


async def _retrieve_with_degrade(
    retriever: Any,
    *,
    query: str,
    primary_sources: list[str],
    top_k: int,
    stage: str,
    logger_extra: dict[str, Any] | None,
    original_query: str | None = None,
) -> list[Evidence]:
    """执行检索并降级。任何失败（含 RAGUnavailableError）→ 空证据，不 503。"""
    extra = {"query_len": len(query), "stage": stage}
    if logger_extra:
        extra.update(logger_extra)
    try:
        settings = get_settings()
        dual_query_retrieve = getattr(retriever, "retrieve_dual_query", None)
        if (
            bool(getattr(settings, "rag_dual_query_enabled", False))
            and original_query
            and original_query != query
            and callable(dual_query_retrieve)
        ):
            results = await dual_query_retrieve(
                original_query,
                query,
                primary_sources,
                allow_cross_source=True,
                top_k=top_k,
            )
            extra["query_mode"] = "dual_rrf"
        else:
            results = await retriever.retrieve(
                query=query,
                primary_sources=primary_sources,
                allow_cross_source=True,
                top_k=top_k,
            )
        logger.info("RAG %s 检索完成: query_len=%d hits=%d", stage, len(query), len(results), extra=extra)
        return cast(list[Evidence], results)
    except Exception as exc:  # noqa: BLE001 - 检索失败必须降级而非阻断推理
        logger.warning(
            "RAG %s 检索失败，降级为空证据模式: %s: %s",
            stage,
            type(exc).__name__,
            str(exc),
            extra=extra,
        )
        return []
