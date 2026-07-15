"""L2-2 harness contracts: all model calls are fake and countable."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from app.agent_runtime.runtime import AgentRuntime, RuntimeErrorBase
from app.agent_runtime.specs import (
    AgentSpec,
    Capability,
    FailurePolicy,
    ModelPolicy,
    RunSpec,
    RuntimeErrorCode,
    model_input_digest,
    model_output_digest,
)
from app.core.exceptions import (
    ChatStructuredParseError,
    ModelGatewayUnavailableError,
    ModelRunAuditIntegrityError,
    ModelRunAuditUnavailableError,
)
from app.core.gateway import ModelTokenUsage, StructuredChatResponse
from app.services.model_run_audit import (
    ModelRunAuditAlreadyFinalizedError,
    ModelRunAuditProvenanceConflictError,
    ModelRunAuditTerminalConflictError,
    PostgresModelRunRecorder,
)


class InputPayload(BaseModel):
    request: str


class OutputPayload(BaseModel):
    answer: str


class AlternateInputPayload(BaseModel):
    request: str


class MappingInputPayload(BaseModel):
    attributes: dict[str, int]


class NumericInputPayload(BaseModel):
    score: float


class FakeGateway:
    def __init__(self, outcomes: list[Any], *, delay_seconds: float = 0) -> None:
        self.outcomes = outcomes
        self.delay_seconds = delay_seconds
        self.calls: list[dict[str, Any]] = []
        self.actual_request_count = 0
        self.entered = asyncio.Event()

    async def chat_structured(
        self, messages: list[dict[str, Any]], output_schema: type[BaseModel], **kwargs: Any
    ) -> Any:
        self.calls.append({"messages": messages, "output_schema": output_schema, **kwargs})
        # max_requests represents the actual HTTP request budget for this
        # gateway operation. A spy sees exactly one model request per call.
        self.actual_request_count += kwargs.get("max_requests", 1)
        self.entered.set()
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ObservedGateway(FakeGateway):
    async def chat_structured_observed(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> StructuredChatResponse:
        output = await self.chat_structured(messages, output_schema, **kwargs)
        return StructuredChatResponse(
            output=output_schema.model_validate(output),
            model_actual="served-model-2026-07",
            usage=ModelTokenUsage(prompt_tokens=17, completion_tokens=5, total_tokens=22),
        )


class FakeRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def record(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))
        if self.fail:
            raise RuntimeError("api-key=secret prompt for patient Alice")


class IntegrityConflictRecorder:
    """Recorder double that raises only on terminal finalization."""

    def __init__(
        self,
        conflict: ModelRunAuditIntegrityError,
        *,
        conflict_event: str = "succeeded",
    ) -> None:
        self.conflict = conflict
        self.conflict_event = conflict_event
        self.events: list[str] = []

    async def record(self, event: str, data: dict[str, Any]) -> None:
        del data
        self.events.append(event)
        if event == self.conflict_event:
            raise self.conflict


class RequiredRecorder:
    """Required durable recorder double for exception and timeout contracts."""

    required = True
    timeout_seconds = 0.1
    finalization_timeout_seconds = 0.1

    def __init__(self, failure_event: str, failure_mode: str) -> None:
        self.failure_event = failure_event
        self.failure_mode = failure_mode
        self.events: list[str] = []
        self.cancelled = asyncio.Event()
        self._never = asyncio.Event()

    async def record(self, event: str, data: dict[str, Any]) -> None:
        del data
        self.events.append(event)
        if event != self.failure_event:
            return
        if self.failure_mode == "exception":
            raise RuntimeError("private database or patient detail")
        try:
            await self._never.wait()
        finally:
            self.cancelled.set()


class SyncRecorder:
    """Forbidden recorder whose body would expose a late side effect if called."""

    def __init__(self) -> None:
        self.called = threading.Event()

    def record(self, event: str, data: dict[str, Any]) -> None:
        del event, data
        self.called.set()
        raise RuntimeError("api-key=sync-secret prompt for patient Alice")


class BlockingRecorder:
    """An async recorder that waits forever until runtime cancels it."""

    def __init__(self, blocked_events: set[str]) -> None:
        self.blocked_events = blocked_events
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._never = asyncio.Event()

    async def record(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))
        if event in self.blocked_events:
            self.entered.set()
            try:
                await self._never.wait()
            finally:
                self.cancelled.set()


def make_spec(
    *,
    max_attempts: int = 3,
    timeout_seconds: float = 1,
    retryable_codes: set[RuntimeErrorCode] | None = None,
) -> AgentSpec:
    return AgentSpec(
        name="test-agent",
        version="v1",
        input_schema=InputPayload,
        output_schema=OutputPayload,
        model_policy=ModelPolicy(
            model="fake-model",
            temperature=0.7,
            max_tokens=123,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        ),
        failure_policy=FailurePolicy(retryable_codes=frozenset(retryable_codes or set())),
    )


def make_run(*, budget: int = 3, deadline: datetime | None = None, version: str = "v1") -> RunSpec:
    return RunSpec(
        run_id=uuid4(),
        session_id=uuid4(),
        state_version=1,
        stage="inquiry",
        agent_spec_version=version,
        prompt_version="prompt-v1",
        policy_version="test-runtime-policy.v1",
        deadline_at=deadline or datetime.now(UTC) + timedelta(seconds=2),
        total_attempt_budget=budget,
        idempotency_key="idempotency-key",
        trace_id="trace-123",
    )


def secret_messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "Alice, api-key=secret-key, prompt must not be recorded"}]


def test_model_input_digest_is_stable_for_canonical_equivalent_payloads() -> None:
    first = MappingInputPayload(attributes={"b": 2, "a": 1})
    second = MappingInputPayload(attributes={"a": 1, "b": 2})
    first_messages: list[dict[str, object]] = [{"content": "same prompt", "role": "user"}]
    second_messages: list[dict[str, object]] = [{"role": "user", "content": "same prompt"}]

    assert model_input_digest(first, first_messages) == model_input_digest(first, first_messages)
    assert model_input_digest(first, first_messages) == model_input_digest(second, second_messages)


def test_model_input_digest_separates_schema_type_and_input_domain() -> None:
    payload = InputPayload(request="same-value")
    messages: list[dict[str, object]] = [{"role": "user", "content": "same prompt"}]
    canonical = json.dumps(
        {
            "type": f"{type(payload).__module__}.{type(payload).__qualname__}",
            "input": payload.model_dump(mode="json"),
            "messages": messages,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected = hashlib.sha256(f"xuanhu:model-input:v2:{canonical}".encode()).hexdigest()

    assert model_input_digest(payload, messages) == expected
    assert model_input_digest(payload, messages) != model_input_digest(
        AlternateInputPayload(request="same-value"), messages
    )
    assert model_input_digest(payload, messages) != model_output_digest(payload)


def test_model_input_digest_changes_when_the_payload_changes() -> None:
    messages: list[dict[str, object]] = [{"role": "user", "content": "same prompt"}]
    assert model_input_digest(InputPayload(request="first"), messages) != model_input_digest(
        InputPayload(request="second"), messages
    )


def test_model_input_digest_changes_when_messages_change() -> None:
    payload = InputPayload(request="same-value")
    first: list[dict[str, object]] = [{"role": "user", "content": "first prompt"}]
    second: list[dict[str, object]] = [{"role": "user", "content": "second prompt"}]

    assert model_input_digest(payload, first) != model_input_digest(payload, second)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_model_input_digest_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="not canonically serializable"):
        model_input_digest(NumericInputPayload(score=value), [])


def test_specs_validate_boundaries_and_are_read_only() -> None:
    spec = make_spec()
    assert spec.tool_permissions == frozenset()
    assert spec.model_policy.model == "fake-model"
    with pytest.raises(ValidationError):
        ModelPolicy(model="", max_tokens=0)
    with pytest.raises(ValidationError):
        RunSpec.model_validate({**make_run().model_dump(), "state_version": 0})
    with pytest.raises(ValidationError):
        RunSpec.model_validate({**make_run().model_dump(), "deadline_at": datetime.now()})
    with pytest.raises(ValidationError):
        AgentSpec(
            name="forbidden-agent",
            version="v1",
            input_schema=InputPayload,
            output_schema=OutputPayload,
            model_policy=ModelPolicy(model="fake-model"),
            tool_permissions=frozenset({Capability.WRITE_STATE}),
        )


@pytest.mark.asyncio
async def test_input_schema_is_rejected_before_gateway_call() -> None:
    gateway = FakeGateway([{"answer": "unused"}])
    with pytest.raises(RuntimeErrorBase, match="input schema") as exc_info:
        await AgentRuntime(gateway, recorder=None).run(make_spec(), make_run(), {"wrong": "shape"}, secret_messages())
    assert exc_info.value.code is RuntimeErrorCode.INPUT_SCHEMA_INVALID
    assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_output_schema_is_rejected_after_gateway_call() -> None:
    gateway = FakeGateway([{"wrong": "shape"}])
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await AgentRuntime(gateway, recorder=None).run(make_spec(), make_run(), {"request": "ok"}, secret_messages())
    assert exc_info.value.code is RuntimeErrorCode.OUTPUT_SCHEMA_INVALID
    assert gateway.actual_request_count == 1


@pytest.mark.asyncio
async def test_version_mismatch_rejected_before_gateway_call() -> None:
    gateway = FakeGateway([{"answer": "unused"}])
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await AgentRuntime(gateway, recorder=None).run(
            make_spec(), make_run(version="v2"), {"request": "ok"}, secret_messages()
        )
    assert exc_info.value.code is RuntimeErrorCode.AGENT_SPEC_VERSION_MISMATCH
    assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_success_artifact_and_gateway_parameters_are_complete() -> None:
    gateway = FakeGateway([{"answer": "ok"}])
    recorder = FakeRecorder()
    run = make_run()
    artifact = await AgentRuntime(gateway, recorder).run(make_spec(), run, {"request": "ok"}, secret_messages())
    call = gateway.calls[0]
    assert artifact.output == OutputPayload(answer="ok")
    # A legacy fake does not report serving metadata.  Requested model must not
    # be copied into the actual-model field.
    assert artifact.model_actual is None
    assert artifact.attempts == 1
    assert artifact.run_id == run.run_id
    assert artifact.trace_id == run.trace_id
    assert artifact.agent_spec_version == "v1"
    assert artifact.prompt_version == "prompt-v1"
    assert artifact.usage.total_tokens == 0
    assert call["model"] == "fake-model"
    assert call["temperature"] == 0.7
    assert call["max_tokens"] == 123
    assert call["trace_id"] == run.trace_id
    assert call["session_id"] == str(run.session_id)
    assert call["agent_name"] == "test-agent"
    assert call["max_requests"] == 1
    assert [event for event, _ in recorder.events] == ["started", "succeeded"]


@pytest.mark.asyncio
async def test_observed_gateway_populates_actual_model_usage_and_output_digest() -> None:
    gateway = ObservedGateway([{"answer": "ok"}])
    recorder = FakeRecorder()
    artifact = await AgentRuntime(gateway, recorder).run(make_spec(), make_run(), {"request": "ok"}, secret_messages())

    assert artifact.model_actual == "served-model-2026-07"
    assert artifact.usage.prompt_tokens == 17
    assert artifact.usage.completion_tokens == 5
    assert artifact.usage.total_tokens == 22
    succeeded = recorder.events[-1][1]
    started = recorder.events[0][1]
    assert succeeded["model_requested"] == "fake-model"
    assert succeeded["model_actual"] == "served-model-2026-07"
    assert succeeded["policy_version"] == "test-runtime-policy.v1"
    assert succeeded["input_digest"] == started["input_digest"]
    assert succeeded["input_digest"] == model_input_digest(InputPayload(request="ok"), secret_messages())
    assert succeeded["output_digest"] is not None
    assert len(succeeded["output_digest"]) == 64


@pytest.mark.asyncio
async def test_failure_policy_allows_only_listed_retryable_codes() -> None:
    gateway = FakeGateway([ModelGatewayUnavailableError(retryable=True), {"answer": "ok"}])
    spec = make_spec(retryable_codes={RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE})
    artifact = await AgentRuntime(gateway, recorder=None).run(spec, make_run(), {"request": "ok"}, secret_messages())
    assert artifact.attempts == 2
    assert gateway.actual_request_count == 2


@pytest.mark.asyncio
async def test_failure_policy_rejects_retryable_gateway_error_not_listed() -> None:
    gateway = FakeGateway([ModelGatewayUnavailableError(retryable=True), {"answer": "unused"}])
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await AgentRuntime(gateway, recorder=None).run(make_spec(), make_run(), {"request": "ok"}, secret_messages())
    assert exc_info.value.code is RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE
    assert gateway.actual_request_count == 1


@pytest.mark.asyncio
async def test_non_retryable_gateway_error_stops_even_when_code_is_allowed() -> None:
    gateway = FakeGateway([ModelGatewayUnavailableError(retryable=False), {"answer": "unused"}])
    spec = make_spec(retryable_codes={RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE})
    with pytest.raises(RuntimeErrorBase):
        await AgentRuntime(gateway, recorder=None).run(spec, make_run(), {"request": "ok"}, secret_messages())
    assert gateway.actual_request_count == 1


@pytest.mark.asyncio
async def test_total_attempt_budget_caps_actual_model_requests() -> None:
    gateway = FakeGateway([ChatStructuredParseError(), ChatStructuredParseError(), {"answer": "unused"}])
    spec = make_spec(max_attempts=10, retryable_codes={RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID})
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await AgentRuntime(gateway, recorder=None).run(spec, make_run(budget=2), {"request": "ok"}, secret_messages())
    assert exc_info.value.code is RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID
    assert gateway.actual_request_count == 2
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_model_policy_max_attempts_caps_actual_model_requests() -> None:
    gateway = FakeGateway([ChatStructuredParseError(), ChatStructuredParseError(), {"answer": "unused"}])
    spec = make_spec(max_attempts=2, retryable_codes={RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID})
    with pytest.raises(RuntimeErrorBase):
        await AgentRuntime(gateway, recorder=None).run(spec, make_run(budget=10), {"request": "ok"}, secret_messages())
    assert gateway.actual_request_count == 2


@pytest.mark.asyncio
async def test_expired_deadline_makes_zero_gateway_calls_and_records_failure() -> None:
    gateway = FakeGateway([{"answer": "unused"}])
    recorder = FakeRecorder()
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await AgentRuntime(gateway, recorder).run(
            make_spec(),
            make_run(deadline=datetime.now(UTC) - timedelta(seconds=1)),
            {"request": "ok"},
            secret_messages(),
        )
    assert exc_info.value.code is RuntimeErrorCode.RUN_DEADLINE_EXCEEDED
    assert gateway.actual_request_count == 0
    assert [event for event, _ in recorder.events] == ["failed"]


@pytest.mark.asyncio
async def test_single_attempt_timeout_is_recorded_and_not_retried_without_policy() -> None:
    gateway = FakeGateway([{"answer": "late"}], delay_seconds=0.03)
    recorder = FakeRecorder()
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await AgentRuntime(gateway, recorder).run(
            make_spec(timeout_seconds=0.005), make_run(), {"request": "ok"}, secret_messages()
        )
    assert exc_info.value.code is RuntimeErrorCode.MODEL_GATEWAY_TIMEOUT
    assert gateway.actual_request_count == 1
    assert recorder.events[-1][0] == "failed"


@pytest.mark.asyncio
async def test_complete_run_deadline_prevents_a_second_gateway_call() -> None:
    gateway = FakeGateway([ChatStructuredParseError(), {"answer": "unused"}], delay_seconds=0.03)
    spec = make_spec(max_attempts=3, retryable_codes={RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID})
    with pytest.raises(RuntimeErrorBase):
        await AgentRuntime(gateway, recorder=None).run(
            spec,
            make_run(deadline=datetime.now(UTC) + timedelta(seconds=0.01)),
            {"request": "ok"},
            secret_messages(),
        )
    assert gateway.actual_request_count == 1


@pytest.mark.asyncio
async def test_cancelled_error_propagates_and_is_recorded() -> None:
    gateway = FakeGateway([{"answer": "never"}], delay_seconds=1)
    recorder = FakeRecorder()
    task = asyncio.create_task(
        AgentRuntime(gateway, recorder).run(make_spec(), make_run(), {"request": "ok"}, secret_messages())
    )
    await gateway.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.events[-1][0] == "cancelled"


@pytest.mark.asyncio
async def test_recorder_is_minimal_and_its_failure_is_degraded() -> None:
    gateway = FakeGateway([{"answer": "ok"}])
    recorder = FakeRecorder(fail=True)
    await AgentRuntime(gateway, recorder).run(
        make_spec(),
        make_run(),
        {"request": "Alice patient identity"},
        secret_messages(),
    )
    allowed = {
        "run_id",
        "session_id",
        "agent_name",
        "stage",
        "agent_spec_version",
        "prompt_version",
        "policy_version",
        "input_digest",
        "output_schema_id",
        "model_requested",
        "model_actual",
        "attempts",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "output_digest",
        "trace_id",
        "error_code",
    }
    for _, event_data in recorder.events:
        assert set(event_data) <= allowed
        assert "Alice" not in repr(event_data)
        assert "secret-key" not in repr(event_data)
        assert "messages" not in event_data
        assert "input_payload" not in event_data
        assert "raw_output" not in event_data


@pytest.mark.parametrize(
    "conflict_type",
    [ModelRunAuditProvenanceConflictError, ModelRunAuditTerminalConflictError],
)
@pytest.mark.asyncio
async def test_model_run_audit_integrity_conflict_fails_closed(
    conflict_type: type[ModelRunAuditIntegrityError],
) -> None:
    recorder = IntegrityConflictRecorder(conflict_type())

    with pytest.raises(ModelRunAuditIntegrityError) as exc_info:
        await AgentRuntime(FakeGateway([{"answer": "ok"}]), recorder).run(
            make_spec(),
            make_run(),
            {"request": "private patient input"},
            secret_messages(),
        )

    assert type(exc_info.value) is conflict_type
    assert str(exc_info.value) in {
        "model-run audit provenance conflict",
        "model-run audit terminal conflict",
    }
    assert "private" not in str(exc_info.value)
    assert "patient" not in str(exc_info.value)
    assert recorder.events == ["started", "succeeded"]


@pytest.mark.asyncio
async def test_already_finalized_started_record_fails_before_model_call() -> None:
    gateway = FakeGateway([{"answer": "must-not-be-called"}])
    recorder = IntegrityConflictRecorder(
        ModelRunAuditAlreadyFinalizedError(),
        conflict_event="started",
    )

    with pytest.raises(
        ModelRunAuditAlreadyFinalizedError,
        match="^model-run audit already finalized$",
    ):
        await AgentRuntime(gateway, recorder).run(
            make_spec(),
            make_run(),
            {"request": "private patient input"},
            secret_messages(),
        )

    assert gateway.actual_request_count == 0
    assert recorder.events == ["started"]


@pytest.mark.parametrize(
    ("failure_event", "failure_mode"),
    [
        ("started", "exception"),
        ("started", "timeout"),
        ("succeeded", "exception"),
        ("succeeded", "timeout"),
    ],
)
@pytest.mark.asyncio
async def test_required_recorder_failure_never_degrades_to_model_execution_success(
    failure_event: str,
    failure_mode: str,
) -> None:
    gateway = FakeGateway([{"answer": "must-not-be-returned"}])
    recorder = RequiredRecorder(failure_event, failure_mode)

    with pytest.raises(
        ModelRunAuditUnavailableError,
        match="^model-run audit unavailable$",
    ) as exc_info:
        await AgentRuntime(gateway, recorder).run(
            make_spec(),
            make_run(),
            {"request": "private patient input"},
            secret_messages(),
        )

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert "private" not in str(exc_info.value)
    assert "patient" not in str(exc_info.value)
    if failure_event == "started":
        assert gateway.actual_request_count == 0
        assert recorder.events == ["started"]
    else:
        assert gateway.actual_request_count == 1
        assert recorder.events == ["started", "succeeded"]
    if failure_mode == "timeout":
        await asyncio.wait_for(recorder.cancelled.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_blocked_started_recorder_is_bounded_by_run_deadline_before_gateway() -> None:
    gateway = FakeGateway([{"answer": "unused"}])
    recorder = BlockingRecorder({"started"})
    deadline = datetime.now(UTC) + timedelta(seconds=0.01)
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await asyncio.wait_for(
            AgentRuntime(gateway, recorder).run(
                make_spec(), make_run(deadline=deadline), {"request": "ok"}, secret_messages()
            ),
            timeout=0.3,
        )
    assert exc_info.value.code is RuntimeErrorCode.RUN_DEADLINE_EXCEEDED
    assert gateway.actual_request_count == 0
    await asyncio.wait_for(recorder.cancelled.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_blocked_succeeded_recorder_does_not_change_success_or_leave_task() -> None:
    gateway = FakeGateway([{"answer": "ok"}])
    recorder = BlockingRecorder({"succeeded"})
    artifact = await asyncio.wait_for(
        AgentRuntime(gateway, recorder).run(make_spec(), make_run(), {"request": "ok"}, secret_messages()),
        timeout=0.3,
    )
    assert artifact.output == OutputPayload(answer="ok")
    assert gateway.actual_request_count == 1
    await asyncio.wait_for(recorder.cancelled.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_blocked_failed_recorder_preserves_fixed_error_code() -> None:
    gateway = FakeGateway([ModelGatewayUnavailableError(retryable=False)])
    recorder = BlockingRecorder({"failed"})
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await asyncio.wait_for(
            AgentRuntime(gateway, recorder).run(make_spec(), make_run(), {"request": "ok"}, secret_messages()),
            timeout=0.3,
        )
    assert exc_info.value.code is RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE
    await asyncio.wait_for(recorder.cancelled.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_blocked_cancelled_recorder_preserves_cancelled_error() -> None:
    gateway = FakeGateway([{"answer": "never"}], delay_seconds=1)
    recorder = BlockingRecorder({"cancelled"})
    task = asyncio.create_task(
        AgentRuntime(gateway, recorder).run(make_spec(), make_run(), {"request": "ok"}, secret_messages())
    )
    await gateway.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.3)
    await asyncio.wait_for(recorder.cancelled.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_recorder_secret_exception_never_enters_primary_error_chain_or_event_data() -> None:
    gateway = FakeGateway([ModelGatewayUnavailableError(retryable=False)])
    recorder = FakeRecorder(fail=True)
    with pytest.raises(RuntimeErrorBase) as exc_info:
        await AgentRuntime(gateway, recorder).run(make_spec(), make_run(), {"request": "ok"}, secret_messages())
    assert exc_info.value.code is RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE
    chain: list[BaseException] = []
    current: BaseException | None = exc_info.value
    while current is not None:
        chain.append(current)
        current = current.__cause__
    assert "secret" not in repr(chain)
    assert "Alice" not in repr(chain)
    assert all("secret" not in repr(data) and "Alice" not in repr(data) for _, data in recorder.events)


def test_sync_recorder_is_rejected_before_execution_or_thread_creation() -> None:
    recorder = SyncRecorder()
    threads_before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(RuntimeErrorBase) as exc_info:
        AgentRuntime(FakeGateway([{"answer": "unused"}]), recorder)  # type: ignore[arg-type]
    assert exc_info.value.code is RuntimeErrorCode.RECORDER_ASYNC_REQUIRED
    assert recorder.called.is_set() is False
    assert "sync-secret" not in repr(exc_info.value)
    assert "Alice" not in repr(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert threads_before == {thread.ident for thread in threading.enumerate()}


def test_omitted_recorder_uses_required_postgres_audit_with_injected_gateway() -> None:
    runtime = AgentRuntime(FakeGateway([{"answer": "unused"}]))

    assert isinstance(runtime.recorder, PostgresModelRunRecorder)
    assert runtime.recorder.required is True


@pytest.mark.asyncio
async def test_blocked_async_recorder_leaves_no_runtime_pending_task() -> None:
    recorder = BlockingRecorder({"succeeded"})
    artifact = await AgentRuntime(FakeGateway([{"answer": "ok"}]), recorder).run(
        make_spec(), make_run(), {"request": "ok"}, secret_messages()
    )
    assert artifact.output == OutputPayload(answer="ok")
    await asyncio.wait_for(recorder.cancelled.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task.get_name() == "agent-runtime-recorder" and not task.done()
    ]


@pytest.mark.asyncio
async def test_runtime_and_artifact_have_no_authoritative_state_or_approval_surface() -> None:
    runtime = AgentRuntime(FakeGateway([{"answer": "ok"}]), recorder=None)
    artifact = await runtime.run(make_spec(), make_run(), {"request": "ok"}, secret_messages())
    assert not hasattr(runtime, "state")
    assert not hasattr(runtime, "approve_safety")
    assert not hasattr(runtime, "transition_stage")
    assert {"state", "stage", "verified", "approved", "submitted"}.isdisjoint(type(artifact).model_fields)
