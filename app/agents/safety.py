"""安全解释 Agent —— 为 SafetyRuleEngine 的结果生成医师可读解释。

职责：
- 读取 SafetyRuleEngine 输出的 SafetyRuleResult
- 生成 SafetyExplanation（summary + issue_explanations + recommendations）
- 不修改 passed / issues / severity / rollback_target
- 不进行 RAG 检索
- 不进行路由决策

通过 BaseAgentImpl 统一调用模型网关 chat_structured 并处理重试/审计。

SafetyAgent 是 SafetyRuleEngine 的下游解释器，其输出仅用于补充解释文本，
不影响 Supervisor 的路由决策（路由始终以 SafetyRuleEngine 的
SafetyRuleResult 为准）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgentImpl
from app.rag.schemas import Evidence
from app.schemas.agent import (
    PatientInfo,
    SafetyExplanation,
    SafetyRuleResult,
    XuanhuState,
)
from app.schemas.types import Stage

logger = logging.getLogger("xuanhu.safety_agent")


# 妊娠状态中文标签
_PREGNANCY_LABELS: dict[str, str] = {
    "unknown": "未知",
    "no": "未妊娠",
    "pregnant": "妊娠中",
    "possible": "可能妊娠",
    "lactating": "哺乳期",
}


def _pregnancy_status_label(state: XuanhuState) -> str:
    """将妊娠状态转换为中文标签。"""
    status = state.patient_info.pregnancy_status
    if status is None:
        return "未知"
    status_str = status.value if hasattr(status, "value") else str(status)
    return _PREGNANCY_LABELS.get(status_str, status_str)


def _format_issues_for_prompt(rule_result: SafetyRuleResult) -> str:
    """将 SafetyRuleResult.issues 格式化为 prompt 可读文本。"""
    if not rule_result.issues:
        return "（无阻断性问题）"
    lines: list[str] = []
    for idx, issue in enumerate(rule_result.issues, start=1):
        herbs = "、".join(issue.herbs) if issue.herbs else "无"
        lines.append(f"### 问题 {idx}")
        lines.append(f"- 类型：{issue.type}")
        lines.append(f"- 严重度：{issue.severity}")
        lines.append(f"- 涉及药材：{herbs}")
        lines.append(f"- 规则来源：{issue.rule_source}")
        lines.append(f"- 规则建议：{issue.suggestion}")
    return "\n".join(lines)


def _format_warnings_for_prompt(rule_result: SafetyRuleResult) -> str:
    """将 SafetyRuleResult.warnings 格式化为 prompt 可读文本。"""
    if not rule_result.warnings:
        return "（无警告）"
    return "\n".join(f"- {w}" for w in rule_result.warnings)


def _format_composition_for_prompt(rule_result: SafetyRuleResult) -> str:
    """将处方组成格式化为 prompt 可读文本。"""
    lines: list[str] = []
    for h in rule_result.normalized_formula.composition:
        dose_str = f"{h.dose}{h.unit}" if h.dose is not None else f"剂量未知({h.unit})"
        note_str = f"（{h.note}）" if h.note else ""
        lines.append(f"  - {h.herb} {dose_str}{note_str}")
    return "\n".join(lines) if lines else "（无）"


def _allergies_text(patient_info: PatientInfo) -> str:
    """格式化过敏史。"""
    allergies = patient_info.allergies or []
    return "、".join(allergies) if allergies else "无"


class SafetyAgent(BaseAgentImpl):
    """安全解释 Agent。

    在 SafetyRuleEngine 完成确定性检查后运行，为医师生成可读的
    安全审核解释文本。不修改路由决策字段（passed / issues /
    severity / rollback_target），这些字段由规则引擎独占。

    使用 BaseAgentImpl 的统一流程：prompt 加载 -> 模型调用 -> 校验 -> 审计。
    """

    name: str = "safety"
    stage: Stage = Stage.SAFETY
    output_schema: type[SafetyExplanation] = SafetyExplanation
    next_stage: Stage | None = None  # 路由由 Supervisor 处理，不由 Agent 决定

    async def _build_prompt(
        self,
        state: XuanhuState,
        evidences: list[Evidence],
    ) -> list[dict[str, Any]]:
        """构造 OpenAI chat messages。

        系统消息来自 prompt 模板，将 SafetyRuleResult 的字段注入模板占位符。
        """
        del evidences  # SafetyAgent 不调用 RAG

        template = self.prompt_template.content
        rule_result = state.safety_rule_result

        if rule_result is None:
            raise ValueError(
                "state.safety_rule_result is None，SafetyAgent 无法生成解释"
            )

        patient_info = state.patient_info
        passed_text = "是" if rule_result.passed else "否"
        issues_text = _format_issues_for_prompt(rule_result)
        warnings_text = _format_warnings_for_prompt(rule_result)
        composition_text = _format_composition_for_prompt(rule_result)
        allergies_text = _allergies_text(patient_info)
        execution_order_text = " -> ".join(rule_result.execution_order)
        patient_age = (
            str(patient_info.age) if patient_info.age is not None else "未知"
        )
        patient_gender = patient_info.gender or "未知"

        system_content = (
            template.replace("{{ passed }}", passed_text)
            .replace("{{ issue_count }}", str(len(rule_result.issues)))
            .replace("{{ warning_count }}", str(len(rule_result.warnings)))
            .replace("{{ rule_version }}", rule_result.rule_version)
            .replace("{{ execution_order }}", execution_order_text)
            .replace("{{ issues_text }}", issues_text)
            .replace("{{ warnings_text }}", warnings_text)
            .replace("{{ formula_name }}", rule_result.normalized_formula.name)
            .replace("{{ composition_text }}", composition_text)
            .replace("{{ formula_rationale }}", rule_result.normalized_formula.rationale)
            .replace("{{ patient_gender }}", patient_gender)
            .replace("{{ patient_age }}", patient_age)
            .replace("{{ allergies_text }}", allergies_text)
            .replace("{{ pregnancy_status }}", _pregnancy_status_label(state))
        )

        return [
            {"role": "system", "content": system_content},
        ]

    async def _retrieve_evidence(
        self, state: XuanhuState, trace_id: str
    ) -> list[Evidence]:
        """SafetyAgent 不调用 RAG。"""
        del state, trace_id
        return []


__all__ = [
    "SafetyAgent",
    "_format_composition_for_prompt",
    "_format_issues_for_prompt",
    "_format_warnings_for_prompt",
    "_pregnancy_status_label",
]
