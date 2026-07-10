"""悬壶 Harness 与 LangGraph Agent Runtime 公共契约。

本包是 LangGraph v2 执行体系的最小运行底座，不包含临床逻辑。

L1-2 范围：
- ``XuanhuGraphState``：对齐实施计划 §6.2 的最小可序列化执行游标状态。
- ``MainGraph``：START -> command router -> 占位节点 -> END/blocked/manual terminal。
- 命令路由：message/advance/review/recover/unknown。

禁止事项（L1-2 边界）：
- 不接入业务 Agent。
- 不接入真实模型、Redis、RAG 或患者数据。
- 不接入 FastAPI 生产路由。
- 不接入 AsyncPostgresSaver 生产 checkpointer（留给 L1-3）。
- 不实现 GraphRunner/stream（留给 L1-4）。
"""

from __future__ import annotations

from app.agent_runtime.context import (
    ContextBuilder,
    ContextBuilderError,
    ContextPacket,
    PromptLayer,
    PromptMessage,
    PseudonymKeyProvider,
    PseudonymKeyUnavailable,
    TemplateValidationError,
    TokenBudget,
    TokenBudgetExceeded,
    pseudonym,
    render_template,
)
from app.agent_runtime.reducer import (
    DomainDelta,
    DomainReducerError,
    DomainState,
    ReducerErrorCode,
    domain_delta_digest,
    reduce_domain_state,
    validate_domain_delta,
)
from app.agent_runtime.runtime import AgentRuntime, RuntimeErrorBase, RuntimeRunRecorder
from app.agent_runtime.specs import (
    AgentSpec,
    Capability,
    FailurePolicy,
    ModelPolicy,
    RunArtifact,
    RunSpec,
    RuntimeErrorCode,
    TokenUsage,
)
from app.agent_runtime.verifiers import (
    DEFAULT_VERIFIER_CHAIN,
    CheckResult,
    CheckStatus,
    DeltaLegalityVerifier,
    OutputTypeVerifier,
    PrerequisiteVerifier,
    ProvenanceVersionVerifier,
    SchemaVerifier,
    VerificationContext,
    VerificationFailureClass,
    VerificationFailureCode,
    VerificationReport,
    Verifier,
    VerifierChain,
    VerifierName,
)

__all__ = [
    "AgentRuntime",
    "RuntimeErrorBase",
    "RuntimeRunRecorder",
    "AgentSpec",
    "Capability",
    "FailurePolicy",
    "ModelPolicy",
    "RunArtifact",
    "RunSpec",
    "RuntimeErrorCode",
    "TokenUsage",
    "ContextBuilder",
    "ContextBuilderError",
    "ContextPacket",
    "PromptLayer",
    "PromptMessage",
    "PseudonymKeyProvider",
    "PseudonymKeyUnavailable",
    "TemplateValidationError",
    "TokenBudget",
    "TokenBudgetExceeded",
    "pseudonym",
    "render_template",
    "DomainDelta",
    "DomainReducerError",
    "DomainState",
    "ReducerErrorCode",
    "domain_delta_digest",
    "reduce_domain_state",
    "validate_domain_delta",
    "DEFAULT_VERIFIER_CHAIN",
    "CheckResult",
    "CheckStatus",
    "DeltaLegalityVerifier",
    "OutputTypeVerifier",
    "PrerequisiteVerifier",
    "ProvenanceVersionVerifier",
    "SchemaVerifier",
    "VerificationContext",
    "VerificationFailureClass",
    "VerificationFailureCode",
    "VerificationReport",
    "Verifier",
    "VerifierChain",
    "VerifierName",
]
