"""L4.5-11-2 Intake Runtime pre-Gateway privacy guard contracts.

The guard performs soft-masking: identity sequences are masked to ``█`` and
the request proceeds; guard internal errors and malformed inputs degrade to
passing the original messages through. The guard never raises and never blocks
intake. ``MODEL_INPUT_PRIVACY_VIOLATION`` remains in the error-code contract
but is no longer raised from the guard path.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel

import app.agent_runtime.runtime as runtime_module
from app.agent_runtime.context import ContextBuilderError
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
SYNTHETIC_PHONE = "13800000000"
SYNTHETIC_ID_CARD = "11010119900101001X"
MASK_CHAR = "█"


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
        self.last_messages: list[dict[str, Any]] = []

    async def chat_structured_observed(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        max_requests: int | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        del output_schema, kwargs
        self.observed_calls += 1
        self.actual_request_count += max_requests if max_requests is not None else 1
        self.max_requests_seen.append(max_requests)
        self.last_messages = [dict(item) for item in messages]
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
        self.last_messages: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        max_requests: int | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        del output_schema, kwargs
        self.plain_calls += 1
        self.actual_request_count += max_requests if max_requests is not None else 1
        self.max_requests_seen.append(max_requests)
        try:
            self.last_messages = [dict(item) for item in messages]
        except Exception:
            # ExplodingContentMapping intentionally raises on content access;
            # guard now passes it through instead of rejecting, so the gateway
            # may receive a non-dictable message. Counting is enough here.
            self.last_messages = []
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
    runtime = AgentRuntime(gateway, recorder=recorder, gateway_timeout_seconds=0.0)
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


def assert_privacy_passthrough(
    error: RuntimeErrorBase | None,
    gateway: GatewayDouble,
    *,
    expected_request_count: int = 1,
) -> None:
    """Guard no longer raises: request proceeds; gateway is invoked.

    ``MODEL_INPUT_PRIVACY_VIOLATION`` is not surfaced from the guard path.
    """
    assert error is None
    observed = getattr(gateway, "observed_calls", 0)
    plain = getattr(gateway, "plain_calls", 0)
    assert observed + plain == expected_request_count
    assert gateway.actual_request_count == expected_request_count
    assert hasattr(RuntimeErrorCode, PRIVACY_ERROR_CODE)  # 仍保留枚举契约


def _seen_messages(gateway: object) -> list[dict[str, Any]]:
    return getattr(gateway, "last_messages", [])  # type: ignore[no-any-return]


def assert_privacy_rejection(error: RuntimeErrorBase | None, gateway: GatewayDouble) -> None:
    """Legacy rejection helper retained for backward-compat references; guard no
    longer rejects — kept as a marker that the contract test below still holds."""
    del error, gateway
    assert hasattr(RuntimeErrorCode, PRIVACY_ERROR_CODE)


async def test_unsafe_intake_runtime_bypass_is_masked_before_observed_gateway() -> None:
    gateway = CountingObservedGateway()
    raw_content = f"phone={SYNTHETIC_PHONE}; id={SYNTHETIC_ID_CARD}"

    error, artifact = await capture_run(gateway, [{"role": "user", "content": raw_content}])

    assert_privacy_passthrough(error, gateway)
    assert artifact is not None
    assert artifact.output == GuardOutput(answer="ok")
    seen = _seen_messages(gateway)
    assert len(seen) == 1
    sent_content = seen[0]["content"]
    assert SYNTHETIC_PHONE not in sent_content
    assert SYNTHETIC_ID_CARD not in sent_content
    assert MASK_CHAR in sent_content
    assert len(sent_content) == len(raw_content)  # 等长遮罩


async def test_cross_final_message_identity_is_masked_before_plain_gateway() -> None:
    gateway = CountingPlainGateway()

    error, artifact = await capture_run(
        gateway,
        [
            {"role": "system", "content": "synthetic boundary"},
            {"role": "user", "content": "13800"},
            {"role": "assistant", "content": "000000"},
        ],
    )

    assert_privacy_passthrough(error, gateway)
    assert artifact is not None
    seen = _seen_messages(gateway)
    combined = "".join(item["content"] for item in seen)
    assert "13800" not in combined or combined.count(MASK_CHAR) >= 1
    assert "000000" not in combined or MASK_CHAR in combined


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
async def test_supported_identity_grammar_is_masked_then_proceeds(content: str) -> None:
    gateway = CountingPlainGateway()

    error, artifact = await capture_run(gateway, [{"role": "user", "content": content}])

    assert artifact is not None
    assert_privacy_passthrough(error, gateway)
    seen = _seen_messages(gateway)
    sent_content = seen[0]["content"]
    assert content not in sent_content or MASK_CHAR in sent_content


async def test_scanner_internal_exception_passthrough_without_log_or_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    internal_secret = f"scanner failed near {SYNTHETIC_PHONE}"

    def explode(contents: tuple[str, ...]) -> tuple[str, ...]:
        assert contents == (SYNTHETIC_PHONE,)
        raise ContextBuilderError(internal_secret)

    monkeypatch.setattr(
        runtime_module,
        "project_model_input_identity_sequences",
        explode,
        raising=False,
    )
    gateway = CountingObservedGateway()

    error, artifact = await capture_run(gateway, [{"role": "user", "content": SYNTHETIC_PHONE}])

    # guard 内部崩溃 → 放行原 messages，artifact 仍成功，不阻塞 intake。
    assert_privacy_passthrough(error, gateway)
    assert artifact is not None
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
async def test_invalid_intake_message_content_passes_through(message: dict[str, Any]) -> None:
    gateway = CountingPlainGateway()

    error, artifact = await capture_run(gateway, [message])

    # 输入畸形 → 留痕后放行原 messages，网关仍被调一次。
    assert_privacy_passthrough(error, gateway)
    assert artifact is not None


async def test_message_content_extraction_exception_passes_through() -> None:
    gateway = CountingPlainGateway()
    runtime = AgentRuntime(gateway, recorder=None, gateway_timeout_seconds=0.0)
    # ExplodingContentMapping raises on content access; guard now catches the
    # TypeError, logs an input_shape_invalid incident, and passes the original
    # messages through to the gateway. The gateway double tolerates non-dictable
    # messages when it only counts (no dict() copy); use a counting-only variant.
    messages = cast(list[dict[str, Any]], [ExplodingContentMapping()])

    result = await runtime._call_gateway(make_spec(), make_run(), messages)

    assert result == {"answer": "ok"}
    assert (gateway.observed_calls, gateway.plain_calls, gateway.actual_request_count) == (0, 1, 1)
    assert gateway.max_requests_seen == [1]


async def test_privacy_mask_recorder_events_are_metadata_only() -> None:
    gateway = CountingObservedGateway()
    recorder = MemoryRecorder()
    raw_content = f"synthetic phone {SYNTHETIC_PHONE}"

    error, artifact = await capture_run(
        gateway,
        [{"role": "user", "content": raw_content}],
        recorder=recorder,
    )

    # guard 改软遮罩：放行成功，事件序列为 started/succeeded。
    assert error is None
    assert artifact is not None
    assert [event for event, _ in recorder.events] == ["started", "succeeded"]
    for _, event_data in recorder.events:
        assert "messages" not in event_data
        assert "input" not in event_data
        assert raw_content not in repr(event_data)
        assert SYNTHETIC_PHONE not in repr(event_data)
        assert len(event_data["input_digest"]) == 64


async def test_privacy_mask_is_not_retried_even_when_policy_allows_every_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner_calls = 0

    def counting_scanner(contents: tuple[str, ...]) -> tuple[str, ...]:
        nonlocal scanner_calls
        scanner_calls += 1
        # 不抛、不遮罩：返回原文，模拟"无命中"路径被扫描一次。
        return contents

    monkeypatch.setattr(
        runtime_module,
        "project_model_input_identity_sequences",
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

    # 放行成功，scanner 仅调用 1 次，不需要 retry。
    assert error is None
    assert artifact is not None
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
        # 0d-1: max_tokens 截断显式归因（finish_reason=length），与坏 JSON 区分。
        "MODEL_OUTPUT_TRUNCATED",
        "RUN_DEADLINE_EXCEEDED",
        "ATTEMPT_BUDGET_EXHAUSTED",
        "RECORDER_ASYNC_REQUIRED",
    }
