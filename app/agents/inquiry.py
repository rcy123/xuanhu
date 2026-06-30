"""问诊 Agent —— 结构化问诊信息抽取与补问生成。

职责：
- 从医师代录/患者描述（XuanhuState + 近期消息）中抽取问诊信息
- 输出结构化问诊增量（InquiryAgentOutput）
- 生成下一条单一核心补问
- 不判断问诊完备性，不进入辨证

通过 BaseAgentImpl 统一调用模型网关 chat_structured 并处理重试/审计。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgentImpl
from app.rag.schemas import Evidence
from app.schemas.agent import (
    FourDiagnosis,
    InquiryAgentOutput,
    MenstruationInfo,
    PatientInfo,
    TenQuestions,
    XuanhuState,
)
from app.schemas.types import Gender, Stage

logger = logging.getLogger("xuanhu.inquiry")

# ---------------------------------------------------------------------------
# 状态摘要构造
# ---------------------------------------------------------------------------

_BLANK = "（未采集）"


def _format_patient_info(info: PatientInfo) -> str:
    """格式化患者基础信息为 prompt 可读文本。"""
    lines: list[str] = []
    if info.name:
        lines.append(f"- 姓名/标识：{info.name}")
    if info.patient_ref:
        lines.append(f"- 门诊号：{info.patient_ref}")
    if info.gender and info.gender != Gender.UNKNOWN:
        lines.append(f"- 性别：{info.gender}")
    if info.age is not None:
        lines.append(f"- 年龄：{info.age} 岁")
    if info.allergies:
        lines.append(f"- 已知过敏史：{'、'.join(info.allergies)}")
    if info.pregnancy_status and info.pregnancy_status != "unknown":
        lines.append(f"- 妊娠/哺乳状态：{info.pregnancy_status}")
    if info.menstruation_summary:
        lines.append(f"- 月经概要：{info.menstruation_summary}")
    if info.special_conditions:
        lines.append(f"- 特殊情况：{'、'.join(info.special_conditions)}")
    return "\n".join(lines) if lines else "（无患者基础信息）"


def _format_ten_questions(tq: TenQuestions) -> str:
    """格式化十问歌为 prompt 可读文本。"""
    lines: list[str] = []
    mapping: list[tuple[str, str]] = [
        ("cold_heat", "寒热"),
        ("sweat", "汗出"),
        ("head_body", "头身"),
        ("stool_urine", "二便"),
        ("diet", "饮食"),
        ("chest_abdomen", "胸腹"),
        ("hearing", "听力"),
        ("thirst", "口渴"),
        ("sleep", "睡眠"),
        ("menstruation", "月经/带下"),
    ]
    for field, label in mapping:
        val = getattr(tq, field, None)
        if val:
            lines.append(f"- {label}：{val}")
    return "\n".join(lines) if lines else _BLANK


def _format_four_diagnosis(fd: FourDiagnosis) -> str:
    """格式化四诊摘要为 prompt 可读文本。"""
    lines: list[str] = []
    mapping: list[tuple[str, str]] = [
        ("inspection", "望诊"),
        ("auscultation_olfaction", "闻诊"),
        ("inquiry", "问诊"),
        ("palpation", "切诊"),
    ]
    for field, label in mapping:
        val = getattr(fd, field, None)
        if val:
            lines.append(f"- {label}：{val}")
    return "\n".join(lines) if lines else _BLANK


def _format_conversation_history(messages: list[dict[str, Any]], max_messages: int = 20) -> str:
    """格式化近期对话为 prompt 可读文本。"""
    if not messages:
        return "（尚无对话记录）"

    recent = messages[-max_messages:]
    lines: list[str] = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        text = content[:500] if isinstance(content, str) else str(content)[:500]
        label = {"doctor": "医师", "patient": "患者", "assistant": "助手", "system": "系统"}.get(role, role)
        lines.append(f"[{label}] {text}")
    return "\n".join(lines)


def build_state_summary(state: XuanhuState) -> str:
    """从 XuanhuState 构建用于 prompt 的状态摘要文本。"""
    sections: list[str] = []

    # 患者基础信息
    sections.append(f"### 患者基础信息\n{_format_patient_info(state.patient_info)}")

    # 主诉
    sections.append(f"### 主诉\n{state.chief_complaint or _BLANK}")

    # 现病史
    sections.append(f"### 现病史\n{state.present_illness or _BLANK}")

    # 既往史
    sections.append(f"### 既往史\n{state.past_history or _BLANK}")

    # 个人/家族史
    sections.append(f"### 个人/家族史\n{state.personal_family_history or _BLANK}")

    # 十问歌
    sections.append(f"### 十问歌\n{_format_ten_questions(state.ten_questions)}")

    # 四诊摘要
    sections.append(f"### 四诊摘要\n{_format_four_diagnosis(state.four_diagnosis)}")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 安全合并
# ---------------------------------------------------------------------------


def _merge_ten_questions(existing: TenQuestions, delta: TenQuestions) -> TenQuestions:
    """将十问歌 delta 合并到已有数据，仅写入非空新字段。"""
    merged = existing.model_copy()
    scalar_fields = [
        "cold_heat", "sweat", "head_body", "stool_urine", "diet",
        "chest_abdomen", "hearing", "thirst", "sleep", "menstruation",
    ]
    for field in scalar_fields:
        delta_val = getattr(delta, field, None)
        if delta_val is not None and delta_val != "":
            setattr(merged, field, delta_val)

    if delta.menstruation_detail is not None:
        existing_detail = merged.menstruation_detail or MenstruationInfo()
        merged_detail = existing_detail.model_copy()
        detail_fields = ["cycle", "volume", "color", "texture", "pain", "last_menstrual_period"]
        for field in detail_fields:
            delta_val = getattr(delta.menstruation_detail, field, None)
            if delta_val is not None and delta_val != "":
                setattr(merged_detail, field, delta_val)
        if delta.menstruation_detail.menopause_status and delta.menstruation_detail.menopause_status != "unknown":
            merged_detail.menopause_status = delta.menstruation_detail.menopause_status
        merged.menstruation_detail = merged_detail

    return merged


def _merge_four_diagnosis(existing: FourDiagnosis, delta: FourDiagnosis) -> FourDiagnosis:
    """将四诊 delta 合并到已有数据，仅写入非空新字段。"""
    merged = existing.model_copy()
    for field in ("inspection", "auscultation_olfaction", "inquiry", "palpation"):
        delta_val = getattr(delta, field, None)
        if delta_val is not None and delta_val != "":
            setattr(merged, field, delta_val)
    return merged


def merge_inquiry_output_to_state(
    state: XuanhuState,
    output: InquiryAgentOutput,
) -> dict[str, Any]:
    """将 InquiryAgentOutput 安全合并为 XuanhuState 的 update dict。

    关键规则：
    - 增量合并：已有字段再收到新值时追加（以"；"分隔），而非覆盖。
    - 仅在 output 字段非空时写入。
    - ten_questions / four_diagnosis 做字段级合并（仅写入 delta 中有值的子字段）。
    - inquiry_messages 追加而非替换。
    """
    updates: dict[str, Any] = {}

    # 标量字段：增量追加——已有值和新值以"；"拼接，避免覆盖旧问诊信息
    for field in ("chief_complaint", "present_illness", "past_history", "personal_family_history"):
        output_val = getattr(output, field, None)
        if output_val is None or output_val == "":
            continue
        existing_val = getattr(state, field, None)
        if existing_val and existing_val.strip():
            # 已有有效值 → 增量追加，避免简单覆盖
            updates[field] = f"{existing_val}；{output_val}"
        else:
            updates[field] = output_val

    # ten_questions：字段级合并
    if output.ten_questions_delta is not None:
        updates["ten_questions"] = _merge_ten_questions(state.ten_questions, output.ten_questions_delta)

    # four_diagnosis：字段级合并
    if output.four_diagnosis_delta is not None:
        updates["four_diagnosis"] = _merge_four_diagnosis(state.four_diagnosis, output.four_diagnosis_delta)

    # inquiry_messages：追加
    new_message: dict[str, Any] = {
        "role": "assistant",
        "content": output.next_question,
        "asked_dimension": output.asked_dimension,
    }
    if output.safety_info_requested:
        new_message["safety_info_requested"] = output.safety_info_requested
    if output.safety_notes:
        new_message["safety_notes"] = output.safety_notes
    updates["inquiry_messages"] = list(state.inquiry_messages) + [new_message]

    return updates


# ---------------------------------------------------------------------------
# InquiryAgent
# ---------------------------------------------------------------------------


class InquiryAgent(BaseAgentImpl):
    """问诊 Agent。

    从当前 XuanhuState 和近期问诊消息中抽取结构化问诊信息，
    生成下一条单一核心补问。

    使用 BaseAgentImpl 的统一流程：prompt 加载 → 模型调用 → 校验 → 审计。
    """

    name: str = "inquiry"
    stage: Stage = Stage.INQUIRY
    output_schema: type[InquiryAgentOutput] = InquiryAgentOutput
    next_stage: Stage | None = Stage.SUFFICIENCY

    async def _build_prompt(
        self,
        state: XuanhuState,
        evidences: list[Evidence],
    ) -> list[dict[str, Any]]:
        """构造 OpenAI chat messages。

        系统消息来自 prompt 模板，用户消息为状态摘要和对话历史。
        """
        del evidences  # 当前阶段不调用 RAG

        template = self.prompt_template.content
        state_summary = build_state_summary(state)
        conversation_history = _format_conversation_history(state.inquiry_messages)

        system_content = template.replace("{state_summary}", state_summary).replace(
            "{conversation_history}", conversation_history
        )

        return [
            {"role": "system", "content": system_content},
        ]

    async def _retrieve_evidence(self, state: XuanhuState, trace_id: str) -> list[Evidence]:
        """P5-1 不调用 RAG。P5-2 及之后可覆写以检索知识库。"""
        del state, trace_id
        return []
