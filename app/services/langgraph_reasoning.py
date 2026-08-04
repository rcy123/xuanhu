"""LangGraph-backed L4-4 reasoning flow.

This service owns orchestration only. Syndrome and Formula agents still run
through their L4 execution boundaries; durable clinical outputs are persisted
as versioned artifact payloads and Graph State only receives references.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_logger = logging.getLogger("xuanhu.services.langgraph_reasoning")

from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1, NODE_REASONING_SUBGRAPH_V1
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION
from app.agent_runtime.ephemeral_cache import BoundedTTLCache
from app.agent_runtime.formula_consistency import (
    FORMULA_CONSISTENCY_POLICY_VERSION,
    FormulaConsistencyFailureCode,
    FormulaConsistencyReport,
    verify_trusted_formula_execution,
)
from app.agent_runtime.formula_verifier import (
    FORMULA_AGENT_NAME,
    FORMULA_AGENT_VERSION,
    FORMULA_POLICY_PROMPT_PAIRS,
    FORMULA_PROMPT_VERSION,
    FORMULA_RAG_PROMPT_VERSION,
    FormulaVerificationReport,
)
from app.agent_runtime.reasoning_subgraph import (
    ROUTE_FORMULA_COMPLETED,
    ROUTE_MANUAL_REQUIRED,
    ROUTE_NEEDS_MORE_INFO,
    ROUTE_SYNDROME_COMPLETED,
)
from app.agent_runtime.reducer import DomainDelta, DomainState, _current_observations
from app.agent_runtime.repository import (
    AgentEvidenceSpec,
    AgentRunSpec,
    ArtifactPayloadRecord,
    ArtifactPayloadSpec,
    ConsultMessageSpec,
    GraphStepSpec,
    PostgresDomainRepository,
    ReasoningAuthoritySnapshot,
    RepositoryError,
    RepositoryErrorCode,
    artifact_payload_digest,
)
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.specs import (
    AgentSpec,
    Capability,
    FailurePolicy,
    ModelPolicy,
    RunArtifact,
    RunSpec,
    TokenUsage,
    run_artifact_subject_digest,
)
from app.agent_runtime.state import ArtifactRef, XuanhuGraphState
from app.agent_runtime.syndrome_verifier import (
    SYNDROME_AGENT_NAME,
    SYNDROME_AGENT_VERSION,
    SYNDROME_PROMPT_VERSION,
    SYNDROME_RAG_PROMPT_VERSION,
)
from app.agent_runtime.verifiers import DEFAULT_VERIFIER_CHAIN, VerificationContext
from app.agents.formula_draft import (
    BASE_FORMULA_AGENT_VERSION,
    BASE_FORMULA_RAG_POLICY_VERSION,
    MODIFICATION_DRAFT_AGENT_VERSION,
    MODIFICATION_DRAFT_RAG_POLICY_VERSION,
    FormulaExecutionResult,
    FormulaExecutionStatus,
    _consume_trusted_formula_execution,
    build_base_formula_agent_spec,
    build_modification_draft_agent_spec,
    execute_base_formula_draft,
    execute_formula_draft,
    execute_modification_draft,
)
from app.agents.question_composer import QUESTION_TEMPLATES, validate_single_question_text
from app.agents.syndrome_draft import (
    SyndromeExecutionResult,
    SyndromeExecutionStatus,
    _consume_trusted_syndrome_execution,
    build_syndrome_agent_spec,
    execute_syndrome_draft,
    recover_trusted_syndrome_from_repository,
)
from app.core.config import agent_model_timeout_seconds, get_settings
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.models.domain import GraphRun, IntakeCommandClaim
from app.rag.reasoning_retrieval import stage_rag_enabled
from app.rag.schemas import Evidence
from app.schemas.domain import ArtifactRevisionSchema, ArtifactStatus
from app.schemas.formula import (
    BASE_FORMULA_POLICY_VERSION,
    BASE_FORMULA_PROMPT_VERSION,
    BASE_FORMULA_RAG_PROMPT_VERSION,
    FORMULA_POLICY_VERSION,
    FORMULA_RAG_POLICY_VERSION,
    FORMULA_READY_STAGE,
    MODIFICATION_DRAFT_INPUT_SCHEMA_VERSION,
    MODIFICATION_DRAFT_POLICY_VERSION,
    MODIFICATION_DRAFT_PROMPT_VERSION,
    MODIFICATION_DRAFT_RAG_PROMPT_VERSION,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaDraftInput,
    ModificationDraftInput,
)
from app.schemas.question import GapSelectionKind
from app.schemas.syndrome import (
    SYNDROME_POLICY_VERSION,
    SYNDROME_RAG_POLICY_VERSION,
    SYNDROME_READY_STAGE,
    SyndromeDraft,
    SyndromeDraftDecision,
    SyndromeDraftInput,
    SyndromeObservationContext,
)

REASONING_COMMAND_COMPLETED = "reasoning.command_completed.v1"
REASONING_ARTIFACT_COMMITTED = "reasoning.artifact_committed.v1"
SYNDROME_ARTIFACT_TYPE = "syndrome_draft"
FORMULA_ARTIFACT_TYPE = "formula_draft"
CONTROL_ARTIFACT_TYPE = "reasoning_control"
SYNDROME_PAYLOAD_SCHEMA_VERSION = "syndrome-artifact-payload.v1"
FORMULA_PAYLOAD_SCHEMA_VERSION = "formula-artifact-payload.v1"
CONTROL_PAYLOAD_SCHEMA_VERSION = "reasoning-control-payload.v1"

_SYNDROME_RESULT_CACHE: BoundedTTLCache[uuid.UUID, SyndromeExecutionResult] = BoundedTTLCache(
    max_size=256,
    ttl_seconds=300,
)
_FORMULA_ROUTE_CACHE: BoundedTTLCache[uuid.UUID, str] = BoundedTTLCache(max_size=256, ttl_seconds=300)
_execute_syndrome = cast(Callable[..., Awaitable[SyndromeExecutionResult]], execute_syndrome_draft)
_execute_formula = cast(Callable[..., Awaitable[Any]], execute_formula_draft)


class _EmptyOutput(BaseModel):
    ok: bool = True


def _deadline(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _stable_ref(prefix: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._:-]+", "_", value).strip("._:-")
    if safe and len(safe) <= 96:
        return f"{prefix}:{safe}"
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _node_trace_id(state: XuanhuGraphState) -> str:
    return state.get("run_id") or state.get("command_id") or ""


def _artifact_id(session_id: uuid.UUID, artifact_type: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{artifact_type}:{session_id}")


def _commit_run_id(claim: IntakeCommandClaim, step: str) -> uuid.UUID:
    if step == "syndrome":
        return claim.run_id
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:reasoning:{claim.run_id}:{step}")


def _agent_run_spec(
    *,
    run_spec: RunSpec,
    agent_name: str,
    input_payload: BaseModel,
    output: BaseModel,
    artifact: RunArtifact,
) -> AgentRunSpec:
    """构造 agent_runs 投影（轻量摘要；完整内容在 artifact payload 中）。"""
    return AgentRunSpec(
        run_id=run_spec.run_id,
        session_id=run_spec.session_id,
        agent_name=agent_name,
        stage=run_spec.stage,
        input_snapshot={
            "state_version": getattr(input_payload, "state_version", None),
            "policy_version": run_spec.policy_version,
            "observation_count": len(getattr(input_payload, "context_observations", ())),
        },
        output_snapshot={
            "decision": str(getattr(output, "decision", "")),
            "evidence_mode": getattr(output, "evidence_mode", None),
            "confidence": getattr(output, "confidence", None),
            "claim_evidence_links": len(getattr(output, "claim_evidence_links", ())),
        },
        prompt_version=run_spec.prompt_version,
        model=artifact.model_actual or "",
        retry_count=0,
        status="success",
        error_code=None,
        latency_ms=artifact.latency_ms,
        trace_id=run_spec.trace_id,
    )


def _agent_evidence_specs(
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    retrieved_evidence: tuple[Evidence, ...],
) -> tuple[AgentEvidenceSpec, ...]:
    """构造 agent_evidences 投影（确定性行 ID，幂等重放不产生重复行）。"""
    specs: list[AgentEvidenceSpec] = []
    for evidence in retrieved_evidence:
        try:
            source_id = uuid.UUID(str(evidence.source_id))
        except (ValueError, TypeError):
            source_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:evidence-source:{evidence.source_id}")
        chunk_id = None
        if evidence.chunk_id:
            try:
                chunk_id = uuid.UUID(str(evidence.chunk_id))
            except (ValueError, TypeError):
                chunk_id = None
        specs.append(
            AgentEvidenceSpec(
                evidence_row_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"xuanhu:evidence:{run_id}:{evidence.evidence_id}",
                ),
                agent_run_id=run_id,
                session_id=session_id,
                evidence_id=evidence.evidence_id,
                source_type=evidence.source_type,
                source_id=source_id,
                chunk_id=chunk_id,
                title=evidence.title,
                content_snippet=evidence.content_snippet,
                score=evidence.score,
                rank=evidence.rank,
            )
        )
    return tuple(specs)


def _reasoning_policy(stage: str) -> tuple[str, str]:
    """编排层按配置选 (policy_version, prompt_version)（D1：策略级决策）。

    RAG 开关为 true 时返回 *.rag.v1 配对，否则维持 *.no-rag.v1 配对。
    agent 内按 input_payload.policy_version 分支检索；verifier 按
    run_spec.policy_version 分派校验——双端一致由本函数唯一决定。
    2.8 两阶段开方：base_formula / modification 各自独立的 policy/prompt。
    """
    if stage_rag_enabled(stage):
        if stage == "syndrome":
            return SYNDROME_RAG_POLICY_VERSION, SYNDROME_RAG_PROMPT_VERSION
        if stage == "base_formula":
            return BASE_FORMULA_RAG_POLICY_VERSION, BASE_FORMULA_RAG_PROMPT_VERSION
        if stage == "modification":
            return MODIFICATION_DRAFT_RAG_POLICY_VERSION, MODIFICATION_DRAFT_RAG_PROMPT_VERSION
        return FORMULA_RAG_POLICY_VERSION, FORMULA_RAG_PROMPT_VERSION
    if stage == "syndrome":
        return SYNDROME_POLICY_VERSION, SYNDROME_PROMPT_VERSION
    if stage == "base_formula":
        return BASE_FORMULA_POLICY_VERSION, BASE_FORMULA_PROMPT_VERSION
    if stage == "modification":
        return MODIFICATION_DRAFT_POLICY_VERSION, MODIFICATION_DRAFT_PROMPT_VERSION
    return FORMULA_POLICY_VERSION, FORMULA_PROMPT_VERSION


def _rag_retriever() -> Any | None:
    """编排层构造推理检索器（延迟 import，避免启动耦合）。

    总开关关闭时返回 None；构造失败返回 None（agent 内走空证据 RAG 模式，
    不阻断推理——D3 降级）。RAGRetriever 构造不建立连接（延迟连接）。
    """
    if not get_settings().rag_enabled:
        return None
    try:
        from app.rag.retriever import RAGRetriever

        return RAGRetriever()
    except Exception as exc:  # noqa: BLE001 - 检索器不可用必须降级而非阻断
        _logger.warning("RAG retriever 构造失败，推理将走空证据降级: %s: %s", type(exc).__name__, str(exc))
        return None


# 模型质量类随机失败：同输入重放一次大概率成功（与 intake 抽取同款策略）。
# 真实会话（229a048c 等）：formula/syndrome 草稿偶发 confidence 超 no-RAG 上限、
# schema 越界、截断、网关超时——旧实现一律 manual_required → blocked，瞬时失败
# 把整个辨证开方永久卡死（只能 recover 重跑）。此处对可重试码自动重试一次。
_REASONING_RETRYABLE_MODEL_CODES = frozenset(
    {
        "SYNDROME_SCHEMA_INVALID",
        "SYNDROME_OUTPUT_TYPE_INVALID",
        "SYNDROME_DECISION_CONTENT_INVALID",
        "SYNDROME_CONFIDENCE_EXCEEDS_NO_RAG_LIMIT",
        "SYNDROME_CONFIDENCE_EXCEEDS_RAG_LIMIT",
        "SYNDROME_EVIDENCE_LINK_FABRICATED",
        "SYNDROME_EVIDENCE_MODE_POLICY_MISMATCH",
        "SYNDROME_FACT_LINK_INVALID",
        "FORMULA_SCHEMA_INVALID",
        "FORMULA_OUTPUT_TYPE_INVALID",
        "FORMULA_DECISION_CONTENT_INVALID",
        "FORMULA_CONFIDENCE_EXCEEDS_NO_RAG_LIMIT",
        "FORMULA_CONFIDENCE_EXCEEDS_RAG_LIMIT",
        "FORMULA_EVIDENCE_LINK_FABRICATED",
        "FORMULA_EVIDENCE_MODE_POLICY_MISMATCH",
        "FORMULA_FACT_LINK_INVALID",
        "FORMULA_SYNDROME_FACT_LINK_INVALID",
        "STRUCTURED_OUTPUT_INVALID",
        "MODEL_OUTPUT_TRUNCATED",
        "MODEL_GATEWAY_TIMEOUT",
        "MODEL_GATEWAY_UNAVAILABLE",
        # 2.8 一致性校验失败（模型输出与基础方矛盾，如加减引用不存在的药）：
        # 属模型随机质量问题，同输入重放大概率自愈；仍失败才 manual_required。
        *(code.value for code in FormulaConsistencyFailureCode),
    }
)


def _reasoning_failure_is_retryable(code: str | None) -> bool:
    return (code or "") in _REASONING_RETRYABLE_MODEL_CODES


def _reasoning_retry_spec(original: RunSpec, *, suffix: str, attempt: int = 1) -> RunSpec:
    """Fresh RunSpec for a retry (new run_id / idempotency key).

    ``attempt`` 从 1 开始（第 1 次重试）。deadline 随尝试递增，
    避免模型随机慢响应再次撞节点超时。
    """
    return original.model_copy(
        update={
            "run_id": uuid.uuid4(),
            "idempotency_key": f"{original.idempotency_key}:retry:{suffix}:{attempt}",
            "deadline_at": _deadline(agent_model_timeout_seconds() * (attempt + 1) + 30),
        }
    )


def _reasoning_retry_backoff_seconds(attempt: int) -> float:
    """第 ``attempt`` 次重试前的等待秒数（指数退避）。

    第三方中转网关（如 dmxapi）对短时高频调用有频率风控（403，窗口约 30-60s），
    重试必须退避以跨过风控窗口，否则 3 连发必然全挂。基数可经
    REASONING_RETRY_BACKOFF_BASE_SECONDS 配置（0=关闭，测试用）。
    """
    from app.core.config import get_settings

    base = get_settings().reasoning_retry_backoff_base_seconds
    return base * attempt


# 模型质量/网关类失败的最大重试次数（同输入重放，随机失败大概率自愈）。
_REASONING_MAX_RETRIES = 2


async def run_reasoning_precheck_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        authority = await _current_authority(repository, claim.session_id)
        if authority is None:
            await _mark_claim_failed(claim.id, "REASONING_PRECHECK_FAILED")
            return _sanitized_graph_error(state, "REASONING_PRECHECK_FAILED", "reasoning authority is unavailable")
        await _save_intermediate(
            claim.id,
            {
                "precheck": {
                    "input_state_version": authority.current_state_version,
                    "source_gate_state_version": authority.source_gate_state_version,
                    "source_gate_id": str(authority.source_gate_id),
                }
            },
            step="reasoning_precheck",
        )
        return {
            "route": NODE_REASONING_SUBGRAPH_V1,
            "domain_state_version": authority.current_state_version,
            "last_error": None,
        }
    finally:
        await db.close()


async def run_reasoning_build_syndrome_context_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        authority = await _current_authority(repository, claim.session_id)
        if authority is None:
            return _sanitized_graph_error(state, "REASONING_AUTHORITY_MISSING", "reasoning authority is unavailable")
        feedback = await _load_review_feedback(claim.session_id)
        input_payload = _build_syndrome_input(authority, feedback)
        await _save_intermediate(
            claim.id,
            {
                "syndrome_context": {
                    "input_state_version": input_payload.state_version,
                    "active_observation_count": len(input_payload.context_observations),
                }
            },
            step="build_syndrome_context",
        )
        return {
            "route": NODE_REASONING_SUBGRAPH_V1,
            "domain_state_version": input_payload.state_version,
            "last_error": None,
        }
    finally:
        await db.close()


async def run_reasoning_draft_syndrome_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        authority = await _current_authority(repository, claim.session_id)
        if authority is None:
            return _sanitized_graph_error(state, "REASONING_AUTHORITY_MISSING", "reasoning authority is unavailable")
        recovered = await _load_trusted_syndrome_result(repository, claim.session_id, authority)
        if recovered is not None:
            _SYNDROME_RESULT_CACHE[claim.id] = recovered
            await _save_intermediate_step(claim.id, "draft_syndrome")
            return _syndrome_graph_update(claim, recovered, await repository.get_state(claim.session_id))

        feedback = await _load_review_feedback(claim.session_id)
        input_payload = _build_syndrome_input(authority, feedback)
        syndrome_policy, syndrome_prompt = _reasoning_policy("syndrome")
        run_spec = RunSpec(
            run_id=_commit_run_id(claim, "syndrome"),
            session_id=claim.session_id,
            state_version=input_payload.state_version,
            stage=SYNDROME_READY_STAGE,
            agent_spec_version=SYNDROME_AGENT_VERSION,
            prompt_version=syndrome_prompt,
            policy_version=syndrome_policy,
            # 节点级 RunSpec deadline 需 > SYNDROME AgentSpec ModelPolicy.timeout
            # （= 网关超时 + 余量），避免外层先判超时错误归因为 MODEL_GATEWAY_TIMEOUT。
            deadline_at=_deadline(agent_model_timeout_seconds() + 15),
            total_attempt_budget=1,
            idempotency_key=f"{claim.idempotency_key}:syndrome",
            trace_id=_node_trace_id(state),
        )
        result = await _execute_syndrome(
            runtime=AgentRuntime(),
            repository=repository,
            run_spec=run_spec,
            input_payload=input_payload,
            agent_spec=build_syndrome_agent_spec(),
            retriever=_rag_retriever(),
        )
        if result.status is not SyndromeExecutionStatus.SUCCEEDED or result.output is None or result.verification is None:
            code = str(result.failure_code or "SYNDROME_FAILED")
            retried = 0
            if _reasoning_failure_is_retryable(code):
                while retried < _REASONING_MAX_RETRIES:
                    retried += 1
                    # 第三方网关频率风控（403）：重试前退避，跨过风控窗口。
                    await asyncio.sleep(_reasoning_retry_backoff_seconds(retried))
                    retried_result = await _execute_syndrome(
                        runtime=AgentRuntime(),
                        repository=repository,
                        run_spec=_reasoning_retry_spec(run_spec, suffix="syndrome", attempt=retried),
                        input_payload=input_payload,
                        agent_spec=build_syndrome_agent_spec(),
                    )
                    if (
                        retried_result.status is SyndromeExecutionStatus.SUCCEEDED
                        and retried_result.output is not None
                        and retried_result.verification is not None
                    ):
                        result = retried_result
                        code = None
                        break
            await _save_intermediate(
                claim.id,
                {
                    "syndrome": {
                        "failure_code": code or "SYNDROME_FAILED",
                        "retry_count": retried,
                        "retried": code is None,
                    }
                },
                step="draft_syndrome",
            )
            if code is not None:
                return {
                    "route": NODE_REASONING_SUBGRAPH_V1,
                    "reasoning_route": ROUTE_MANUAL_REQUIRED,
                    "last_error": None,
                }
        commit = await _commit_syndrome_artifact(repository, claim, result, trace_id=_node_trace_id(state))
        if commit is None:
            await _save_intermediate(
                claim.id,
                {"syndrome": {"failure_code": "SYNDROME_TRUSTED_EXECUTION_MISSING"}},
                step="draft_syndrome",
            )
            return {"route": NODE_REASONING_SUBGRAPH_V1, "reasoning_route": ROUTE_MANUAL_REQUIRED, "last_error": None}
        _SYNDROME_RESULT_CACHE[claim.id] = result
        await _save_intermediate(
            claim.id,
            {
                "syndrome": {
                    "decision": result.output.decision.value,
                    "artifact_id": str(_artifact_id(claim.session_id, SYNDROME_ARTIFACT_TYPE)),
                    "revision": commit["revision"],
                    "content_digest": commit["content_digest"],
                    "output_state_version": commit["output_state_version"],
                    "missing_dimension": (
                        result.output.missing_inputs[0].value if result.output.missing_inputs else None
                    ),
                }
            },
            step="draft_syndrome",
        )
        return _syndrome_graph_update(claim, result, await repository.get_state(claim.session_id))
    finally:
        await db.close()


async def run_reasoning_verify_syndrome_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        authority = await _current_authority(repository, claim.session_id)
        result = (
            _SYNDROME_RESULT_CACHE.get(claim.id)
            or await _load_trusted_syndrome_result(repository, claim.session_id, authority)
        )
        if result is None or result.output is None:
            return {"route": NODE_REASONING_SUBGRAPH_V1, "reasoning_route": ROUTE_MANUAL_REQUIRED, "last_error": None}
        if result.output.decision is SyndromeDraftDecision.COMPLETED:
            reasoning_route = ROUTE_SYNDROME_COMPLETED
        elif result.output.decision is SyndromeDraftDecision.NEEDS_MORE_INFO:
            reasoning_route = ROUTE_NEEDS_MORE_INFO
        else:
            reasoning_route = ROUTE_MANUAL_REQUIRED
        await _save_intermediate(
            claim.id,
            {"syndrome_verifier": {"decision": result.output.decision.value, "route": reasoning_route}},
            step="verify_syndrome",
        )
        return {
            "route": NODE_REASONING_SUBGRAPH_V1,
            "reasoning_route": reasoning_route,
            "domain_state_version": (await repository.get_state(claim.session_id)).state_version,
            "last_error": None,
        }
    finally:
        await db.close()


async def run_reasoning_build_formula_context_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        authority = await _current_authority(repository, claim.session_id)
        syndrome_result = (
            _SYNDROME_RESULT_CACHE.get(claim.id)
            or await _load_trusted_syndrome_result(repository, claim.session_id, authority)
        )
        if syndrome_result is None or syndrome_result.output is None or authority is None:
            return _sanitized_graph_error(state, "FORMULA_CONTEXT_MISSING", "formula context authority is unavailable")
        feedback = await _load_review_feedback(claim.session_id)
        input_payload = _build_formula_input(authority, syndrome_result.output, feedback)
        await _save_intermediate(
            claim.id,
            {
                "formula_context": {
                    "input_state_version": input_payload.state_version,
                    "syndrome_artifact_id": str(_artifact_id(claim.session_id, SYNDROME_ARTIFACT_TYPE)),
                    "active_observation_count": len(input_payload.context_observations),
                }
            },
            step="build_formula_context",
        )
        return {
            "route": NODE_REASONING_SUBGRAPH_V1,
            "domain_state_version": input_payload.state_version,
            "last_error": None,
        }
    finally:
        await db.close()


async def _run_formula_stage_with_retry(
    claim: IntakeCommandClaim,
    repository: PostgresDomainRepository,
    syndrome_result: SyndromeExecutionResult,
    executor: Callable[..., Awaitable[FormulaExecutionResult]],
    run_spec: RunSpec,
    *,
    input_payload: FormulaDraftInput | ModificationDraftInput,
    agent_spec: AgentSpec,
    step_label: str,
) -> tuple[FormulaExecutionResult, FormulaConsistencyReport] | None:
    """2.8 执行一个开方阶段（base_formula / modification）+ 统一重试循环。

    执行失败（模型质量/网关）与一致性校验失败（加减引用基础方不存在的药等）
    都是模型随机质量问题，同输入重放大概率自愈；共用 _REASONING_MAX_RETRIES
    预算，重试耗尽返回 None（intermediate 已留痕，调用方转 manual_required）。
    """
    result = await executor(
        runtime=AgentRuntime(),
        repository=repository,
        run_spec=run_spec,
        input_payload=input_payload,
        syndrome_result=syndrome_result,
        agent_spec=agent_spec,
        retriever=_rag_retriever(),
    )
    consistency = verify_trusted_formula_execution(result)
    retried = 0
    while True:
        retry_code: str | None = None
        if result.status is not FormulaExecutionStatus.SUCCEEDED or result.output is None:
            retry_code = str(result.failure_code or "FORMULA_FAILED")
        elif result.output.decision is FormulaDraftDecision.COMPLETED and not consistency.passed:
            retry_code = str(consistency.failure_code or "FORMULA_CONSISTENCY_FAILED")
        if retry_code is None:
            break
        if not _reasoning_failure_is_retryable(retry_code) or retried >= _REASONING_MAX_RETRIES:
            await _save_intermediate(
                claim.id,
                {
                    "formula": {
                        "stage": step_label,
                        "failure_code": retry_code,
                        "retry_count": retried,
                        "retried": False,
                    },
                    "formula_consistency": (
                        {
                            "passed": False,
                            "failure_code": retry_code,
                        }
                        if result.status is FormulaExecutionStatus.SUCCEEDED
                        and result.output is not None
                        else None
                    ),
                },
                step="draft_formula",
            )
            return None
        retried += 1
        # 第三方网关频率风控（403）：重试前退避，跨过风控窗口。
        await asyncio.sleep(_reasoning_retry_backoff_seconds(retried))
        retried_result = await executor(
            runtime=AgentRuntime(),
            repository=repository,
            run_spec=_reasoning_retry_spec(run_spec, suffix=step_label, attempt=retried),
            input_payload=input_payload,
            syndrome_result=syndrome_result,
            agent_spec=agent_spec,
        )
        if retried_result.status is not FormulaExecutionStatus.SUCCEEDED or retried_result.output is None:
            result = retried_result
            consistency = FormulaConsistencyReport(passed=False, failure_code=None)
            continue
        result = retried_result
        consistency = verify_trusted_formula_execution(result)
    if retried:
        await _save_intermediate(
            claim.id,
            {"formula": {"stage": step_label, "retry_count": retried, "retried": True}},
            step="draft_formula",
        )
    return result, consistency


async def run_reasoning_draft_formula_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        existing = await repository.get_artifact_payload(
            claim.session_id,
            artifact_type=FORMULA_ARTIFACT_TYPE,
            artifact_id=_artifact_id(claim.session_id, FORMULA_ARTIFACT_TYPE),
            status="current",
        )
        if existing is not None and existing.produced_by_run_id == _commit_run_id(claim, "formula"):
            route = _formula_route_from_record(existing)
            _FORMULA_ROUTE_CACHE[claim.id] = route
            await _save_intermediate_step(claim.id, "draft_formula")
            return _formula_graph_update(existing, route)

        authority = await _current_authority(repository, claim.session_id)
        syndrome_result = (
            _SYNDROME_RESULT_CACHE.get(claim.id)
            or await _load_trusted_syndrome_result(repository, claim.session_id, authority)
        )
        if syndrome_result is None or syndrome_result.output is None or authority is None:
            return _sanitized_graph_error(state, "FORMULA_CONTEXT_MISSING", "formula context authority is unavailable")
        feedback = await _load_review_feedback(claim.session_id)
        input_payload = _build_formula_input(authority, syndrome_result.output, feedback)
        # ---- 2.8 阶段 1：基础方草稿（仅选方，不做加减）----
        base_policy, base_prompt = _reasoning_policy("base_formula")
        # preflight 强制 input.policy_version == run_spec.policy_version：
        # 基础方阶段使用 base 专属 policy/prompt 配对。
        input_payload = input_payload.model_copy(update={"policy_version": base_policy})
        base_policy, base_prompt = _reasoning_policy("base_formula")
        base_run_spec = RunSpec(
            run_id=_commit_run_id(claim, "base_formula"),
            session_id=claim.session_id,
            state_version=input_payload.state_version,
            stage=FORMULA_READY_STAGE,
            agent_spec_version=BASE_FORMULA_AGENT_VERSION,
            prompt_version=base_prompt,
            policy_version=base_policy,
            deadline_at=_deadline(agent_model_timeout_seconds() + 15),
            total_attempt_budget=1,
            idempotency_key=f"{claim.idempotency_key}:base_formula",
            trace_id=_node_trace_id(state),
        )
        base_stage = await _run_formula_stage_with_retry(
            claim,
            repository,
            syndrome_result,
            execute_base_formula_draft,
            base_run_spec,
            input_payload=input_payload,
            agent_spec=build_base_formula_agent_spec(),
            step_label="base_formula",
        )
        if base_stage is None:
            _FORMULA_ROUTE_CACHE[claim.id] = ROUTE_MANUAL_REQUIRED
            return {
                "route": NODE_REASONING_SUBGRAPH_V1,
                "reasoning_route": ROUTE_MANUAL_REQUIRED,
                "last_error": None,
            }
        base_result, _ = base_stage
        base_formula = base_result.output.base_formula
        base_missing = _formula_missing_dimension(base_result.output)
        if base_result.output.decision is not FormulaDraftDecision.COMPLETED or base_formula is None:
            # 基础方阶段 needs_more_info/abstained：走与单步一致的缺失分支
            if base_result.output.decision is FormulaDraftDecision.NEEDS_MORE_INFO and base_missing is None:
                await _save_intermediate(
                    claim.id,
                    {
                        "formula": {
                            "stage": "base_formula",
                            "decision": base_result.output.decision.value,
                            "failure_code": "FORMULA_MISSING_INPUT_UNMAPPED",
                        }
                    },
                    step="draft_formula",
                )
            _FORMULA_ROUTE_CACHE[claim.id] = ROUTE_MANUAL_REQUIRED
            return {
                "route": NODE_REASONING_SUBGRAPH_V1,
                "reasoning_route": ROUTE_MANUAL_REQUIRED,
                "last_error": None,
            }

        # ---- 2.8 阶段 2：加减草稿（输入含权威基础方全文）----
        mod_policy, mod_prompt = _reasoning_policy("modification")
        mod_input = ModificationDraftInput(
            schema_version=MODIFICATION_DRAFT_INPUT_SCHEMA_VERSION,
            session_id=input_payload.session_id,
            state_version=input_payload.state_version,
            current_stage=input_payload.current_stage,
            policy_version=mod_policy,
            domain_state=input_payload.domain_state,
            triage_gate=input_payload.triage_gate,
            completeness_gate=input_payload.completeness_gate,
            context_observations=input_payload.context_observations,
            syndrome_draft=input_payload.syndrome_draft,
            base_formula=base_formula,
            base_formula_rationale=base_result.output.rationale,
            base_confidence=base_result.output.confidence,
            review_feedback=input_payload.review_feedback,
        )
        mod_run_spec = RunSpec(
            run_id=_commit_run_id(claim, "modification"),
            session_id=claim.session_id,
            state_version=input_payload.state_version,
            stage=FORMULA_READY_STAGE,
            agent_spec_version=MODIFICATION_DRAFT_AGENT_VERSION,
            prompt_version=mod_prompt,
            policy_version=mod_policy,
            deadline_at=_deadline(agent_model_timeout_seconds() + 15),
            total_attempt_budget=1,
            idempotency_key=f"{claim.idempotency_key}:modification",
            trace_id=_node_trace_id(state),
        )
        mod_stage = await _run_formula_stage_with_retry(
            claim,
            repository,
            syndrome_result,
            execute_modification_draft,
            mod_run_spec,
            input_payload=mod_input,
            agent_spec=build_modification_draft_agent_spec(),
            step_label="modification",
        )
        if mod_stage is None:
            _FORMULA_ROUTE_CACHE[claim.id] = ROUTE_MANUAL_REQUIRED
            return {
                "route": NODE_REASONING_SUBGRAPH_V1,
                "reasoning_route": ROUTE_MANUAL_REQUIRED,
                "last_error": None,
            }
        result, consistency = mod_stage
        formula_missing_dimension = _formula_missing_dimension(result.output)
        if result.output.decision is FormulaDraftDecision.NEEDS_MORE_INFO and formula_missing_dimension is None:
            await _save_intermediate(
                claim.id,
                {
                    "formula": {
                        "decision": result.output.decision.value,
                        "failure_code": "FORMULA_MISSING_INPUT_UNMAPPED",
                    }
                },
                step="draft_formula",
            )
            _FORMULA_ROUTE_CACHE[claim.id] = ROUTE_MANUAL_REQUIRED
            return {"route": NODE_REASONING_SUBGRAPH_V1, "reasoning_route": ROUTE_MANUAL_REQUIRED, "last_error": None}

        commit = await _commit_formula_artifact(
            repository,
            claim,
            result,
            consistency,
            trace_id=_node_trace_id(state),
        )
        if commit is None:
            await _save_intermediate(
                claim.id,
                {"formula": {"failure_code": "FORMULA_TRUSTED_EXECUTION_MISSING"}},
                step="draft_formula",
            )
            _FORMULA_ROUTE_CACHE[claim.id] = ROUTE_MANUAL_REQUIRED
            return {"route": NODE_REASONING_SUBGRAPH_V1, "reasoning_route": ROUTE_MANUAL_REQUIRED, "last_error": None}
        if result.output.decision is FormulaDraftDecision.COMPLETED:
            route = ROUTE_FORMULA_COMPLETED
        elif result.output.decision is FormulaDraftDecision.NEEDS_MORE_INFO:
            route = ROUTE_NEEDS_MORE_INFO
        else:
            route = ROUTE_MANUAL_REQUIRED
        _FORMULA_ROUTE_CACHE[claim.id] = route
        await _save_intermediate(
            claim.id,
            {
                "formula": {
                    "decision": result.output.decision.value,
                    "artifact_id": str(_artifact_id(claim.session_id, FORMULA_ARTIFACT_TYPE)),
                    "revision": commit["revision"],
                    "content_digest": commit["content_digest"],
                    "output_state_version": commit["output_state_version"],
                    "missing_dimension": formula_missing_dimension,
                },
                "formula_consistency": {
                    "passed": consistency.passed,
                    "policy_version": FORMULA_CONSISTENCY_POLICY_VERSION,
                    "failure_code": str(consistency.failure_code) if consistency.failure_code else None,
                },
            },
            step="draft_formula",
        )
        return _formula_graph_update_from_commit(claim, route, commit)
    finally:
        await db.close()


async def run_reasoning_verify_formula_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        route = _FORMULA_ROUTE_CACHE.get(claim.id) or _route_from_intermediate(claim, "formula")
        if route is None:
            repository = PostgresDomainRepository(get_session_factory())
            record = await repository.get_artifact_payload(
                claim.session_id,
                artifact_type=FORMULA_ARTIFACT_TYPE,
                artifact_id=_artifact_id(claim.session_id, FORMULA_ARTIFACT_TYPE),
                status="current",
            )
            route = _formula_route_from_record(record) if record is not None else ROUTE_MANUAL_REQUIRED
        await _save_intermediate(claim.id, {"formula_verifier": {"route": route}}, step="verify_formula_consistency")
        return {"route": NODE_REASONING_SUBGRAPH_V1, "reasoning_route": route, "last_error": None}
    finally:
        await db.close()


async def run_reasoning_invalidate_downstream_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        output_state_version, question_message_id, question = await _commit_needs_more_info(
            repository,
            claim,
            trace_id=_node_trace_id(state),
        )
        response = _response_payload(
            session_id=claim.session_id,
            current_stage="inquiry",
            from_stage="syndrome",
            state_version=output_state_version,
            blocked_reason=None,
            trace_id=_node_trace_id(state),
            route=NODE_INTAKE_SUBGRAPH_V1,
            artifact_refs=[],
            gate_results=[
                {
                    "gate_name": "reasoning_needs_more_info",
                    "decision": "blocked",
                    "policy_version": "reasoning-branch-policy.v1",
                }
            ],
            question_message_id=question_message_id,
        )
        await _complete_claim(claim.id, response, output_state_version)
        return {
            "route": NODE_INTAKE_SUBGRAPH_V1,
            "reasoning_route": ROUTE_NEEDS_MORE_INFO,
            "domain_state_version": output_state_version,
            "artifact_refs": [],
            "gate_results": response["gate_results"],
            "last_error": None,
        }
    finally:
        await db.close()


async def run_reasoning_manual_required_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        output_state_version = await _commit_manual_required(repository, claim, trace_id=_node_trace_id(state))
        response = _response_payload(
            session_id=claim.session_id,
            current_stage="blocked",
            from_stage="syndrome",
            state_version=output_state_version,
            blocked_reason="reasoning_manual_required",
            trace_id=_node_trace_id(state),
            route=NODE_REASONING_SUBGRAPH_V1,
            artifact_refs=[],
            gate_results=[
                {
                    "gate_name": "reasoning_manual_required",
                    "decision": "blocked",
                    "policy_version": "reasoning-branch-policy.v1",
                }
            ],
        )
        await _complete_claim(claim.id, response, output_state_version)
        return {
            "route": NODE_REASONING_SUBGRAPH_V1,
            "reasoning_route": ROUTE_MANUAL_REQUIRED,
            "domain_state_version": output_state_version,
            "gate_results": response["gate_results"],
            "last_error": None,
        }
    finally:
        await db.close()


async def run_reasoning_ready_for_safety_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_reasoning_claim(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim = loaded
    try:
        completed = await _completed_graph_update(claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        domain_state = await repository.get_state(claim.session_id)
        formula = await repository.get_artifact_payload(
            claim.session_id,
            artifact_type=FORMULA_ARTIFACT_TYPE,
            artifact_id=_artifact_id(claim.session_id, FORMULA_ARTIFACT_TYPE),
            status="current",
        )
        if formula is None:
            return _sanitized_graph_error(state, "FORMULA_ARTIFACT_MISSING", "formula artifact is missing")
        refs = [
            {"kind": FORMULA_ARTIFACT_TYPE, "artifact_id": str(formula.artifact_id), "revision": formula.revision}
        ]
        response = _response_payload(
            session_id=claim.session_id,
            current_stage="safety",
            from_stage="syndrome",
            state_version=domain_state.state_version,
            blocked_reason=None,
            trace_id=_node_trace_id(state),
            route=NODE_REASONING_SUBGRAPH_V1,
            artifact_refs=refs,
            gate_results=[
                {
                    "gate_name": "ready_for_safety",
                    "decision": "passed",
                    "policy_version": "reasoning-ready-for-safety.v1",
                }
            ],
        )
        await _complete_claim(claim.id, response, domain_state.state_version)
        return {
            "route": NODE_REASONING_SUBGRAPH_V1,
            "reasoning_route": ROUTE_FORMULA_COMPLETED,
            "domain_state_version": domain_state.state_version,
            "artifact_refs": refs,
            "gate_results": response["gate_results"],
            "last_error": None,
        }
    finally:
        await db.close()


async def _load_reasoning_claim(
    state: XuanhuGraphState,
) -> tuple[AsyncSession, IntakeCommandClaim] | dict[str, Any]:
    if state.get("last_error") is not None:
        return {"route": NODE_REASONING_SUBGRAPH_V1, "last_error": state.get("last_error")}
    try:
        session_id = uuid.UUID(state.get("session_id", ""))
    except (TypeError, ValueError):
        return _sanitized_graph_error(state, "REASONING_COMMAND_REF_INVALID", "reasoning command session ref is invalid")
    command_id = state.get("command_id")
    if not command_id:
        return _sanitized_graph_error(state, "REASONING_COMMAND_REF_INVALID", "reasoning command id is missing")
    factory = get_session_factory()
    db = factory()
    await db.__aenter__()
    try:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_id,
            )
        )
        if claim is None:
            await db.__aexit__(None, None, None)
            return _sanitized_graph_error(state, "REASONING_COMMAND_NOT_FOUND", "reasoning command claim was not found")
        return db, claim
    except Exception:
        await db.__aexit__(None, None, None)
        raise


async def _current_authority(
    repository: PostgresDomainRepository,
    session_id: uuid.UUID,
) -> ReasoningAuthoritySnapshot | None:
    state = await repository.get_state(session_id)
    return await repository.get_reasoning_authority(session_id, state.state_version)


def _build_syndrome_input(
    authority: ReasoningAuthoritySnapshot, review_feedback: str | None = None
) -> SyndromeDraftInput:
    policy, _prompt = _reasoning_policy("syndrome")
    return SyndromeDraftInput(
        session_id=authority.session_id,
        state_version=authority.current_state_version,
        current_stage=SYNDROME_READY_STAGE,
        policy_version=policy,
        domain_state=authority.domain_state,
        triage_gate=authority.triage_gate,
        completeness_gate=authority.completeness_gate,
        context_observations=_context_from_domain_state(authority.domain_state),
        review_feedback=review_feedback,
    )


def _build_formula_input(
    authority: ReasoningAuthoritySnapshot, syndrome: SyndromeDraft, review_feedback: str | None = None
) -> FormulaDraftInput:
    policy, _prompt = _reasoning_policy("formula")
    return FormulaDraftInput(
        session_id=authority.session_id,
        state_version=authority.current_state_version,
        current_stage=FORMULA_READY_STAGE,
        policy_version=policy,
        domain_state=authority.domain_state,
        triage_gate=authority.triage_gate,
        completeness_gate=authority.completeness_gate,
        context_observations=_context_from_domain_state(authority.domain_state),
        syndrome_draft=syndrome,
        review_feedback=review_feedback,
    )


def _context_from_domain_state(domain_state: DomainState) -> tuple[SyndromeObservationContext, ...]:
    # 3a 统一口径：与 syndrome_draft/formula_draft 的 _authoritative_input 完全一致
    # （槽位路径投影合成 id，否则裸键）。调用方先 build 的 input 在 preflight 时也会
    # 被权威重建，但「首次 preflight 用调用方输入」这一步也必须在同一口径下成立，
    # 否则 FORMULA_SYNDROME_FACT_LINK_INVALID（合成 syndrome fact_ids ⊄ 裸键集）。
    from app.agents.syndrome_draft import _context_from_domain_state as _authoritative_context

    return _authoritative_context(domain_state)


async def _commit_syndrome_artifact(
    repository: PostgresDomainRepository,
    claim: IntakeCommandClaim,
    result: SyndromeExecutionResult,
    *,
    trace_id: str,
) -> dict[str, Any] | None:
    assert result.output is not None and result.verification is not None
    trusted = _consume_trusted_syndrome_execution(result)
    if trusted is None:
        return None
    payload: dict[str, object] = {
        "kind": SYNDROME_ARTIFACT_TYPE,
        "output": trusted.output.model_dump(mode="json"),
        "input_payload": trusted.input_payload.model_dump(mode="json"),
        "run_spec": trusted.run_spec.model_dump(mode="json"),
        "run_artifact": _run_artifact_payload(trusted.artifact),
        "verification": result.verification.model_dump(mode="json"),
    }
    # D2：完整证据随 payload 落库（v1 记录无此键；恢复路径按记录自身有无该键重建）。
    payload["retrieved_evidence"] = [
        evidence.model_dump(mode="json") for evidence in trusted.retrieved_evidence
    ]
    return await _commit_artifact_payload(
        repository,
        claim,
        artifact_type=SYNDROME_ARTIFACT_TYPE,
        payload_schema_version=SYNDROME_PAYLOAD_SCHEMA_VERSION,
        payload=payload,
        expected_state_version=trusted.input_payload.state_version,
        commit_run_id=trusted.run_spec.run_id,
        trace_id=trace_id,
        idempotency_key=f"{claim.idempotency_key}:syndrome",
        gate_name="syndrome_verifier",
        gate_decision="passed" if result.verification.passed else "failed",
        gate_policy=trusted.run_spec.policy_version,
        session_updates=None,
        outbox_event_type=REASONING_ARTIFACT_COMMITTED,
        outbox_payload_extra={"artifact_type": SYNDROME_ARTIFACT_TYPE, "decision": trusted.output.decision.value},
        agent_runs=(
            _agent_run_spec(
                run_spec=trusted.run_spec,
                agent_name=SYNDROME_AGENT_NAME,
                input_payload=trusted.input_payload,
                output=trusted.output,
                artifact=trusted.artifact,
            ),
        ),
        agent_evidences=_agent_evidence_specs(
            run_id=trusted.run_spec.run_id,
            session_id=trusted.input_payload.session_id,
            retrieved_evidence=trusted.retrieved_evidence,
        ),
    )


async def _commit_formula_artifact(
    repository: PostgresDomainRepository,
    claim: IntakeCommandClaim,
    result: FormulaExecutionResult,
    consistency: FormulaConsistencyReport,
    *,
    trace_id: str,
) -> dict[str, Any] | None:
    assert result.output is not None and result.verification is not None
    trusted = _consume_trusted_formula_execution(result)
    if trusted is None:
        return None
    payload: dict[str, object] = {
        "kind": FORMULA_ARTIFACT_TYPE,
        "output": trusted.output.model_dump(mode="json"),
        "input_payload": trusted.input_payload.model_dump(mode="json"),
        "run_spec": trusted.run_spec.model_dump(mode="json"),
        "run_artifact": _run_artifact_payload(trusted.artifact),
        "verification": result.verification.model_dump(mode="json"),
        "consistency": consistency.model_dump(mode="json"),
    }
    # D2：完整证据随 payload 落库（v1 记录无此键；恢复路径按记录自身有无该键重建）。
    payload["retrieved_evidence"] = [
        evidence.model_dump(mode="json") for evidence in trusted.retrieved_evidence
    ]
    session_updates = None
    outbox_event_type = REASONING_ARTIFACT_COMMITTED
    if trusted.output.decision is FormulaDraftDecision.COMPLETED and consistency.passed:
        # 保留 intake→syndrome 的 advance 出处（后续 reject 回 syndrome 重新
        # 开方时 reasoning 预检依赖 completeness gate 来源门）。
        preserve_advance = await _load_session_advance(claim.session_id)
        session_updates = _session_updates(
            current_stage="safety",
            status="active",
            recovery_status="normal",
            blocked_reason=None,
            trace_id=trace_id,
            output_state_version=trusted.input_payload.state_version + 1,
            route="ready_for_safety",
            preserve_advance=preserve_advance,
        )
        outbox_event_type = REASONING_COMMAND_COMPLETED
    return await _commit_artifact_payload(
        repository,
        claim,
        artifact_type=FORMULA_ARTIFACT_TYPE,
        payload_schema_version=FORMULA_PAYLOAD_SCHEMA_VERSION,
        payload=payload,
        expected_state_version=trusted.input_payload.state_version,
        commit_run_id=trusted.run_spec.run_id,
        trace_id=trace_id,
        idempotency_key=f"{claim.idempotency_key}:formula",
        gate_name="formula_consistency",
        gate_decision="passed" if consistency.passed else "failed",
        gate_policy=FORMULA_CONSISTENCY_POLICY_VERSION,
        session_updates=session_updates,
        outbox_event_type=outbox_event_type,
        outbox_payload_extra={"artifact_type": FORMULA_ARTIFACT_TYPE, "decision": trusted.output.decision.value},
        agent_runs=(
            _agent_run_spec(
                run_spec=trusted.run_spec,
                agent_name=FORMULA_AGENT_NAME,
                input_payload=trusted.input_payload,
                output=trusted.output,
                artifact=trusted.artifact,
            ),
        ),
        agent_evidences=_agent_evidence_specs(
            run_id=trusted.run_spec.run_id,
            session_id=trusted.input_payload.session_id,
            retrieved_evidence=trusted.retrieved_evidence,
        ),
    )


async def _commit_artifact_payload(
    repository: PostgresDomainRepository,
    claim: IntakeCommandClaim,
    *,
    artifact_type: str,
    payload_schema_version: str,
    payload: dict[str, object],
    expected_state_version: int,
    commit_run_id: uuid.UUID,
    trace_id: str,
    idempotency_key: str,
    gate_name: str,
    gate_decision: str,
    gate_policy: str,
    session_updates: dict[str, object] | None,
    outbox_event_type: str,
    outbox_payload_extra: dict[str, object],
    agent_runs: Sequence[AgentRunSpec] = (),
    agent_evidences: Sequence[AgentEvidenceSpec] = (),
) -> dict[str, Any]:
    state = await repository.get_state(claim.session_id)
    if state.state_version != expected_state_version:
        raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)
    artifact_id = _artifact_id(claim.session_id, artifact_type)
    latest = await repository.get_artifact_payload(
        claim.session_id,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        status=None,
    )
    revision = 1 if latest is None else latest.revision + 1
    artifact_revision = ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        revision=revision,
        session_id=claim.session_id,
        input_state_version=expected_state_version,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=commit_run_id,
        parent_revision_id=None if latest is None else latest.artifact_revision_row_id,
        parent_revision=None if latest is None else latest.revision,
        created_at=datetime.now(UTC),
    )
    delta = DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:{commit_run_id}"),
        run_id=commit_run_id,
        session_id=claim.session_id,
        expected_state_version=expected_state_version,
        artifact_revisions=(artifact_revision,),
    )
    digest = artifact_payload_digest(payload_schema_version, payload)
    context = _verification_context(delta=delta, state=state, trace_id=trace_id, idempotency_key=idempotency_key)
    result = await repository.commit(
        delta,
        context,
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(
            _gate_result(gate_name, gate_policy, expected_state_version, gate_decision, {"artifact_digest": digest}),
        ),
        graph_steps=(
            GraphStepSpec(
                step_name=f"commit_{artifact_type}",
                status="completed",
                metadata={"artifact_id": str(artifact_id), "revision": revision, "content_digest": digest},
            ),
        ),
        artifact_payloads=(
            ArtifactPayloadSpec(
                session_id=claim.session_id,
                artifact_id=artifact_id,
                revision=revision,
                payload_schema_version=payload_schema_version,
                payload=payload,
                content_digest=digest,
            ),
        ),
        agent_runs=agent_runs,
        agent_evidences=agent_evidences,
        session_updates=session_updates,
        outbox_event_type=outbox_event_type,
        outbox_payload={
            "session_id": str(claim.session_id),
            "command_id": claim.idempotency_key,
            "artifact_id": str(artifact_id),
            "revision": revision,
            "input_state_version": expected_state_version,
            "output_state_version": expected_state_version + 1,
            "content_digest": digest,
            **outbox_payload_extra,
        },
    )
    return {
        "artifact_id": artifact_id,
        "revision": revision,
        "content_digest": digest,
        "output_state_version": result.output_state_version,
    }


async def _commit_needs_more_info(
    repository: PostgresDomainRepository,
    claim: IntakeCommandClaim,
    *,
    trace_id: str,
) -> tuple[int, uuid.UUID, str]:
    state = await repository.get_state(claim.session_id)
    invalidations = tuple(item.artifact_id for item in state.artifacts if item.status is ArtifactStatus.CURRENT)
    question = _question_from_intermediate(claim, state)
    question_id = uuid.uuid4()
    delta = DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:{_commit_run_id(claim, 'needs_more_info')}"),
        run_id=_commit_run_id(claim, "needs_more_info"),
        session_id=claim.session_id,
        expected_state_version=state.state_version,
        invalidate_artifact_ids=invalidations,
        artifact_revisions=() if invalidations else (_control_artifact(claim, state, "needs_more_info"),),
    )
    context = _verification_context(
        delta=delta,
        state=state,
        trace_id=trace_id,
        idempotency_key=f"{claim.idempotency_key}:needs-more-info",
    )
    session_updates = _session_updates(
        current_stage="inquiry",
        status="active",
        recovery_status="normal",
        blocked_reason=None,
        trace_id=trace_id,
        output_state_version=state.state_version + 1,
        route="needs_more_info",
    )
    result = await repository.commit(
        delta,
        context,
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(_gate_result("reasoning_needs_more_info", "reasoning-branch-policy.v1", state.state_version, "blocked", {}),),
        graph_steps=_reasoning_steps("needs_more_info"),
        artifact_payloads=_control_payloads(claim, delta, state, "needs_more_info") if not invalidations else (),
        consult_messages=(
            ConsultMessageSpec(
                message_id=question_id,
                role="agent",
                stage="inquiry",
                agent_name="question_composer",
                content=question,
                structured_delta={"question": question, "source": "reasoning_needs_more_info"},
                trace_id=trace_id[:64],
            ),
        ),
        session_updates=session_updates,
        outbox_event_type=REASONING_COMMAND_COMPLETED,
        outbox_payload={
            "session_id": str(claim.session_id),
            "command_id": claim.idempotency_key,
            "route": "needs_more_info",
            "question_message_id": str(question_id),
            "invalidated_artifact_ids": [str(item) for item in invalidations],
            "input_state_version": state.state_version,
            "output_state_version": state.state_version + 1,
        },
    )
    return result.output_state_version, question_id, question


async def _commit_manual_required(
    repository: PostgresDomainRepository,
    claim: IntakeCommandClaim,
    *,
    trace_id: str,
) -> int:
    state = await repository.get_state(claim.session_id)
    delta = DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:{_commit_run_id(claim, 'manual_required')}"),
        run_id=_commit_run_id(claim, "manual_required"),
        session_id=claim.session_id,
        expected_state_version=state.state_version,
        artifact_revisions=(_control_artifact(claim, state, "manual_required"),),
    )
    context = _verification_context(
        delta=delta,
        state=state,
        trace_id=trace_id,
        idempotency_key=f"{claim.idempotency_key}:manual",
    )
    result = await repository.commit(
        delta,
        context,
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(_gate_result("reasoning_manual_required", "reasoning-branch-policy.v1", state.state_version, "blocked", {}),),
        graph_steps=_reasoning_steps("manual_required"),
        artifact_payloads=_control_payloads(claim, delta, state, "manual_required"),
        session_updates=_session_updates(
            current_stage="blocked",
            status="blocked",
            recovery_status="manual_required",
            blocked_reason="reasoning_manual_required",
            trace_id=trace_id,
            output_state_version=state.state_version + 1,
            route="manual_required",
        ),
        outbox_event_type=REASONING_COMMAND_COMPLETED,
        outbox_payload={
            "session_id": str(claim.session_id),
            "command_id": claim.idempotency_key,
            "route": "manual_required",
            "input_state_version": state.state_version,
            "output_state_version": state.state_version + 1,
        },
    )
    return result.output_state_version


def _control_artifact(claim: IntakeCommandClaim, state: DomainState, reason: str) -> ArtifactRevisionSchema:
    artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{CONTROL_ARTIFACT_TYPE}:{claim.run_id}:{reason}")
    return ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type=CONTROL_ARTIFACT_TYPE,
        revision=1,
        session_id=claim.session_id,
        input_state_version=state.state_version,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=_commit_run_id(claim, reason),
        created_at=datetime.now(UTC),
    )


def _control_payloads(
    claim: IntakeCommandClaim,
    delta: DomainDelta,
    state: DomainState,
    reason: str,
) -> tuple[ArtifactPayloadSpec, ...]:
    artifact = delta.artifact_revisions[0]
    payload: dict[str, object] = {
        "kind": CONTROL_ARTIFACT_TYPE,
        "reason": reason,
        "input_state_version": state.state_version,
        "command_id_ref": "reasoning",
    }
    return (
        ArtifactPayloadSpec(
            session_id=claim.session_id,
            artifact_id=artifact.artifact_id,
            revision=artifact.revision,
            payload_schema_version=CONTROL_PAYLOAD_SCHEMA_VERSION,
            payload=payload,
            content_digest=artifact_payload_digest(CONTROL_PAYLOAD_SCHEMA_VERSION, payload),
        ),
    )


def _verification_context(
    *,
    delta: DomainDelta,
    state: DomainState,
    trace_id: str,
    idempotency_key: str,
) -> VerificationContext:
    agent_spec = AgentSpec(
        name="reasoning_domain_delta",
        version="reasoning-domain-delta.v1",
        input_schema=_EmptyOutput,
        output_schema=DomainDelta,
        model_policy=ModelPolicy(model="deterministic-reducer", max_attempts=1),
        tool_permissions=frozenset({Capability.READ_STATE}),
        verifier_chain=tuple(verifier.name.value for verifier in DEFAULT_VERIFIER_CHAIN.verifiers),
        failure_policy=FailurePolicy(),
    )
    run_spec = RunSpec(
        run_id=delta.run_id,
        session_id=delta.session_id,
        state_version=delta.expected_state_version,
        stage="reasoning_reduce",
        agent_spec_version=agent_spec.version,
        prompt_version="reasoning-domain-delta.v1",
        policy_version="reasoning-domain-delta-policy.v1",
        deadline_at=_deadline(30),
        total_attempt_budget=1,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    artifact = RunArtifact(
        output=delta,
        model_actual="deterministic-reducer",
        attempts=1,
        latency_ms=0,
        trace_id=trace_id,
        run_id=delta.run_id,
        agent_spec_version=agent_spec.version,
        prompt_version=run_spec.prompt_version,
    )
    return VerificationContext(
        agent_spec=agent_spec,
        run_spec=run_spec,
        artifact=artifact,
        state=state,
        allowed_source_message_ids=frozenset(delta.source_message_ids),
        allowed_stages=frozenset({"reasoning_reduce"}),
    )


def _gate_result(name: str, policy: str, state_version: int, decision: str, details: dict[str, object]) -> Any:
    from app.schemas.domain import GateDecision, GateResultSchema

    return GateResultSchema(
        gate_name=name,
        policy_version=policy,
        input_state_version=state_version,
        decision=GateDecision(decision),
        details=details,
    )


def _run_artifact_payload(artifact: RunArtifact) -> dict[str, object]:
    return {
        "output": artifact.output.model_dump(mode="json"),
        "model_actual": artifact.model_actual,
        "attempts": artifact.attempts,
        "latency_ms": artifact.latency_ms,
        "trace_id": artifact.trace_id,
        "run_id": str(artifact.run_id),
        "agent_spec_version": artifact.agent_spec_version,
        "prompt_version": artifact.prompt_version,
        "usage": artifact.usage.model_dump(mode="json"),
        "evidence_ids": list(artifact.evidence_ids),
    }


async def _load_trusted_syndrome_result(
    repository: PostgresDomainRepository,
    session_id: uuid.UUID,
    authority: ReasoningAuthoritySnapshot | None,
) -> SyndromeExecutionResult | None:
    if authority is None:
        return None
    try:
        record = await repository.get_artifact_payload(
            session_id,
            artifact_type=SYNDROME_ARTIFACT_TYPE,
            artifact_id=_artifact_id(session_id, SYNDROME_ARTIFACT_TYPE),
            status="current",
        )
    except RepositoryError:
        return None
    if record is None:
        return None
    return await recover_trusted_syndrome_from_repository(
        session_id=session_id,
        artifact_id=record.artifact_id,
        revision=record.revision,
        expected_content_digest=record.content_digest,
    )


def _formula_route_from_record(record: ArtifactPayloadRecord | None) -> str:
    if record is None:
        return ROUTE_MANUAL_REQUIRED
    try:
        output = FormulaDraft.model_validate(record.payload["output"])
        input_payload = FormulaDraftInput.model_validate(record.payload["input_payload"])
        run_spec = RunSpec.model_validate(record.payload["run_spec"])
        artifact = _run_artifact_from_payload(cast(dict[str, Any], record.payload["run_artifact"]), output)
        verification = FormulaVerificationReport.model_validate(record.payload["verification"])
        consistency = FormulaConsistencyReport.model_validate(record.payload["consistency"])
    except (KeyError, TypeError, ValueError):
        return ROUTE_MANUAL_REQUIRED
    canonical_payload: dict[str, object] = {
        "kind": FORMULA_ARTIFACT_TYPE,
        "output": output.model_dump(mode="json"),
        "input_payload": input_payload.model_dump(mode="json"),
        "run_spec": run_spec.model_dump(mode="json"),
        "run_artifact": _run_artifact_payload(artifact),
        "verification": verification.model_dump(mode="json"),
        "consistency": consistency.model_dump(mode="json"),
    }
    # D2：v1 记录无 retrieved_evidence 键；有键时重建必须原样保留（route 判定
    # 用，证据内容已由 content_digest 防篡改）。
    if "retrieved_evidence" in record.payload:
        canonical_payload["retrieved_evidence"] = record.payload["retrieved_evidence"]
    if (
        record.artifact_type != FORMULA_ARTIFACT_TYPE
        or record.status != "current"
        or record.payload_schema_version != FORMULA_PAYLOAD_SCHEMA_VERSION
        or record.input_state_version != input_payload.state_version
        or record.input_state_version != run_spec.state_version
        or record.produced_by_run_id != run_spec.run_id
        or run_spec.session_id != record.session_id
        or input_payload.session_id != record.session_id
        or artifact.run_id != run_spec.run_id
        or artifact.trace_id != run_spec.trace_id
        or artifact.agent_spec_version != FORMULA_AGENT_VERSION
        or (run_spec.policy_version, artifact.prompt_version) not in FORMULA_POLICY_PROMPT_PAIRS
        or artifact.output != output
        or not verification.passed
        or verification.subject_digest != run_artifact_subject_digest(artifact)
        or record.payload != canonical_payload
        or artifact_payload_digest(FORMULA_PAYLOAD_SCHEMA_VERSION, canonical_payload) != record.content_digest
    ):
        return ROUTE_MANUAL_REQUIRED
    if output.decision is FormulaDraftDecision.COMPLETED:
        return ROUTE_FORMULA_COMPLETED
    if output.decision is FormulaDraftDecision.NEEDS_MORE_INFO:
        return ROUTE_NEEDS_MORE_INFO if _formula_missing_dimension(output) is not None else ROUTE_MANUAL_REQUIRED
    return ROUTE_MANUAL_REQUIRED


def _run_artifact_from_payload(payload: dict[str, Any], output: BaseModel) -> RunArtifact:
    if payload.get("output") != output.model_dump(mode="json"):
        raise ValueError("run artifact output mismatch")
    return RunArtifact(
        output=output,
        model_actual=None if payload["model_actual"] is None else str(payload["model_actual"]),
        attempts=int(payload["attempts"]),
        latency_ms=int(payload["latency_ms"]),
        usage=TokenUsage.model_validate(payload["usage"]),
        evidence_ids=tuple(str(item) for item in payload["evidence_ids"]),
        trace_id=str(payload["trace_id"]),
        run_id=uuid.UUID(str(payload["run_id"])),
        agent_spec_version=str(payload["agent_spec_version"]),
        prompt_version=str(payload["prompt_version"]),
    )


def _formula_missing_dimension(output: FormulaDraft) -> str | None:
    if not output.missing_inputs:
        return None
    from app.schemas.completeness import InquiryDimension

    keyword_map: tuple[tuple[InquiryDimension, tuple[str, ...]], ...] = (
        (InquiryDimension.TEN_SLEEP, ("sleep", "睡眠", "入睡", "失眠")),
        (InquiryDimension.TEN_STOOL_URINE, ("stool", "urine", "二便", "大便", "小便", "排便", "排尿")),
        (InquiryDimension.TEN_DIET, ("diet", "appetite", "饮食", "食欲", "胃口")),
        (InquiryDimension.TEN_COLD_HEAT, ("cold", "heat", "chill", "fever", "寒热", "怕冷", "发热", "恶寒")),
        (InquiryDimension.TEN_PAIN, ("pain", "疼痛", "痛")),
        (InquiryDimension.TEN_CHEST_ABDOMEN, ("chest", "abdomen", "胸", "腹")),
        (InquiryDimension.TEN_THIRST, ("thirst", "口渴", "饮水")),
        (InquiryDimension.TEN_RESPIRATORY, ("respiratory", "cough", "breath", "呼吸", "咳", "痰")),
        (InquiryDimension.TEN_MENSES_LEUKORRHEA, ("menses", "menstru", "leukorrhea", "月经", "经带", "白带")),
        (InquiryDimension.TEN_SWEAT, ("sweat", "汗", "出汗")),
        (InquiryDimension.TEN_HEAD_BODY, ("head", "body", "头身", "头痛", "身痛")),
        (InquiryDimension.ALLERGY_STATUS, ("allergy", "过敏")),
        (InquiryDimension.PREGNANCY_STATUS, ("pregnan", "妊娠", "怀孕", "孕")),
        (InquiryDimension.LACTATION_STATUS, ("lactat", "哺乳")),
        (InquiryDimension.MEDICATION_STATUS, ("medication", "medicine", "drug", "用药", "药物")),
        (InquiryDimension.MAJOR_CONDITION_STATUS, ("major", "condition", "重大疾病", "基础病")),
    )
    for raw in output.missing_inputs:
        text = str(raw).strip()
        if not text:
            continue
        try:
            return InquiryDimension(text).value
        except ValueError:
            lowered = text.lower()
            for dimension, keywords in keyword_map:
                if any(keyword in lowered or keyword in text for keyword in keywords):
                    return dimension.value
    return None


def _route_from_intermediate(claim: IntakeCommandClaim, section: str) -> str | None:
    payload = claim.intermediate_payload if isinstance(claim.intermediate_payload, dict) else {}
    raw = payload.get(f"{section}_verifier")
    route = raw.get("route") if isinstance(raw, dict) else None
    if isinstance(route, str):
        return route
    return None


def _question_from_intermediate(claim: IntakeCommandClaim, state: DomainState) -> str:
    payload = claim.intermediate_payload if isinstance(claim.intermediate_payload, dict) else {}
    syndrome = payload.get("syndrome")
    formula = payload.get("formula")
    model_missing = None
    if isinstance(formula, dict) and isinstance(formula.get("missing_dimension"), str):
        model_missing = formula["missing_dimension"]
    if model_missing is None and isinstance(syndrome, dict) and isinstance(syndrome.get("missing_dimension"), str):
        model_missing = syndrome["missing_dimension"]
    # 2.8 确定性缺失判定：模型报的 missing_dimension 可能与 context 中已存在的事实冲突
    # （REAL-SESSION b801423b：模型误报 ten_questions.cold_heat/stool_urine 缺失，实际已在
    # 观察表中）。只对真正缺失的维度生成回退问句，避免“已采集却被反复追问”的死循环。
    true_missing = _true_missing_dimensions(state, model_missing)
    dimension = true_missing[0] if true_missing else None
    question = _question_for_dimension(dimension)
    if validate_single_question_text(question) is not None:
        return "请补充一个关键问诊信息？"
    return question


def _true_missing_dimensions(state: DomainState, model_missing: str | None) -> list[str]:
    """2.8：确定性计算真实缺失的辨证维度（过滤模型误报）。

    - four_diagnosis（舌脉）只要无 inspection/palpation 事实即视为缺失；
    - 模型报的 missing_dimension 若其 keyset 在活跃观察中已命中，则视为误报剔除。
    """
    from app.agent_runtime.intake_dimension_mapping import DIMENSION_KEYSETS
    from app.schemas.completeness import InquiryDimension

    active_keys = {item.fact_key for item in _current_observations(state.observations)}
    missing: list[str] = []
    if not any(k in active_keys for k in ("four_diagnosis.inspection", "four_diagnosis.palpation")):
        missing.append(InquiryDimension.FOUR_DIAGNOSIS.value)
    if model_missing:
        try:
            dimension = InquiryDimension(model_missing)
        except ValueError:
            dimension = None
        if dimension is not None and dimension.value not in missing:
            keyset = DIMENSION_KEYSETS.get(dimension, (dimension.value,))
            if not any(k in active_keys for k in keyset):
                missing.append(dimension.value)
    return missing


def _question_for_dimension(raw_dimension: object) -> str:
    from app.schemas.completeness import InquiryDimension

    dimension = None
    if isinstance(raw_dimension, str):
        try:
            dimension = InquiryDimension(raw_dimension)
        except ValueError:
            dimension = None
    if dimension is None:
        return "请补充一个关键问诊信息？"
    # 2.8 舌脉采集：四诊为 optional 维度，question_composer 不主动问；
    # 辨证需要时由 reasoning 回退引导用户直接输入舌脉描述。
    if dimension is InquiryDimension.FOUR_DIAGNOSIS:
        return "请提供患者舌脉情况：舌象（舌色、舌形、舌苔）与脉象？"
    template = QUESTION_TEMPLATES.get((dimension, GapSelectionKind.REQUIRED))
    return template.question if template is not None else "请补充一个关键问诊信息？"


async def _load_review_feedback(session_id: uuid.UUID) -> str | None:
    """读取医师否决反馈（reject 时写入 state_snapshot.review_feedback）。

    重新辨证/开方时注入模型输入；无反馈（首次开方）返回 None。
    """
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(ConsultSession, session_id)
        if row is None or not isinstance(row.state_snapshot, dict):
            return None
        feedback = row.state_snapshot.get("review_feedback")
        return feedback if isinstance(feedback, str) and feedback else None


async def _load_session_advance(session_id: uuid.UUID) -> dict[str, Any] | None:
    """读取会话当前 state_snapshot 中的 advance 出处（intake completeness gate）。

    reasoning 子图（syndrome→safety）重建 snapshot 时必须原样保留该出处，
    否则医生 reject 回 syndrome 后重新 advance 时 reasoning 预检找不到来源门
    → REASONING_PRECHECK_FAILED，被否决的方子无法重新开方。
    """
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(ConsultSession, session_id)
        if row is None or not isinstance(row.state_snapshot, dict):
            return None
        advance = row.state_snapshot.get("advance")
        return advance if isinstance(advance, dict) else None


def _session_updates(
    *,
    current_stage: str,
    status: str,
    recovery_status: str,
    blocked_reason: str | None,
    trace_id: str,
    output_state_version: int,
    route: str,
    preserve_advance: dict[str, Any] | None = None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "agent_runtime": "langgraph",
        "current_stage": current_stage,
        "state_version": output_state_version,
        "recovery_status": recovery_status,
        "blocked_reason": blocked_reason,
        "langgraph_reasoning": {
            "version": "reasoning-subgraph.v1",
            "route": route,
            "ready_for_safety": route == "ready_for_safety",
            "review_required": True,
            "safety_executed": False,
            "trace_id": trace_id,
        },
    }
    if preserve_advance is not None:
        snapshot["advance"] = preserve_advance
    return {
        "current_stage": current_stage,
        "status": status,
        "recovery_status": recovery_status,
        "blocked_reason": blocked_reason,
        "blocked_at": datetime.now(UTC).replace(tzinfo=None) if status == "blocked" else None,
        "state_snapshot": snapshot,
    }


def _reasoning_steps(route: str) -> tuple[GraphStepSpec, ...]:
    return tuple(
        GraphStepSpec(step_name=name, status="completed", metadata={})
        for name in (
            "reasoning_precheck",
            "build_syndrome_context",
            "draft_syndrome",
            "verify_syndrome",
            f"route:{route}",
        )
    )


def _response_payload(
    *,
    session_id: uuid.UUID,
    current_stage: str,
    from_stage: str,
    state_version: int,
    blocked_reason: str | None,
    trace_id: str,
    route: str,
    artifact_refs: list[dict[str, object]],
    gate_results: list[dict[str, object]],
    question_message_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": str(session_id),
        "current_stage": current_stage,
        "from_stage": from_stage,
        "state_version": state_version,
        "blocked_reason": blocked_reason,
        "agent_name": "reasoning_subgraph",
        "trace_id": trace_id,
        "route": route,
        "artifact_refs": artifact_refs,
        "gate_results": gate_results,
        "review_required": True,
        "safety_executed": False,
    }
    if question_message_id is not None:
        payload["question_message_id"] = str(question_message_id)
    return payload


async def _complete_claim(claim_id: uuid.UUID, response_payload: dict[str, Any], output_state_version: int) -> None:
    try:
        factory = get_session_factory()
        async with factory() as db:
            if db.in_transaction():
                await db.rollback()
            async with db.begin():
                claim = await db.get(IntakeCommandClaim, claim_id, with_for_update=True)
                if claim is None:
                    return
                claim.status = "completed"
                claim.output_state_version = output_state_version
                claim.response_payload = response_payload
                claim.updated_at = func.now()
                graph_run = await db.get(GraphRun, claim.run_id)
                if graph_run is not None:
                    graph_run.status = "completed"
                    graph_run.completed_at = func.now()
    finally:
        _SYNDROME_RESULT_CACHE.pop(claim_id, None)
        _FORMULA_ROUTE_CACHE.pop(claim_id, None)


async def _mark_claim_failed(claim_id: uuid.UUID, error_code: str) -> None:
    try:
        factory = get_session_factory()
        async with factory() as db:
            if db.in_transaction():
                await db.rollback()
            async with db.begin():
                claim = await db.get(IntakeCommandClaim, claim_id, with_for_update=True)
                if claim is not None and claim.status != "completed":
                    claim.status = "failed"
                    claim.error_code = error_code[:64]
                    claim.updated_at = func.now()
    finally:
        _SYNDROME_RESULT_CACHE.pop(claim_id, None)
        _FORMULA_ROUTE_CACHE.pop(claim_id, None)


async def _completed_graph_update(claim: IntakeCommandClaim) -> dict[str, Any] | None:
    if claim.status == "completed" and claim.response_payload is not None:
        _SYNDROME_RESULT_CACHE.pop(claim.id, None)
        _FORMULA_ROUTE_CACHE.pop(claim.id, None)
        return _graph_update_from_response(dict(claim.response_payload))
    return None


def _graph_update_from_response(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": response.get("route", NODE_REASONING_SUBGRAPH_V1),
        "reasoning_route": (
            ROUTE_FORMULA_COMPLETED
            if response.get("current_stage") == "safety"
            else ROUTE_NEEDS_MORE_INFO
            if response.get("current_stage") == "inquiry"
            else ROUTE_MANUAL_REQUIRED
        ),
        "domain_state_version": response.get("state_version", 0),
        "artifact_refs": response.get("artifact_refs", []),
        "gate_results": response.get("gate_results", []),
        "last_error": None,
    }


def _syndrome_graph_update(
    claim: IntakeCommandClaim,
    result: SyndromeExecutionResult,
    state: DomainState,
) -> dict[str, Any]:
    refs: list[ArtifactRef] = []
    if result.output is not None:
        refs.append(
            {
                "kind": SYNDROME_ARTIFACT_TYPE,
                "artifact_id": str(_artifact_id(claim.session_id, SYNDROME_ARTIFACT_TYPE)),
                "revision": max(
                    (item.revision for item in state.artifacts if item.artifact_type == SYNDROME_ARTIFACT_TYPE),
                    default=1,
                ),
            }
        )
    return {
        "route": NODE_REASONING_SUBGRAPH_V1,
        "domain_state_version": state.state_version,
        "artifact_refs": refs,
        "last_error": None,
    }


def _formula_graph_update(record: ArtifactPayloadRecord, route: str) -> dict[str, Any]:
    return {
        "route": NODE_REASONING_SUBGRAPH_V1,
        "reasoning_route": route,
        "domain_state_version": record.input_state_version + 1,
        "artifact_refs": [{"kind": FORMULA_ARTIFACT_TYPE, "artifact_id": str(record.artifact_id), "revision": record.revision}],
        "last_error": None,
    }


def _formula_graph_update_from_commit(claim: IntakeCommandClaim, route: str, commit: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": NODE_REASONING_SUBGRAPH_V1,
        "reasoning_route": route,
        "domain_state_version": commit["output_state_version"],
        "artifact_refs": [
            {
                "kind": FORMULA_ARTIFACT_TYPE,
                "artifact_id": str(_artifact_id(claim.session_id, FORMULA_ARTIFACT_TYPE)),
                "revision": commit["revision"],
            }
        ],
        "last_error": None,
    }


async def _save_intermediate_step(claim_id: uuid.UUID, step: str) -> None:
    await _save_intermediate(claim_id, {}, step=step)


async def _save_intermediate(claim_id: uuid.UUID, patch: dict[str, Any], *, step: str) -> None:
    factory = get_session_factory()
    async with factory() as db:
        if db.in_transaction():
            await db.rollback()
        async with db.begin():
            claim = await db.get(IntakeCommandClaim, claim_id, with_for_update=True)
            if claim is None or claim.status == "completed":
                return
            payload = dict(claim.intermediate_payload or {})
            steps = dict(payload.get("steps") or {})
            steps[step] = "completed"
            payload["steps"] = steps
            payload.update(patch)
            claim.intermediate_payload = payload
            claim.updated_at = func.now()


def _sanitized_graph_error(state: XuanhuGraphState, code: str, detail: str) -> dict[str, Any]:
    return {
        "route": NODE_REASONING_SUBGRAPH_V1,
        "reasoning_route": ROUTE_MANUAL_REQUIRED,
        "last_error": {
            "code": code,
            "trace_id": state.get("run_id", ""),
            "detail": detail,
        },
    }
