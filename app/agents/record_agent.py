"""病历生成 Agent —— 基于全量 State 与医师确认生成 AI 初版病历。

职责：
- 在 record 阶段，读取 XuanhuState（含问诊信息、辨证结论、处方、安全审核、
  医师确认），生成 MedicalRecord（文本病历 + 结构化 JSON 病历）。
- 通过覆写 `_build_prompt` 将 state 信息注入 prompt 模板。
- 不调用 RAG 检索（病历生成基于已有信息，不检索新证据）。
- 不做病历编辑、不导出病历、不绕过医师确认。
- 由 Supervisor 在 _advance_locked 中调用 RecordAgent 后负责
  medical_records 落库、session 状态更新和事件发送。

通过 BaseAgentImpl 统一调用模型网关 chat_structured 并处理重试/审计。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgentImpl
from app.agents.inquiry import (
    build_state_summary,
)
from app.rag.schemas import Evidence
from app.schemas.agent import (
    FormulaResult,
    MedicalRecord,
    ModifiedFormulaResult,
    XuanhuState,
)
from app.schemas.types import Stage

logger = logging.getLogger("xuanhu.record")


# ---------------------------------------------------------------------------
# 摘要构造工具
# ---------------------------------------------------------------------------


def build_syndrome_summary_for_record(state: XuanhuState) -> str:
    """从 state.syndrome_result 构造辨证/治法摘要，供 prompt 使用。"""
    syndrome_result = state.syndrome_result
    if syndrome_result is None:
        return "（尚无辨证结论，病历仅供参考）"
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


def build_formula_summary_for_record(state: XuanhuState) -> str:
    """构造最终处方摘要，以医师确认后的版本为准。

    优先级：
    - modify: doctor_review.formula_override → state.modified_formula
    - confirm: state.modified_formula → state.base_formula
    - 无处方信息时标注"待医师补充"。
    """
    doctor_review = state.doctor_review or {}

    # modify 路径：优先使用 formula_override
    if doctor_review.get("action") == "modify":
        override = doctor_review.get("formula_override")
        if override and isinstance(override, dict):
            return _format_formula_override(override)
        # 回退到 state.modified_formula
        if state.modified_formula is not None:
            return _format_modified_formula(state.modified_formula)

    # confirm 路径：使用 modified_formula
    if doctor_review.get("action") == "confirm":
        if state.modified_formula is not None:
            return _format_modified_formula(state.modified_formula)
        if state.base_formula is not None:
            return _format_formula_result(state.base_formula)

    # 无医师确认：使用当前可用处方
    if state.modified_formula is not None:
        return _format_modified_formula(state.modified_formula)
    if state.base_formula is not None:
        return _format_formula_result(state.base_formula)

    return "（无处方信息，待医师补充）"


def _format_formula_override(override: dict[str, Any]) -> str:
    """格式化 formula_override dict 为 prompt 可读文本。"""
    lines: list[str] = []
    name = override.get("name", "")
    if name:
        lines.append(f"- 方名：{name}（医师修改方）")
    else:
        lines.append("- 方名：医师修改方")
    composition = override.get("composition", [])
    if composition:
        lines.append("- 组成：")
        for h in composition:
            if isinstance(h, dict):
                herb = h.get("herb", "")
                dose = h.get("dose")
                unit = h.get("unit", "g")
                note = h.get("note", "")
                dose_text = f"{dose}{unit}" if dose is not None else unit
                note_text = f"（{note}）" if note else ""
                lines.append(f"  - {herb} {dose_text}{note_text}")
    return "\n".join(lines)


def _format_modified_formula(mf: ModifiedFormulaResult) -> str:
    """格式化 ModifiedFormulaResult 为 prompt 可读文本。"""
    lines: list[str] = [_format_formula_result(mf.formula)]
    if mf.modifications:
        lines.append("- 加减记录：")
        for mod in mf.modifications:
            lines.append(f"  - {mod.action} {mod.herb} {mod.dose}{mod.unit}：{mod.reason}")
    return "\n".join(lines)


def _format_formula_result(formula: FormulaResult) -> str:
    """格式化 FormulaResult 为 prompt 可读文本。"""
    lines: list[str] = [f"- 方名：{formula.name}"]
    if formula.source:
        lines.append(f"- 出处：{formula.source}")
    lines.append(f"- 方义：{formula.rationale}")
    if formula.composition:
        lines.append("- 组成：")
        for herb in formula.composition:
            dose_text = f"{herb.dose}{herb.unit}" if herb.dose is not None else herb.unit
            note = f"（{herb.note}）" if herb.note else ""
            lines.append(f"  - {herb.herb} {dose_text}{note}")
    return "\n".join(lines)


def build_safety_summary_for_record(state: XuanhuState) -> str:
    """构造安全审核摘要，供 prompt 使用。"""
    safety_review = state.safety_review
    safety_rule_result = state.safety_rule_result

    if safety_review is None and safety_rule_result is None:
        return "（无安全审核记录）"

    parts: list[str] = []
    if safety_review is not None:
        parts.append(f"- 审核通过：{'是' if safety_review.passed else '否'}")
        parts.append(f"- 审核摘要：{safety_review.summary}")
        if safety_review.issues:
            parts.append("- 问题列表：")
            for issue in safety_review.issues:
                severity = issue.severity if isinstance(issue.severity, str) else issue.severity.value
                herbs = "、".join(issue.herbs) if issue.herbs else "无"
                parts.append(f"  - [{severity}] {issue.suggestion}（涉及：{herbs}）")
        if safety_review.explanation:
            parts.append(f"- 解释说明：{safety_review.explanation}")

    if safety_rule_result is not None:
        parts.append(f"- 规则版本：{safety_rule_result.rule_version}")

    return "\n".join(parts)


def build_doctor_review_summary_for_record(state: XuanhuState) -> str:
    """构造医师确认摘要，供 prompt 使用。"""
    doctor_review = state.doctor_review
    if doctor_review is None or not isinstance(doctor_review, dict):
        return "（尚无医师确认记录）"

    action = doctor_review.get("action", "未知")
    action_label = {"confirm": "确认处方", "modify": "修改处方", "reject": "否决处方"}.get(
        action, action
    )

    lines: list[str] = [
        f"- 操作：{action_label}",
        f"- 医师：{doctor_review.get('reviewed_by', '未知')}",
        f"- 确认时间：{doctor_review.get('reviewed_at', '未知')}",
    ]

    feedback = doctor_review.get("feedback")
    if feedback:
        lines.append(f"- 医师反馈：{feedback}")

    if action == "modify":
        override = doctor_review.get("formula_override")
        if override:
            lines.append("- 医师修改处方：已替换原处方")
        lines.append("- 注意：最终处方以医师修改后的版本为准")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# RecordAgent
# ---------------------------------------------------------------------------


class RecordAgent(BaseAgentImpl):
    """病历生成 Agent。

    在 record 阶段基于全量 XuanhuState 与医师确认记录，
    通过 LLM 生成可读病历文本与结构化 JSON 病历。

    通过 `_build_prompt` 将 state 信息注入 prompt 模板，
    不调用 RAG 检索（病历生成不需要新检索证据）。
    """

    name: str = "record"
    stage: Stage = Stage.RECORD
    primary_sources: tuple[str, ...] = ()  # 不检索 RAG
    allow_cross_source: bool = False
    output_schema: type[MedicalRecord] = MedicalRecord
    next_stage: Stage | None = Stage.DONE

    async def _build_prompt(
        self,
        state: XuanhuState,
        evidences: list[Evidence],
    ) -> list[dict[str, Any]]:
        """构造 OpenAI chat messages。

        系统消息来自 prompt 模板（record_v1.jinja2），
        将 state 中的问诊、辨证、处方、安全审核、医师确认信息
        注入模板占位符。
        """
        template = self.prompt_template.content
        state_summary = build_state_summary(state)
        syndrome_summary = build_syndrome_summary_for_record(state)
        formula_summary = build_formula_summary_for_record(state)
        safety_summary = build_safety_summary_for_record(state)
        doctor_review_summary = build_doctor_review_summary_for_record(state)

        system_content = (
            template.replace("{state_summary}", state_summary)
            .replace("{syndrome_summary}", syndrome_summary)
            .replace("{formula_summary}", formula_summary)
            .replace("{safety_summary}", safety_summary)
            .replace("{doctor_review_summary}", doctor_review_summary)
        )

        return [
            {"role": "system", "content": system_content},
        ]


# ---------------------------------------------------------------------------
# 病历落库与 session 更新（由 Supervisor 调用）
# ---------------------------------------------------------------------------


def _build_medical_record_json(state: XuanhuState) -> dict[str, Any]:
    """从 state 构建 record_json 的兜底版本（不依赖 LLM 输出）。

    当 RecordAgent 调用失败或需要降级时使用。
    实际上，在主流程中由 RecordAgent 的 LLM 输出填充。
    """
    patient_info = state.patient_info.model_dump(mode="python")
    four_diagnosis = {
        "inspection": state.four_diagnosis.inspection or "未采集",
        "auscultation_olfaction": state.four_diagnosis.auscultation_olfaction or "未采集",
        "inquiry": state.four_diagnosis.inquiry or "未采集",
        "palpation": state.four_diagnosis.palpation or "未采集",
    }

    syndrome = state.syndrome_result.syndrome if state.syndrome_result else "待医师补充"
    treatment_principle = (
        state.syndrome_result.treatment_principle
        if state.syndrome_result
        else "待医师补充"
    )
    syndrome_analysis = (
        "；".join(state.syndrome_result.syndrome_basis)
        if state.syndrome_result and state.syndrome_result.syndrome_basis
        else "待医师补充"
    )

    return {
        "patient_info": patient_info,
        "chief_complaint": state.chief_complaint or "未采集",
        "present_illness": state.present_illness or "未采集",
        "past_history": state.past_history or "未采集",
        "personal_family_history": state.personal_family_history or "未采集",
        "four_diagnosis": four_diagnosis,
        "syndrome_analysis": syndrome_analysis,
        "syndrome": syndrome,
        "treatment_principle": treatment_principle,
        "formula": state.modified_formula.model_dump(mode="python")
        if state.modified_formula
        else (state.base_formula.model_dump(mode="python") if state.base_formula else {}),
        "advice": [],
        "safety_review": state.safety_review.model_dump(mode="python")
        if state.safety_review
        else {},
        "doctor_review": state.doctor_review or {},
    }


__all__ = [
    "RecordAgent",
    "build_doctor_review_summary_for_record",
    "build_formula_summary_for_record",
    "build_safety_summary_for_record",
    "build_syndrome_summary_for_record",
    "_build_medical_record_json",
]
