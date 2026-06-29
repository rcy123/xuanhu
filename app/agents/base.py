"""Agent 统一执行骨架。"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.errors import AgentRunError
from app.agents.prompt_loader import PromptLoader, PromptTemplate
from app.core.config import get_settings
from app.core.exceptions import (
    ChatStructuredParseError,
    ModelGatewayTimeoutError,
    ModelGatewayUnavailableError,
)
from app.core.gateway import ModelGatewayClient
from app.models.agent import AgentRun
from app.models.audit import AuditEvent
from app.rag.schemas import Evidence
from app.schemas.agent import XuanhuState
from app.schemas.types import Stage


class AgentResult(BaseModel):
    """Agent 执行结果。"""

    output: BaseModel
    messages: list[dict[str, Any]] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)
    next_stage: Stage | None = None
    agent_run_id: str | None = None
    prompt_version: str


class BaseAgent(Protocol):
    """Agent 统一协议。"""

    name: str
    stage: Stage
    primary_sources: Sequence[str]
    allow_cross_source: bool
    output_schema: type[BaseModel]

    async def run(self, state: XuanhuState, trace_id: str) -> AgentResult:
        """执行 Agent，返回结构化结果。"""
        ...


class StructuredGateway(Protocol):
    """BaseAgentImpl 依赖的模型网关最小协议，方便测试注入 fake gateway。"""

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> BaseModel | dict[str, Any]:
        """返回结构化输出。"""
        ...


class BaseAgentImpl(ABC):
    """Agent 基类实现。

    只封装公共基础设施：Evidence 获取钩子、Prompt 版本加载、模型调用、
    schema 校验、重试、agent_runs 记录和 audit_events 审计。
    """

    name: str
    stage: Stage
    primary_sources: Sequence[str] = ()
    allow_cross_source: bool = True
    output_schema: type[BaseModel]
    next_stage: Stage | None = None

    def __init__(
        self,
        *,
        gateway: StructuredGateway | None = None,
        db: AsyncSession | None = None,
        prompt_loader: PromptLoader | None = None,
        max_retries: int | None = None,
        model_name: str | None = None,
    ) -> None:
        settings = get_settings()
        self.gateway = gateway or ModelGatewayClient(settings)
        self.db = db
        self.prompt_loader = prompt_loader or PromptLoader(settings.prompt_manifest_path)
        self.max_retries = settings.agent_max_retries if max_retries is None else max_retries
        self.model_name = model_name or settings.chat_model
        self._active_prompt_template: PromptTemplate | None = None

    @property
    def prompt_template(self) -> PromptTemplate:
        """当前 run 加载的 Prompt 模板。"""
        if self._active_prompt_template is None:
            raise AgentRunError(
                "Prompt 尚未加载",
                code="PROMPT_NOT_LOADED",
                retryable=False,
            )
        return self._active_prompt_template

    async def run(self, state: XuanhuState, trace_id: str) -> AgentResult:
        """执行 Agent 公共流程。"""
        started_at = time.perf_counter()
        prompt_template = self.prompt_loader.load(self.name)
        self._active_prompt_template = prompt_template
        agent_run_id: uuid.UUID | None = None
        evidences: list[Evidence] = []
        messages: list[dict[str, Any]] = []
        retry_count = 0
        try:
            await self._write_audit_event(
                state,
                "agent.started",
                {
                    "agent_name": self.name,
                    "stage": self.stage.value,
                    "prompt_version": prompt_template.prompt_version,
                },
                trace_id,
            )
            evidences = await self._retrieve_evidence(state, trace_id)
            messages = await self._build_prompt(state, evidences)
            output, retry_count = await self._call_with_retries(state, messages, trace_id)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            agent_run_id = await self._write_agent_run(
                state,
                status="success",
                prompt_version=prompt_template.prompt_version,
                trace_id=trace_id,
                latency_ms=latency_ms,
                input_snapshot=self._input_snapshot(state, evidences),
                output_snapshot=output.model_dump(mode="json"),
                retry_count=retry_count,
            )
            await self._write_audit_event(
                state,
                "agent.finished",
                {
                    "agent_name": self.name,
                    "stage": self.stage.value,
                    "agent_run_id": str(agent_run_id) if agent_run_id else None,
                    "status": "success",
                    "prompt_version": prompt_template.prompt_version,
                    "latency_ms": latency_ms,
                },
                trace_id,
            )
            return AgentResult(
                output=output,
                messages=messages,
                evidences=evidences,
                next_stage=self.next_stage,
                agent_run_id=str(agent_run_id) if agent_run_id else None,
                prompt_version=prompt_template.prompt_version,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            error = self._to_agent_error(exc)
            retry_count = error.retry_count
            agent_run_id = await self._write_agent_run(
                state,
                status="failed",
                prompt_version=prompt_template.prompt_version,
                trace_id=trace_id,
                latency_ms=latency_ms,
                input_snapshot=self._input_snapshot(state, evidences),
                output_snapshot={"error_code": error.code, "retryable": error.retryable},
                retry_count=retry_count,
                error_code=error.code,
            )
            await self._write_audit_event(
                state,
                "agent.failed",
                {
                    "agent_name": self.name,
                    "stage": self.stage.value,
                    "agent_run_id": str(agent_run_id) if agent_run_id else None,
                    "error_code": error.code,
                    "retryable": error.retryable,
                    "prompt_version": prompt_template.prompt_version,
                    "latency_ms": latency_ms,
                },
                trace_id,
            )
            raise error from exc
        finally:
            self._active_prompt_template = None

    async def _retrieve_evidence(self, state: XuanhuState, trace_id: str) -> list[Evidence]:
        """检索证据钩子。P4-2 默认不调用 RAG。"""
        del state, trace_id
        return []

    @abstractmethod
    async def _build_prompt(
        self,
        state: XuanhuState,
        evidences: list[Evidence],
    ) -> list[dict[str, Any]]:
        """组装 OpenAI messages。"""

    async def _call_with_retries(
        self,
        state: XuanhuState,
        messages: list[dict[str, Any]],
        trace_id: str,
    ) -> tuple[BaseModel, int]:
        attempts = max(self.max_retries, 0) + 1
        last_error: Exception | None = None
        actual_attempts = 0
        for _attempt in range(attempts):
            actual_attempts += 1
            try:
                raw = await self.gateway.chat_structured(
                    messages,
                    self.output_schema,
                    trace_id=trace_id,
                    session_id=state.session_id,
                    agent_name=self.name,
                )
                return self._validate_output(raw), actual_attempts - 1
            except (
                ValidationError,
                ChatStructuredParseError,
                ModelGatewayTimeoutError,
                ModelGatewayUnavailableError,
            ) as exc:
                last_error = exc
                if not self._is_retryable(exc):
                    break
        if last_error is None:
            raise AgentRunError("Agent 执行失败", code="AGENT_FAILED", retryable=True)
        error = self._to_agent_error(last_error)
        error.retry_count = max(actual_attempts - 1, 0)
        raise error from last_error

    def _validate_output(self, raw: BaseModel | dict[str, Any]) -> BaseModel:
        if isinstance(raw, self.output_schema):
            return raw
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(mode="python")
        return self.output_schema.model_validate(raw)

    def _to_agent_error(self, exc: Exception) -> AgentRunError:
        if isinstance(exc, AgentRunError):
            return exc
        if isinstance(exc, ValidationError | ChatStructuredParseError):
            return AgentRunError(
                "Agent 结构化输出校验失败",
                code="AGENT_SCHEMA_INVALID",
                retryable=False,
                detail=type(exc).__name__,
            )
        if isinstance(exc, ModelGatewayTimeoutError):
            return AgentRunError(
                "Agent 模型调用超时",
                code="AGENT_MODEL_TIMEOUT",
                retryable=True,
                detail=type(exc).__name__,
            )
        if isinstance(exc, ModelGatewayUnavailableError):
            return AgentRunError(
                "Agent 模型网关不可用",
                code="AGENT_MODEL_UNAVAILABLE",
                retryable=exc.retryable,
                detail=type(exc).__name__,
            )
        return AgentRunError(
            "Agent 执行失败",
            code="AGENT_FAILED",
            retryable=True,
            detail=type(exc).__name__,
        )

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, ValidationError | ChatStructuredParseError | ModelGatewayTimeoutError):
            return True
        if isinstance(exc, ModelGatewayUnavailableError):
            return exc.retryable
        return False

    async def _write_agent_run(
        self,
        state: XuanhuState,
        *,
        status: str,
        prompt_version: str,
        trace_id: str,
        latency_ms: int,
        input_snapshot: dict[str, Any],
        output_snapshot: dict[str, Any],
        retry_count: int,
        error_code: str | None = None,
    ) -> uuid.UUID | None:
        if self.db is None:
            return None
        run = AgentRun(
            session_id=uuid.UUID(state.session_id),
            agent_name=self.name,
            stage=self.stage.value,
            input_snapshot=input_snapshot,
            output_snapshot=output_snapshot,
            prompt_version=prompt_version,
            model=self.model_name,
            retry_count=retry_count,
            status=status,
            error_code=error_code,
            latency_ms=latency_ms,
            trace_id=trace_id,
        )
        self.db.add(run)
        await self.db.flush()
        return run.id

    async def _write_audit_event(
        self,
        state: XuanhuState,
        event_type: str,
        payload: dict[str, Any],
        trace_id: str,
    ) -> None:
        if self.db is None:
            return
        event = AuditEvent(
            session_id=uuid.UUID(state.session_id),
            event_type=event_type,
            actor_type="agent",
            actor_id=self.name,
            payload=payload,
            trace_id=trace_id,
        )
        self.db.add(event)
        await self.db.flush()

    def _input_snapshot(self, state: XuanhuState, evidences: list[Evidence]) -> dict[str, Any]:
        return {
            "session_id": state.session_id,
            "current_stage": state.current_stage,
            "state_version": state.state_version,
            "evidence_count": len(evidences),
        }
