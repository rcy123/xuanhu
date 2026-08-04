"""病历叙述 Agent（饮食建议/预后情况）Schema。

只生成「叙述性」内容：饮食建议与预后情况。主诉/现病史/辨证过程/中医诊断/
处方/注意事项等结构化字段由 langgraph_record 确定性拼接，不经过模型。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RECORD_NARRATIVE_INPUT_SCHEMA_VERSION: Literal["record-narrative-input.v1"] = "record-narrative-input.v1"
RECORD_NARRATIVE_OUTPUT_SCHEMA_VERSION: Literal["record-narrative-output.v1"] = "record-narrative-output.v1"
RECORD_NARRATIVE_AGENT_NAME = "record_narrative"
RECORD_NARRATIVE_AGENT_VERSION = "record-narrative-agent.v1"
RECORD_NARRATIVE_PROMPT_VERSION = "record_narrative_v1.jinja2"
RECORD_NARRATIVE_POLICY_VERSION = "record-narrative-policy.v1"
RECORD_NARRATIVE_CONTEXT_TOKEN_LIMIT = 3_200


class RecordNarrativeInput(BaseModel):
    """病历叙述生成的权威上下文投影（全部来自已确认产物，不含原始消息）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["record-narrative-input.v1"] = RECORD_NARRATIVE_INPUT_SCHEMA_VERSION
    session_id: str = Field(min_length=1)
    state_version: int = Field(ge=1)
    policy_version: Literal["record-narrative-policy.v1"] = RECORD_NARRATIVE_POLICY_VERSION
    # 确定性拼接的临床结构（langgraph_record 负责填充）
    chief_complaint: str = Field(default="", max_length=500)
    present_illness: str = Field(default="", max_length=2_000)
    syndrome_process: str = Field(default="", max_length=2_000)
    diagnosis: str = Field(default="", max_length=500)
    treatment_principle: str = Field(default="", max_length=500)
    formula_name: str = Field(default="", max_length=200)
    formula_composition: str = Field(default="", max_length=2_000)
    precautions: str = Field(default="", max_length=1_000)


class RecordNarrativeOutput(BaseModel):
    """模型生成的叙述性段落。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["record-narrative-output.v1"] = RECORD_NARRATIVE_OUTPUT_SCHEMA_VERSION
    diet_advice: str = Field(min_length=1, max_length=500)
    prognosis: str = Field(min_length=1, max_length=500)


class RecordNarrativeResult(BaseModel):
    """执行结果（失败时 output 为 None，调用方降级为模板话术）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["record-narrative-output.v1"] = RECORD_NARRATIVE_OUTPUT_SCHEMA_VERSION
    output: RecordNarrativeOutput | None
    failure_code: str | None = Field(default=None, max_length=64)
