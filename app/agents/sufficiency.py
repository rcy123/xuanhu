"""完备性 Agent —— 判断问诊信息是否充分可进入辨证。

职责：
- 评估当前 XuanhuState 中已采集问诊信息对辨证所需维度的覆盖情况
- 输出 SufficiencyReport（covered / missing / sufficient / suggestions / next_question）
- 不进行辨证，不开方，不输出证型、治法、处方、剂量、安全审核结论

通过 BaseAgentImpl 统一调用模型网关 chat_structured 并处理重试/审计。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgentImpl
from app.agents.inquiry import _format_conversation_history, build_state_summary
from app.rag.schemas import Evidence
from app.schemas.agent import SufficiencyReport, XuanhuState
from app.schemas.types import Stage

logger = logging.getLogger("xuanhu.sufficiency")


def merge_sufficiency_report_to_state(
    state: XuanhuState,
    report: SufficiencyReport,
) -> dict[str, Any]:
    """将 SufficiencyReport 合并为 XuanhuState 的 update dict。

    当前阶段仅写入 sufficiency_report 字段，不改动已采集问诊信息，
    以保证 Supervisor 在信息不足回退 inquiry 时保留此前采集内容。
    """
    del state  # 当前无需依据旧 state 调整；保留签名以便后续扩展
    return {"sufficiency_report": report}


class SufficiencyAgent(BaseAgentImpl):
    """完备性 Agent。

    从当前 XuanhuState（含已合并的问诊信息）评估问诊覆盖情况，
    输出 SufficiencyReport 供 Supervisor 决定回退 inquiry 或推进 syndrome。

    使用 BaseAgentImpl 的统一流程：prompt 加载 → 模型调用 → 校验 → 审计。
    """

    name: str = "sufficiency"
    stage: Stage = Stage.SUFFICIENCY
    output_schema: type[SufficiencyReport] = SufficiencyReport
    next_stage: Stage | None = Stage.SYNDROME

    async def _build_prompt(
        self,
        state: XuanhuState,
        evidences: list[Evidence],
    ) -> list[dict[str, Any]]:
        """构造 OpenAI chat messages。

        系统消息来自 prompt 模板，用户消息为状态摘要和对话历史。
        """
        del evidences  # 完备性判断不调用 RAG

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
        """P5-2 不调用 RAG。"""
        del state, trace_id
        return []
