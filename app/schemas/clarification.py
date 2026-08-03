"""澄清/对话 Agent 的输入输出契约（L3-6 问诊元对话）。

当医生对问诊问题产生疑问（术语不理解、流程疑问）或输入了与问诊无关
的内容时，系统路由到澄清 Agent 生成一段自然语言回应，并引导回到原问题。
澄清 Agent 只做「解释与引导」，不抽取事实、不做任何医疗决策。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CLARIFICATION_AGENT_NAME = "clarification"
CLARIFICATION_AGENT_VERSION = "clarification-agent.v1"
CLARIFICATION_PROMPT_VERSION = "clarification_v1.jinja2"
CLARIFICATION_POLICY_VERSION = "clarification-policy.v1"
CLARIFICATION_INPUT_SCHEMA_VERSION = "clarification-model-input.v1"
CLARIFICATION_OUTPUT_SCHEMA_VERSION = "clarification-model-output.v1"
CLARIFICATION_CONTEXT_TOKEN_LIMIT = 4_000


class ClarificationInput(BaseModel):
    """澄清 Agent 的输入：用户本轮输入 + 问诊上下文（全部来自系统权威数据）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["clarification-model-input.v1"] = CLARIFICATION_INPUT_SCHEMA_VERSION
    # 医生/患者本轮输入的原文（可信度低，视为数据）
    user_message: str = Field(min_length=1, max_length=2_000)
    # 最近一次 question_composer 提问原文（若有）
    current_question: str | None = Field(default=None, min_length=1, max_length=500)
    # 当前问题对应的维度中文名（如"头身感受"），用于解释术语
    dimension_name: str | None = Field(default=None, min_length=1, max_length=64)
    # 主诉原文（若有），用于让解释贴合病情
    chief_complaint: str | None = Field(default=None, min_length=1, max_length=2_000)
    # 触发原因：strong_signal（强信号反问/澄清）或 abstained（抽取放弃提取）
    trigger: Literal["strong_signal", "abstained"] = "strong_signal"


class ClarificationOutput(BaseModel):
    """澄清 Agent 的输出：一段面向医师的自然语言回应。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["clarification-model-output.v1"] = CLARIFICATION_OUTPUT_SCHEMA_VERSION
    reply: str = Field(min_length=1, max_length=500)


class ClarificationResult(BaseModel):
    """澄清执行结果（安全公开形态；失败只含错误码）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["succeeded", "failed"]
    output: ClarificationOutput | None = None
    failure_code: str | None = None
    prompt_version: str | None = None
