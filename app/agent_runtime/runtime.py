"""Unified, side-effect-free model execution boundary for L2-2."""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app.core.exceptions import ChatStructuredParseError, ModelGatewayTimeoutError, ModelGatewayUnavailableError
from app.core.gateway import ModelGatewayClient

from .specs import AgentSpec, RunArtifact, RunSpec, RuntimeErrorCode


class RuntimeErrorBase(Exception):
    """A sanitized failure whose code is stable and safe to record."""

    def __init__(self, code: RuntimeErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RuntimeRunRecorder(Protocol):
    """Async-only minimal observability seam; recorder failures are ignored."""

    async def record(self, event: str, data: dict[str, Any]) -> None: ...


RECORDER_TIMEOUT_SECONDS = 0.05
RECORDER_FINALIZATION_TIMEOUT_SECONDS = 0.05


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
    """Bound recorder work without changing the primary execution result.

    Returns whether the recorder was stopped by its timeout.
    """
    if recorder is None:
        return False
    if timeout_seconds <= 0:
        return True
    task = asyncio.create_task(_invoke_recorder(recorder, event, data), name="agent-runtime-recorder")
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
    except TimeoutError:
        await _cancel_recorder_task(task)
        return True
    except asyncio.CancelledError:
        # A recorder may cancel itself; external cancellation leaves the
        # shielded task pending and must keep propagating to the caller.
        if task.done() and task.cancelled():
            return False
        await _cancel_recorder_task(task)
        raise
    except Exception:
        return False
    return False


class AgentRuntime:
    """Execute an AgentSpec without writing state, persistence, or approvals."""

    def __init__(self, gateway: Any | None = None, recorder: RuntimeRunRecorder | None = None) -> None:
        # Only the runtime's constructed client has retries disabled.  Legacy
        # BaseAgentImpl retains its configured ModelGatewayClient unchanged.
        if recorder is not None and not self._is_async_recorder(recorder):
            raise RuntimeErrorBase(
                RuntimeErrorCode.RECORDER_ASYNC_REQUIRED,
                "runtime recorder must implement async record",
            ) from None
        self.gateway = gateway or ModelGatewayClient(max_retries=0)
        self.recorder = recorder

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
        self._validate_preflight(agent_spec, run_spec, input_payload)
        started = time.perf_counter()
        attempts = 0

        try:
            started_timeout = self._run_recorder_timeout(run_spec)
            started_timed_out = await _record_safely(
                self.recorder,
                "started",
                self._record_data(run_spec, agent_spec, attempts, started),
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
                output, failure = await self._one_attempt(
                    agent_spec,
                    run_spec,
                    messages,
                    min(agent_spec.model_policy.timeout_seconds, remaining),
                )
                if output is not None:
                    artifact = RunArtifact(
                        output=output,
                        model_actual=agent_spec.model_policy.model,
                        attempts=attempts,
                        latency_ms=self._latency_ms(started),
                        trace_id=run_spec.trace_id,
                        run_id=run_spec.run_id,
                        agent_spec_version=agent_spec.version,
                        prompt_version=run_spec.prompt_version,
                    )
                    await _record_safely(
                        self.recorder,
                        "succeeded",
                        self._record_data(run_spec, agent_spec, attempts, started),
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
            await _record_safely(
                self.recorder,
                "cancelled",
                self._record_data(run_spec, agent_spec, attempts, started),
                timeout_seconds=RECORDER_FINALIZATION_TIMEOUT_SECONDS,
            )
            raise
        except RuntimeErrorBase as exc:
            await _record_safely(
                self.recorder,
                "failed",
                self._record_data(run_spec, agent_spec, attempts, started, error_code=exc.code),
                timeout_seconds=RECORDER_FINALIZATION_TIMEOUT_SECONDS,
            )
            raise

    def _validate_preflight(self, agent_spec: AgentSpec, run_spec: RunSpec, input_payload: Any) -> None:
        if run_spec.agent_spec_version != agent_spec.version:
            raise RuntimeErrorBase(RuntimeErrorCode.AGENT_SPEC_VERSION_MISMATCH, "AgentSpec version mismatch")
        try:
            if not isinstance(input_payload, agent_spec.input_schema):
                agent_spec.input_schema.model_validate(input_payload)
        except ValidationError as exc:
            raise RuntimeErrorBase(RuntimeErrorCode.INPUT_SCHEMA_INVALID, "input schema invalid") from exc

    async def _one_attempt(
        self,
        agent_spec: AgentSpec,
        run_spec: RunSpec,
        messages: list[dict[str, Any]],
        timeout: float,
    ) -> tuple[BaseModel | None, RuntimeErrorBase | None]:
        try:
            result = await asyncio.wait_for(self._call_gateway(agent_spec, run_spec, messages), timeout=timeout)
            output = (
                result
                if isinstance(result, agent_spec.output_schema)
                else agent_spec.output_schema.model_validate(result)
            )
            return output, None
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return None, RuntimeErrorBase(RuntimeErrorCode.MODEL_GATEWAY_TIMEOUT, "model call timed out", retryable=True)
        except ModelGatewayTimeoutError:
            return None, RuntimeErrorBase(RuntimeErrorCode.MODEL_GATEWAY_TIMEOUT, "model call timed out", retryable=True)
        except ModelGatewayUnavailableError as exc:
            return None, RuntimeErrorBase(
                RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE,
                "model gateway unavailable",
                retryable=exc.retryable,
            )
        except ChatStructuredParseError:
            return None, RuntimeErrorBase(
                RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID,
                "structured output invalid",
                retryable=True,
            )
        except ValidationError:
            return None, RuntimeErrorBase(RuntimeErrorCode.OUTPUT_SCHEMA_INVALID, "output schema invalid")

    async def _call_gateway(self, agent_spec: AgentSpec, run_spec: RunSpec, messages: list[dict[str, Any]]) -> Any:
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
        if self._accepts_max_requests():
            kwargs["max_requests"] = 1
        return await self.gateway.chat_structured(messages, agent_spec.output_schema, **kwargs)

    def _accepts_max_requests(self) -> bool:
        try:
            signature = inspect.signature(self.gateway.chat_structured)
        except (TypeError, ValueError):
            return False
        return "max_requests" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )

    @staticmethod
    def _remaining_seconds(run_spec: RunSpec) -> float:
        return (run_spec.deadline_at - datetime.now(UTC)).total_seconds()

    def _run_recorder_timeout(self, run_spec: RunSpec) -> float:
        return min(RECORDER_TIMEOUT_SECONDS, max(self._remaining_seconds(run_spec), 0))

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
        error_code: RuntimeErrorCode | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": str(run_spec.run_id),
            "session_id": str(run_spec.session_id),
            "agent_spec_version": agent_spec.version,
            "prompt_version": run_spec.prompt_version,
            "model": agent_spec.model_policy.model,
            "attempts": attempts,
            "latency_ms": self._latency_ms(started),
            "trace_id": run_spec.trace_id,
        }
        if error_code is not None:
            data["error_code"] = error_code.value
        return data
