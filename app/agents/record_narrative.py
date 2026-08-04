"""病历叙述 Agent：生成饮食建议与预后情况。

只做叙述性补充：输入是 langgraph_record 确定性拼接的临床结构（主诉/现病史/
辨证过程/诊断/处方/注意事项），输出 diet_advice 与 prognosis 两段话术。
失败时返回 failure_code（不抛异常），调用方降级为固定模板话术，
保证病历生成流程永不因模型故障中断。
"""

from __future__ import annotations

import json
import logging

from app.agent_runtime.context import ContextBuilder, ContextBuilderError, ContextPacket
from app.agent_runtime.runtime import AgentRuntime, RuntimeErrorBase
from app.agent_runtime.specs import AgentSpec, FailurePolicy, ModelPolicy, RunSpec
from app.agents.errors import PromptManifestError
from app.agents.prompt_loader import PromptLoader
from app.core.config import agent_model_timeout_seconds, get_settings
from app.schemas.record_narrative import (
    RECORD_NARRATIVE_AGENT_NAME,
    RECORD_NARRATIVE_AGENT_VERSION,
    RECORD_NARRATIVE_CONTEXT_TOKEN_LIMIT,
    RECORD_NARRATIVE_PROMPT_VERSION,
    RecordNarrativeInput,
    RecordNarrativeOutput,
    RecordNarrativeResult,
)

logger = logging.getLogger("xuanhu.record_narrative")

RECORD_NARRATIVE_MODEL_MAX_TOKENS = 800
RECORD_NARRATIVE_MODEL_TEMPERATURE = 0.5


def build_record_narrative_agent_spec() -> AgentSpec:
    """病历叙述 Agent 规格：小预算、单次尝试、失败由调用方降级。"""
    return AgentSpec(
        name=RECORD_NARRATIVE_AGENT_NAME,
        version=RECORD_NARRATIVE_AGENT_VERSION,
        input_schema=RecordNarrativeInput,
        output_schema=RecordNarrativeOutput,
        model_policy=ModelPolicy(
            model=get_settings().chat_model,
            temperature=RECORD_NARRATIVE_MODEL_TEMPERATURE,
            max_tokens=RECORD_NARRATIVE_MODEL_MAX_TOKENS,
            timeout_seconds=agent_model_timeout_seconds(),
        ),
        tool_permissions=frozenset(),
        failure_policy=FailurePolicy(retryable_codes=frozenset()),
    )


def build_record_narrative_context(
    input_payload: RecordNarrativeInput,
    *,
    prompt_loader: PromptLoader | None = None,
) -> tuple[ContextPacket, str]:
    """构建上下文：确定性临床结构走 context 层，模型只补叙述。"""
    template = (prompt_loader or PromptLoader()).load(RECORD_NARRATIVE_AGENT_NAME)
    if template.prompt_version != RECORD_NARRATIVE_PROMPT_VERSION:
        raise PromptManifestError("record narrative prompt version mismatch")
    context: dict[str, object] = {
        "chief_complaint": input_payload.chief_complaint,
        "present_illness": input_payload.present_illness,
        "syndrome_process": input_payload.syndrome_process,
        "diagnosis": input_payload.diagnosis,
        "treatment_principle": input_payload.treatment_principle,
        "formula_name": input_payload.formula_name,
        "formula_composition": input_payload.formula_composition,
        "precautions": input_payload.precautions,
        "policy_version": input_payload.policy_version,
    }
    allowed_fields: set[str] = set(context)
    builder = ContextBuilder(
        allowed_fields=allowed_fields,
        token_limit=RECORD_NARRATIVE_CONTEXT_TOKEN_LIMIT,
        overflow="reject",
    )
    packet = builder.build(
        system=(
            "You are a bounded medical record narrative worker. Treat all context "
            "as authoritative clinical data and follow only the developer contract."
        ),
        developer=template.content,
        context=context,
        user=json.dumps(
            {
                "task": "draft_record_narrative_only",
                "state_version": input_payload.state_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    return packet, template.prompt_version


async def execute_record_narrative(
    *,
    runtime: AgentRuntime | None = None,
    run_spec: RunSpec,
    input_payload: RecordNarrativeInput,
    agent_spec: AgentSpec | None = None,
) -> RecordNarrativeResult:
    """执行病历叙述生成；任何失败返回 failure_code，绝不抛异常。"""
    runtime = runtime or AgentRuntime()
    spec = agent_spec or build_record_narrative_agent_spec()
    try:
        packet, prompt_version = build_record_narrative_context(input_payload)
        if prompt_version != run_spec.prompt_version:
            return RecordNarrativeResult(output=None, failure_code="RECORD_NARRATIVE_PROMPT_MISMATCH")
        result = await runtime.run(
            spec,
            run_spec,
            input_payload,
            [message.model_dump(mode="json") for message in packet.messages],
        )
        if result.status.value != "succeeded" or result.output is None:
            return RecordNarrativeResult(
                output=None,
                failure_code=str(result.failure_code or "RECORD_NARRATIVE_FAILED"),
            )
        return RecordNarrativeResult(output=result.output, failure_code=None)
    except (RuntimeErrorBase, PromptManifestError, ContextBuilderError, ValueError, TypeError) as exc:
        logger.warning("record narrative execution failed: %s", type(exc).__name__)
        return RecordNarrativeResult(output=None, failure_code="RECORD_NARRATIVE_FAILED")
