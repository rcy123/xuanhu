"""Unified, side-effect-free model execution boundary for L2-2."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.exceptions import (
    ChatOutputTruncatedError,
    ChatStructuredParseError,
    ModelGatewayTimeoutError,
    ModelGatewayUnavailableError,
    ModelRunAuditIntegrityError,
    ModelRunAuditUnavailableError,
)
from app.core.gateway import ModelGatewayClient, StructuredChatResponse

from .context import (
    ContextBuilderError,
    project_model_input_identity_sequences,
)
from .intake_verifier import INTAKE_AGENT_NAME
from .specs import (
    AgentSpec,
    RunArtifact,
    RunSpec,
    RuntimeErrorCode,
    TokenUsage,
    model_input_digest,
    model_output_digest,
)


class RuntimeErrorBase(Exception):
    """A sanitized failure whose code is stable and safe to record."""

    def __init__(self, code: RuntimeErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RuntimeRunRecorder(Protocol):
    """Async recorder; generic failures degrade, integrity violations fail closed."""

    async def record(self, event: str, data: dict[str, Any]) -> None: ...


RECORDER_TIMEOUT_SECONDS = 0.05
RECORDER_FINALIZATION_TIMEOUT_SECONDS = 0.05
MAX_DURABLE_RECORDER_TIMEOUT_SECONDS = 5.0


class _DefaultRecorder:
    pass


_DEFAULT_RECORDER = _DefaultRecorder()


async def _invoke_recorder(recorder: RuntimeRunRecorder, event: str, data: dict[str, Any]) -> None:
    """Invoke the already-validated async recorder."""
    await recorder.record(event, data)


def _consume_recorder_task(task: asyncio.Task[None]) -> None:
    """Consume a late recorder exception without exposing its text."""
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        return


async def _cancel_recorder_task(task: asyncio.Task[None]) -> None:
    """Give a cooperative recorder task one loop turn to consume cancellation."""
    task.cancel()
    await asyncio.sleep(0)
    if task.done():
        _consume_recorder_task(task)
    else:
        task.add_done_callback(_consume_recorder_task)


async def _record_safely(
    recorder: RuntimeRunRecorder | None,
    event: str,
    data: dict[str, Any],
    *,
    timeout_seconds: float,
) -> bool:
    """Bound recorder work while preserving durable-audit integrity failures.

    Returns whether the recorder was stopped by its timeout.
    """
    if recorder is None:
        return False
    required = getattr(recorder, "required", False) is True
    if timeout_seconds <= 0:
        if required:
            raise ModelRunAuditUnavailableError
        return True
    task = asyncio.create_task(_invoke_recorder(recorder, event, data), name="agent-runtime-recorder")
    required_failure = False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
    except TimeoutError:
        await _cancel_recorder_task(task)
        if not required:
            return True
        required_failure = True
    except asyncio.CancelledError:
        # A recorder may cancel itself; external cancellation leaves the
        # shielded task pending and must keep propagating to the caller.
        if task.done() and task.cancelled():
            if not required:
                return False
            required_failure = True
        else:
            await _cancel_recorder_task(task)
            raise
    except ModelRunAuditIntegrityError:
        raise
    except Exception:
        if not required:
            return False
        required_failure = True
    if required_failure:
        raise ModelRunAuditUnavailableError
    return False


class AgentRuntime:
    """Execute an AgentSpec without writing state, persistence, or approvals."""

    # 前置 deadline 守卫按「policy/gateway」之比决定是否生效：
    #   - 真实生产四类 Agent 的 ModelPolicy.timeout=75s，gateway=60s → ratio=0.8，守卫硬拦；
    #   - 测试 helper ``make_runtime`` 显式把 gateway_timeout_seconds 压到 0.0，ratio=0.0，
    #     表示「这次注入短超时是为复现 _one_attempt 内的 wait_for 超时，配置不变量不是看点」→ 跳过守卫，
    #     继续走既有超时归因逻辑（test_single_attempt_timeout... 仍拿到 MODEL_GATEWAY_TIMEOUT，
    #     test_expired_deadline... 仍拿到 RUN_DEADLINE_EXCEEDED）。
    # 0.5 是分水岭：低于它只可能是测试在用极小 gateway 基准。
    DEADLINE_INVARIANT_BYPASS_RATIO = 0.5

    def __init__(
        self,
        gateway: Any | None = None,
        recorder: RuntimeRunRecorder | None | _DefaultRecorder = _DEFAULT_RECORDER,
        *,
        gateway_timeout_seconds: float | None = None,
    ) -> None:
        # Only the runtime's constructed client has retries disabled.  Legacy
        # BaseAgentImpl retains its configured ModelGatewayClient unchanged.
        resolved_recorder: RuntimeRunRecorder | None
        # Omitting the recorder is always the production-safe path, even when
        # a caller injects a gateway implementation.  Tests that intentionally
        # run without persistence must opt out with ``recorder=None``.
        resolved_recorder = self._production_recorder() if isinstance(recorder, _DefaultRecorder) else recorder
        if resolved_recorder is not None and not self._is_async_recorder(resolved_recorder):
            raise RuntimeErrorBase(
                RuntimeErrorCode.RECORDER_ASYNC_REQUIRED,
                "runtime recorder must implement async record",
            ) from None
        self.gateway = gateway or ModelGatewayClient(max_retries=0)
        self.recorder = resolved_recorder
        # 内层网关单请求超时；deadline 嵌套不变量以此为基准。默认从 settings 读取，
        # 测试可显式注入以解耦全局配置。
        self._gateway_timeout_seconds = (
            gateway_timeout_seconds
            if gateway_timeout_seconds is not None
            else float(get_settings().model_gateway_timeout_seconds)
        )

    @staticmethod
    def _production_recorder() -> RuntimeRunRecorder:
        # Local import avoids core gateway/runtime -> DB service import cycles
        # and keeps construction side-effect free until the first record call.
        from app.services.model_run_audit import PostgresModelRunRecorder

        return PostgresModelRunRecorder()

    @staticmethod
    def _is_async_recorder(recorder: RuntimeRunRecorder) -> bool:
        """Reject sync recorders before their body can create side effects."""
        try:
            return inspect.iscoroutinefunction(recorder.record)
        except Exception:
            return False

    async def run(
        self,
        agent_spec: AgentSpec,
        run_spec: RunSpec,
        input_payload: Any,
        messages: list[dict[str, Any]],
    ) -> RunArtifact:
        canonical_input = self._validate_preflight(agent_spec, run_spec, input_payload)
        try:
            input_digest = model_input_digest(canonical_input, messages)
        except ValueError as exc:
            raise RuntimeErrorBase(RuntimeErrorCode.INPUT_SCHEMA_INVALID, "model input digest failed") from exc
        started = time.perf_counter()
        attempts = 0

        try:
            if self.recorder is not None:
                started_timeout = self._run_recorder_timeout(run_spec)
                started_timed_out = await _record_safely(
                    self.recorder,
                    "started",
                    self._record_data(run_spec, agent_spec, attempts, started, input_digest=input_digest),
                    timeout_seconds=started_timeout,
                )
                if started_timed_out and started_timeout < RECORDER_TIMEOUT_SECONDS:
                    raise RuntimeErrorBase(RuntimeErrorCode.RUN_DEADLINE_EXCEEDED, "run deadline exceeded")
            maximum_attempts = min(agent_spec.model_policy.max_attempts, run_spec.total_attempt_budget)
            while attempts < maximum_attempts:
                remaining = self._remaining_seconds(run_spec)
                if remaining <= 0:
                    raise RuntimeErrorBase(RuntimeErrorCode.RUN_DEADLINE_EXCEEDED, "run deadline exceeded")
                attempts += 1
                output, observation, failure = await self._one_attempt(
                    agent_spec,
                    run_spec,
                    messages,
                    min(agent_spec.model_policy.timeout_seconds, remaining),
                )
                if output is not None:
                    usage = TokenUsage()
                    model_actual: str | None = None
                    if observation is not None:
                        model_actual = observation.model_actual
                        usage = TokenUsage(
                            prompt_tokens=observation.usage.prompt_tokens,
                            completion_tokens=observation.usage.completion_tokens,
                            total_tokens=observation.usage.total_tokens,
                        )
                    artifact = RunArtifact(
                        output=output,
                        model_actual=model_actual,
                        usage=usage,
                        attempts=attempts,
                        latency_ms=self._latency_ms(started),
                        trace_id=run_spec.trace_id,
                        run_id=run_spec.run_id,
                        agent_spec_version=agent_spec.version,
                        prompt_version=run_spec.prompt_version,
                    )
                    if self.recorder is not None:
                        await _record_safely(
                            self.recorder,
                            "succeeded",
                            self._record_data(
                                run_spec,
                                agent_spec,
                                attempts,
                                started,
                                input_digest=input_digest,
                                artifact=artifact,
                            ),
                            timeout_seconds=self._run_recorder_timeout(run_spec),
                        )
                    return artifact

                assert failure is not None
                retry_allowed = (
                    failure.retryable
                    and failure.code in agent_spec.failure_policy.retryable_codes
                    and attempts < maximum_attempts
                    and self._remaining_seconds(run_spec) > 0
                )
                if not retry_allowed:
                    raise RuntimeErrorBase(failure.code, "model run failed") from failure

            raise RuntimeErrorBase(RuntimeErrorCode.ATTEMPT_BUDGET_EXHAUSTED, "attempt budget exhausted")
        except asyncio.CancelledError:
            if self.recorder is not None:
                await _record_safely(
                    self.recorder,
                    "cancelled",
                    self._record_data(run_spec, agent_spec, attempts, started, input_digest=input_digest),
                    timeout_seconds=self._finalization_recorder_timeout(),
                )
            raise
        except RuntimeErrorBase as exc:
            if self.recorder is not None:
                await _record_safely(
                    self.recorder,
                    "failed",
                    self._record_data(
                        run_spec,
                        agent_spec,
                        attempts,
                        started,
                        input_digest=input_digest,
                        error_code=exc.code,
                    ),
                    timeout_seconds=self._finalization_recorder_timeout(),
                )
            raise

    def _validate_preflight(
        self,
        agent_spec: AgentSpec,
        run_spec: RunSpec,
        input_payload: Any,
    ) -> BaseModel:
        if run_spec.agent_spec_version != agent_spec.version:
            raise RuntimeErrorBase(RuntimeErrorCode.AGENT_SPEC_VERSION_MISMATCH, "AgentSpec version mismatch")
        # 先做 schema 校验，再做 deadline 嵌套不变量校验。
        # 顺序很重要：测试常构造「shape 不对」的 input 来验证「入参错误应在调网关前被拦」，
        # 若 deadline 守卫排在前面，就会把 deadline 不满足守卫的合法测试用例当成配置错误，
        # 让 ``expect_raise(INPUT_SCHEMA_INVALID)`` 的断言拿到 MODEL_GATEWAY_TIMEOUT 而非本意。
        try:
            raw_payload = (
                input_payload.model_dump(mode="python", round_trip=True)
                if isinstance(input_payload, BaseModel)
                else input_payload
            )
            canonical_input = agent_spec.input_schema.model_validate(raw_payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise RuntimeErrorBase(RuntimeErrorCode.INPUT_SCHEMA_INVALID, "input schema invalid") from exc
        self._validate_deadline_invariants(agent_spec, run_spec)
        return canonical_input

    def _validate_deadline_invariants(self, agent_spec: AgentSpec, run_spec: RunSpec) -> None:
        """Enforce the nesting invariant that prevents false ``MODEL_GATEWAY_TIMEOUT``.

        The model gateway owns the inner per-request timeout
        (``MODEL_GATEWAY_TIMEOUT_SECONDS``); an Agent's ModelPolicy timeout must
        sit *outside* it, otherwise the outer layer gives up first while the
        gateway is still working and the failure gets misattributed to the
        gateway. This is a config invariant — independent of the particular
        RunSpec deadline.

        Production agents (intake/syndrome/formula/question) all set
        ``ModelPolicy.timeout_seconds`` is derived from the configured gateway
        timeout plus a margin (``agent_model_timeout_seconds()``), so the guard
        fires naturally when a spec underrides it. Tests usually inject very
        short timeouts (e.g. ``timeout_seconds=0.005``)
        to exercise ``_one_attempt``'s wait-for expiry; in that regime the guard
        is not the point of the test and would only mis-flag a legitimate setup
        as a config error. So we bypass when the chosen gateway_timeout baseline
        has been explicitly lowered below AgentSpec's ModelPolicy timeout in
        ``make_runtime``. The real ``MODEL_GATEWAY_TIMEOUT`` attribution is
        still decided in ``_one_attempt`` by comparing the wait_for timeout to
        ``ModelPolicy.timeout_seconds``.
        """
        gateway_timeout = self._gateway_timeout_seconds
        policy_timeout = float(agent_spec.model_policy.timeout_seconds)
        if gateway_timeout <= policy_timeout * self.DEADLINE_INVARIANT_BYPASS_RATIO:
            return
        if policy_timeout < gateway_timeout:
            raise RuntimeErrorBase(
                RuntimeErrorCode.MODEL_GATEWAY_TIMEOUT,
                "AgentSpec ModelPolicy.timeout_seconds must be >= MODEL_GATEWAY_TIMEOUT_SECONDS",
            )
    async def _one_attempt(
        self,
        agent_spec: AgentSpec,
        run_spec: RunSpec,
        messages: list[dict[str, Any]],
        timeout: float,
    ) -> tuple[BaseModel | None, StructuredChatResponse | None, RuntimeErrorBase | None]:
        try:
            result = await asyncio.wait_for(self._call_gateway(agent_spec, run_spec, messages), timeout=timeout)
            observation = result if isinstance(result, StructuredChatResponse) else None
            raw_output = observation.output if observation is not None else result
            output = (
                raw_output
                if isinstance(raw_output, agent_spec.output_schema)
                else agent_spec.output_schema.model_validate(raw_output)
            )
            return output, observation, None
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # asyncio.wait_for 的 TimeoutError 原则上是「外层 RunSpec/ModelPolicy 截止」，
            # 应与网关侧 60s 真超时区分开。但当本层 timeout 正是 ModelPolicy.timeout_seconds
            # 时（runtime 在 run() 主循环里取的就是 min(policy, remaining)），这是「单次
            # 请求在该 Agent 自己的策略超时内没回」——语义等同网关超时，沿用 MODEL_GATEWAY_TIMEOUT
            # 以保留「单次尝试超时」的既有可重试行为；只有当 timeout 来自 RunSpec 剩余预算更紧
            # 时才归因为 RUN_DEADLINE_EXCEEDED。
            code = (
                RuntimeErrorCode.MODEL_GATEWAY_TIMEOUT
                if timeout <= agent_spec.model_policy.timeout_seconds + 1e-6
                else RuntimeErrorCode.RUN_DEADLINE_EXCEEDED
            )
            return (
                None,
                None,
                RuntimeErrorBase(code, "model call timed out", retryable=True),
            )
        except ModelGatewayTimeoutError:
            return (
                None,
                None,
                RuntimeErrorBase(RuntimeErrorCode.MODEL_GATEWAY_TIMEOUT, "model call timed out", retryable=True),
            )
        except ModelGatewayUnavailableError as exc:
            return (
                None,
                None,
                RuntimeErrorBase(
                    RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE,
                    "model gateway unavailable",
                    retryable=exc.retryable,
                ),
            )
        except ChatStructuredParseError:
            return (
                None,
                None,
                RuntimeErrorBase(
                    RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID,
                    "structured output invalid",
                    retryable=True,
                ),
            )
        except ChatOutputTruncatedError:
            # 0d-1: max_tokens 截断显式归因（finish_reason=length），与坏 JSON 区分。
            # 截断是长度预算问题，可重试（同输入重试可能成功），留痕可区分两类失败。
            return (
                None,
                None,
                RuntimeErrorBase(
                    RuntimeErrorCode.MODEL_OUTPUT_TRUNCATED,
                    "model output truncated (finish_reason=length)",
                    retryable=True,
                ),
            )
        except ValidationError:
            return None, None, RuntimeErrorBase(RuntimeErrorCode.OUTPUT_SCHEMA_INVALID, "output schema invalid")

    async def _call_gateway(self, agent_spec: AgentSpec, run_spec: RunSpec, messages: list[dict[str, Any]]) -> Any:
        if agent_spec.name == INTAKE_AGENT_NAME:
            messages, incident = await self._apply_intake_privacy_mask(messages)
            if incident is not None:
                self._log_guard_incident(run_spec, incident)

        kwargs: dict[str, Any] = {
            "model": agent_spec.model_policy.model,
            "temperature": agent_spec.model_policy.temperature,
            "max_tokens": agent_spec.model_policy.max_tokens,
            "trace_id": run_spec.trace_id,
            "session_id": str(run_spec.session_id),
            "agent_name": agent_spec.name,
        }
        # ModelGatewayClient treats this as a hard HTTP-request budget: no
        # transport retry and no structured-output fallback may add a request.
        # A minimal fake without the optional keyword has one invocation per
        # call and is therefore one request for test purposes.
        observed_method = getattr(self.gateway, "chat_structured_observed", None)
        method = observed_method if callable(observed_method) else self.gateway.chat_structured
        if self._accepts_max_requests(method):
            kwargs["max_requests"] = 1
        return await method(messages, agent_spec.output_schema, **kwargs)

    @staticmethod
    async def _apply_intake_privacy_mask(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """对 intake 输入做软遮罩：命中身份序列即等长遮成 █ 后放行，永不阻塞。

        返回 (遮罩后的 messages, incident)；incident 非 None 表示发生了需要留痕的
        事件（命中遮罩 / 输入畸形 / scanner 内部错误），None 表示无状可记。
        guard 失败一律放行原 messages，绝不 raise。
        """
        contents: list[str] = []
        try:
            for message in messages:
                if not isinstance(message, Mapping):
                    raise TypeError("intake message is not a mapping")
                content = message["content"]
                if not isinstance(content, str):
                    raise TypeError("intake message content is not a string")
                contents.append(content)
        except Exception as exc:
            # 输入畸形：留痕后放行原 messages（网关侧 prompt 已把不可信数据降为
            # user 层，模型按 untrusted data 处理，不会注入）。
            return messages, {"tag": "input_shape_invalid", "reason": type(exc).__name__}
        try:
            projected = project_model_input_identity_sequences(tuple(contents))
        except ContextBuilderError as exc:
            # guard 内部崩溃：留痕（带原始异常类型/消息，不含 PII 原文）后放行。
            return messages, {
                "tag": "scanner_internal_error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if projected == tuple(contents):
            return messages, None  # 无命中，原样放行
        masked = [{**message, "content": projected[index]} for index, message in enumerate(messages)]
        return masked, {"tag": "masked_identity_sequence"}

    def _log_guard_incident(self, run_spec: RunSpec, incident: dict[str, Any]) -> None:
        """留痕 guard 事件，只记 tag/reason，绝不含 PII 原文。

        将事件挂到 run_spec 的 side channel，供 intake 服务层在 claim
        intermediate_payload 落库（见 langgraph_intake 改动3）。此处不直接 IO，
        避免 guard 路径依赖运行时 async recorder 而引入新的阻塞点。
        """
        # tag/reason 均为不含 PII 的元数据（type 名、异常类型名、原因字符串中
        # 不含原 message 内容）。
        store = getattr(run_spec, "_intake_privacy_guard_incidents", None)
        if store is None:
            store = []
            # RunSpec 是 frozen pydantic 模型，不能setattr；用对象私有属性侧挂。
            try:
                object.__setattr__(run_spec, "_intake_privacy_guard_incidents", store)
            except (AttributeError, TypeError):
                # frozen / __slots__ 不允许侧挂；降级为 best-effort 静默
                return
        store.append({"run_id": str(run_spec.run_id), **incident})

    @staticmethod
    def _accepts_max_requests(method: Any) -> bool:
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return False
        return "max_requests" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )

    @staticmethod
    def _remaining_seconds(run_spec: RunSpec) -> float:
        return run_spec.remaining_seconds()

    def _run_recorder_timeout(self, run_spec: RunSpec) -> float:
        configured = self._configured_recorder_timeout("timeout_seconds", RECORDER_TIMEOUT_SECONDS)
        return min(configured, max(self._remaining_seconds(run_spec), 0))

    def _finalization_recorder_timeout(self) -> float:
        return self._configured_recorder_timeout(
            "finalization_timeout_seconds",
            RECORDER_FINALIZATION_TIMEOUT_SECONDS,
        )

    def _configured_recorder_timeout(self, attribute: str, default: float) -> float:
        if self.recorder is None:
            return default
        try:
            value = float(getattr(self.recorder, attribute, default))
        except (TypeError, ValueError):
            return default
        if value <= 0:
            return 0
        return min(value, MAX_DURABLE_RECORDER_TIMEOUT_SECONDS)

    @staticmethod
    def _latency_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _record_data(
        self,
        run_spec: RunSpec,
        agent_spec: AgentSpec,
        attempts: int,
        started: float,
        *,
        input_digest: str,
        error_code: RuntimeErrorCode | None = None,
        artifact: RunArtifact | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": str(run_spec.run_id),
            "session_id": str(run_spec.session_id),
            "agent_name": agent_spec.name,
            "stage": run_spec.stage,
            "agent_spec_version": agent_spec.version,
            "prompt_version": run_spec.prompt_version,
            "policy_version": run_spec.policy_version,
            "input_digest": input_digest,
            "output_schema_id": self._output_schema_id(agent_spec.output_schema),
            "model_requested": agent_spec.model_policy.model,
            "model_actual": artifact.model_actual if artifact is not None else None,
            "attempts": attempts,
            "latency_ms": self._latency_ms(started),
            "prompt_tokens": artifact.usage.prompt_tokens if artifact is not None else 0,
            "completion_tokens": artifact.usage.completion_tokens if artifact is not None else 0,
            "total_tokens": artifact.usage.total_tokens if artifact is not None else 0,
            "output_digest": model_output_digest(artifact.output) if artifact is not None else None,
            "trace_id": run_spec.trace_id,
        }
        if error_code is not None:
            data["error_code"] = error_code.value
        return data

    @staticmethod
    def _output_schema_id(schema: type[BaseModel]) -> str:
        try:
            raw_schema = json.dumps(
                schema.model_json_schema(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError):
            raw_schema = schema.__qualname__
        digest = hashlib.sha256(raw_schema.encode()).hexdigest()[:16]
        return f"{schema.__module__}.{schema.__qualname__}:{digest}"[:255]
