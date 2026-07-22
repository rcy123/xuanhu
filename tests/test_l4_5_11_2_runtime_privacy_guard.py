"""L4.5-11-2 Intake Runtime pre-Gateway privacy guard contracts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel

import app.agent_runtime.runtime as runtime_module
from app.agent_runtime.context import contains_model_input_identity_sequence
from app.agent_runtime.intake_verifier import INTAKE_AGENT_NAME
from app.agent_runtime.runtime import AgentRuntime, RuntimeErrorBase
from app.agent_runtime.specs import (
    AgentSpec,
    FailurePolicy,
    ModelPolicy,
    RunArtifact,
    RunSpec,
    RuntimeErrorCode,
)

PRIVACY_ERROR_CODE = "MODEL_INPUT_PRIVACY_VIOLATION"
PRIVACY_ERROR_MESSAGE = "model input privacy guard rejected request"
SYNTHETIC_PHONE = "13800000000"
SYNTHETIC_ID_CARD = "11010119900101001X"


class GuardInput(BaseModel):
    request: str


class GuardOutput(BaseModel):
    answer: str


class CountingObservedGateway:
    def __init__(self) -> None:
        self.observed_calls = 0
        self.plain_calls = 0
        self.actual_request_count = 0
        self.max_requests_seen: list[int | None] = []

    async def chat_structured_observed(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        max_requests: int | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        del messages, output_schema, kwargs
        self.observed_calls += 1
        self.actual_request_count += max_requests if max_requests is not None else 1
        self.max_requests_seen.append(max_requests)
        return {"answer": "ok"}

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        max_requests: int | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        del messages, output_schema, kwargs
        self.plain_calls += 1
        self.actual_request_count += max_requests if max_requests is not None else 1
        self.max_requests_seen.append(max_requests)
        return {"answer": "ok"}


class CountingPlainGateway:
    def __init__(self) -> None:
        self.observed_calls = 0
        self.plain_calls = 0
        self.actual_request_count = 0
        self.max_requests_seen: list[int | None] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        max_requests: int | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        del messages, output_schema, kwargs
        self.plain_calls += 1
        self.actual_request_count += max_requests if max_requests is not None else 1
        self.max_requests_seen.append(max_requests)
        return {"answer": "ok"}


class MemoryRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def record(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))


class ExplodingContentMapping(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise RuntimeError(f"must not expose synthetic content for {key}")

    def __iter__(self) -> Iterator[str]:
        return iter(("content",))

    def __len__(self) -> int:
        return 1


GatewayDouble = CountingObservedGateway | CountingPlainGateway


def make_spec(
    *,
    name: str = INTAKE_AGENT_NAME,
    max_attempts: int = 1,
    allow_every_error_to_retry: bool = False,
) -> AgentSpec:
    retryable_codes = frozenset(RuntimeErrorCode) if allow_every_error_to_retry else frozenset()
    return AgentSpec(
        name=name,
        version="runtime-privacy-guard.v1",
        input_schema=GuardInput,
        output_schema=GuardOutput,
        model_policy=ModelPolicy(
            model="fake-model",
            temperature=0,
            max_tokens=64,
            timeout_seconds=1,
            max_attempts=max_attempts,
        ),
        failure_policy=FailurePolicy(retryable_codes=retryable_codes),
    )


def make_run(*, total_attempt_budget: int = 3) -> RunSpec:
    return RunSpec(
        run_id=uuid4(),
        session_id=uuid4(),
        state_version=1,
        stage="inquiry",
        agent_spec_version="runtime-privacy-guard.v1",
        prompt_version="synthetic-prompt.v1",
        policy_version="runtime-privacy-guard-policy.v1",
        deadline_at=datetime.now(UTC) + timedelta(seconds=2),
        total_attempt_budget=total_attempt_budget,
        idempotency_key="runtime-privacy-guard-idempotency",
        trace_id="runtime-privacy-guard-trace",
    )


async def capture_run(
    gateway: GatewayDouble,
    messages: list[dict[str, Any]],
    *,
    spec: AgentSpec | None = None,
    recorder: MemoryRecorder | None = None,
) -> tuple[RuntimeErrorBase | None, RunArtifact | None]:
    runtime = AgentRuntime(gateway, recorder=recorder)
    try:
        artifact = await runtime.run(
            spec or make_spec(),
            make_run(),
            GuardInput(request="synthetic"),
            messages,
        )
    except RuntimeErrorBase as exc:
        return exc, None
    return None, artifact


def assert_privacy_rejection(error: RuntimeErrorBase | None, gateway: GatewayDouble) -> None:
    actual = (
        error.code.value if error is not None else None,
        gateway.observed_calls,
        gateway.plain_calls,
        gateway.actual_request_count,
        hasattr(RuntimeErrorCode, PRIVACY_ERROR_CODE),
    )
    assert actual == (PRIVACY_ERROR_CODE, 0, 0, 0, True)
    assert error is not None
    assert str(error) == PRIVACY_ERROR_MESSAGE
    assert error.retryable is False
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_unsafe_intake_runtime_bypass_is_rejected_before_observed_gateway() -> None:
    gateway = CountingObservedGateway()

    error, artifact = await capture_run(
        gateway,
        [{"role": "user", "content": f"phone={SYNTHETIC_PHONE}; id={SYNTHETIC_ID_CARD}"}],
    )

    assert_privacy_rejection(error, gateway)
    assert artifact is None


async def test_cross_final_message_identity_is_rejected_before_plain_gateway() -> None:
    gateway = CountingPlainGateway()

    error, artifact = await capture_run(
        gateway,
        [
            {"role": "system", "content": "synthetic boundary"},
            {"role": "user", "content": "13800"},
            {"role": "assistant", "content": "000000"},
        ],
    )

    assert_privacy_rejection(error, gateway)
    assert artifact is None


@pytest.mark.parametrize(
    "content",
    [
        SYNTHETIC_PHONE,
        "１３８００００００００",
        "138 0000 0000",
        "138-0000-0000",
        "138.0000.0000",
        SYNTHETIC_ID_CARD,
        "１１０１０１１９９００１０１００１Ｘ",
    ],
    ids=[
        "ascii-phone",
        "fullwidth-phone",
        "space-separated-phone",
        "hyphen-separated-phone",
        "dot-separated-phone",
        "ascii-id-x",
        "fullwidth-id-x",
    ],
)
async def test_supported_identity_grammar_is_rejected(content: str) -> None:
    gateway = CountingPlainGateway()

    error, artifact = await capture_run(gateway, [{"role": "user", "content": content}])

    assert artifact is None
    assert_privacy_rejection(error, gateway)


async def test_scanner_internal_exception_fails_closed_without_log_or_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    internal_secret = f"scanner failed near {SYNTHETIC_PHONE}"

    def explode(contents: tuple[str, ...]) -> bool:
        assert contents == (SYNTHETIC_PHONE,)
        raise RuntimeError(internal_secret)

    monkeypatch.setattr(
        runtime_module,
        "contains_model_input_identity_sequence",
        explode,
        raising=False,
    )
    gateway = CountingObservedGateway()

    error, artifact = await capture_run(gateway, [{"role": "user", "content": SYNTHETIC_PHONE}])

    assert artifact is None
    assert_privacy_rejection(error, gateway)
    assert SYNTHETIC_PHONE not in caplog.text
    assert internal_secret not in caplog.text


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user"},
        {"role": "user", "content": 13800000000},
    ],
    ids=["missing-content", "non-string-content"],
)
async def test_invalid_intake_message_content_fails_closed(message: dict[str, Any]) -> None:
    gateway = CountingPlainGateway()

    error, artifact = await capture_run(gateway, [message])

    assert artifact is None
    assert_privacy_rejection(error, gateway)


async def test_message_content_extraction_exception_fails_closed() -> None:
    gateway = CountingPlainGateway()
    runtime = AgentRuntime(gateway, recorder=None)
    messages = cast(list[dict[str, Any]], [ExplodingContentMapping()])

    error: RuntimeErrorBase | None = None
    try:
        await runtime._call_gateway(make_spec(), make_run(), messages)
    except RuntimeErrorBase as exc:
        error = exc

    assert_privacy_rejection(error, gateway)


async def test_privacy_rejection_recorder_events_are_metadata_only() -> None:
    gateway = CountingObservedGateway()
    recorder = MemoryRecorder()
    raw_content = f"synthetic phone {SYNTHETIC_PHONE}"

    error, artifact = await capture_run(
        gateway,
        [{"role": "user", "content": raw_content}],
        recorder=recorder,
    )

    assert artifact is None
    assert_privacy_rejection(error, gateway)
    assert [event for event, _ in recorder.events] == ["started", "failed"]
    assert recorder.events[-1][1]["error_code"] == PRIVACY_ERROR_CODE
    for _, event_data in recorder.events:
        assert "messages" not in event_data
        assert "input" not in event_data
        assert raw_content not in repr(event_data)
        assert SYNTHETIC_PHONE not in repr(event_data)
        assert len(event_data["input_digest"]) == 64


async def test_privacy_rejection_is_not_retried_even_when_policy_allows_every_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner_calls = 0

    def counting_scanner(contents: tuple[str, ...]) -> bool:
        nonlocal scanner_calls
        scanner_calls += 1
        return contains_model_input_identity_sequence(contents)

    monkeypatch.setattr(
        runtime_module,
        "contains_model_input_identity_sequence",
        counting_scanner,
        raising=False,
    )
    gateway = CountingObservedGateway()
    spec = make_spec(max_attempts=3, allow_every_error_to_retry=True)

    error, artifact = await capture_run(
        gateway,
        [{"role": "user", "content": SYNTHETIC_PHONE}],
        spec=spec,
    )

    assert artifact is None
    assert_privacy_rejection(error, gateway)
    assert scanner_calls == 1


async def test_safe_intake_calls_gateway_once_with_one_request_budget() -> None:
    gateway = CountingObservedGateway()

    error, artifact = await capture_run(
        gateway,
        [{"role": "user", "content": "synthetic safe intake text without identity sequence"}],
    )

    assert error is None
    assert artifact is not None
    assert artifact.output == GuardOutput(answer="ok")
    assert artifact.attempts == 1
    assert (gateway.observed_calls, gateway.plain_calls, gateway.actual_request_count) == (1, 0, 1)
    assert gateway.max_requests_seen == [1]


async def test_non_intake_agent_preserves_unsafe_message_gateway_behavior() -> None:
    gateway = CountingPlainGateway()
    non_intake_spec = make_spec(name=f"{INTAKE_AGENT_NAME}-other")

    error, artifact = await capture_run(
        gateway,
        [{"role": "user", "content": f"{SYNTHETIC_PHONE} {SYNTHETIC_ID_CARD}"}],
        spec=non_intake_spec,
    )

    assert error is None
    assert artifact is not None
    assert artifact.output == GuardOutput(answer="ok")
    assert (gateway.observed_calls, gateway.plain_calls, gateway.actual_request_count) == (0, 1, 1)
    assert gateway.max_requests_seen == [1]


async def test_non_intake_agent_does_not_read_message_content() -> None:
    gateway = CountingPlainGateway()
    runtime = AgentRuntime(gateway, recorder=None)
    messages = cast(list[dict[str, Any]], [ExplodingContentMapping()])

    result = await runtime._call_gateway(
        make_spec(name=f"other-{INTAKE_AGENT_NAME}"),
        make_run(),
        messages,
    )

    assert result == {"answer": "ok"}
    assert (gateway.observed_calls, gateway.plain_calls, gateway.actual_request_count) == (0, 1, 1)
    assert gateway.max_requests_seen == [1]


def test_runtime_error_code_contract_adds_only_privacy_violation() -> None:
    assert {code.value for code in RuntimeErrorCode} == {
        "AGENT_SPEC_VERSION_MISMATCH",
        "INPUT_SCHEMA_INVALID",
        "OUTPUT_SCHEMA_INVALID",
        "MODEL_GATEWAY_TIMEOUT",
        "MODEL_GATEWAY_UNAVAILABLE",
        "MODEL_INPUT_PRIVACY_VIOLATION",
        "STRUCTURED_OUTPUT_INVALID",
        "RUN_DEADLINE_EXCEEDED",
        "ATTEMPT_BUDGET_EXHAUSTED",
        "RECORDER_ASYNC_REQUIRED",
    }
