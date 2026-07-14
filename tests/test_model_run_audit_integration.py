"""PostgreSQL acceptance for minimal, restart-safe model-run auditing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

import app.agent_runtime.runtime as runtime_module
from app.agent_runtime.runtime import AgentRuntime, RuntimeErrorBase
from app.agent_runtime.specs import AgentSpec, ModelPolicy, RunSpec, model_output_digest
from app.core.exceptions import ModelGatewayUnavailableError
from app.core.gateway import ModelTokenUsage, StructuredChatResponse
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.models.model_run_audit import ModelRunAudit
from app.services.model_run_audit import PostgresModelRunRecorder

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


class _Input(BaseModel):
    request: str


class _Output(BaseModel):
    answer: str


class _ObservedGateway:
    async def chat_structured_observed(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> StructuredChatResponse:
        del messages, kwargs
        return StructuredChatResponse(
            output=output_schema.model_validate({"answer": "validated clinical output"}),
            model_actual="provider/served-revision-2026-07",
            usage=ModelTokenUsage(prompt_tokens=29, completion_tokens=11, total_tokens=40),
        )


class _FailingGateway:
    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> BaseModel:
        del messages, output_schema, kwargs
        raise ModelGatewayUnavailableError("sensitive provider detail", retryable=False)


def _spec(agent_name: str) -> AgentSpec:
    return AgentSpec(
        name=agent_name,
        version=f"{agent_name}-spec-v1",
        input_schema=_Input,
        output_schema=_Output,
        model_policy=ModelPolicy(model="requested/model-alias", max_attempts=1),
    )


def _run(session_id: UUID, agent_name: str, *, run_id: UUID | None = None) -> RunSpec:
    return RunSpec(
        run_id=run_id or uuid4(),
        session_id=session_id,
        state_version=1,
        stage=agent_name,
        agent_spec_version=f"{agent_name}-spec-v1",
        prompt_version=f"{agent_name}-prompt-v2",
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        total_attempt_budget=1,
        idempotency_key=f"audit:{agent_name}:{uuid4()}",
        trace_id=f"trace:{agent_name}:{uuid4()}",
    )


async def _seed_session() -> UUID:
    session_id = uuid4()
    async with get_session_factory()() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                current_stage="inquiry",
                status="active",
                agent_runtime="langgraph",
                rollback_counts={},
                state_version=1,
                recovery_status="normal",
            )
        )
    return session_id


async def _cleanup(session_id: UUID) -> None:
    async with get_session_factory()() as db, db.begin():
        session = await db.get(ConsultSession, session_id)
        if session is not None:
            await db.delete(session)


async def test_default_production_runtime_persists_all_four_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session()
    secret_text = "patient Alice has a private clinical narrative and api-key=never-store"
    agents = ("intake", "question", "syndrome", "formula")
    expected: dict[UUID, tuple[str, str]] = {}
    try:
        monkeypatch.setattr(
            runtime_module,
            "ModelGatewayClient",
            lambda *, max_retries: _ObservedGateway(),
        )
        for agent_name in agents:
            run = _run(session_id, agent_name)
            runtime = AgentRuntime()
            assert isinstance(runtime.recorder, PostgresModelRunRecorder)
            artifact = await runtime.run(
                _spec(agent_name),
                run,
                {"request": secret_text},
                [{"role": "user", "content": secret_text}],
            )
            expected[run.run_id] = (agent_name, model_output_digest(artifact.output))

        async with get_session_factory()() as db:
            rows = list(
                (
                    await db.scalars(
                        select(ModelRunAudit)
                        .where(ModelRunAudit.session_id == session_id)
                        .order_by(ModelRunAudit.agent_name)
                    )
                ).all()
            )
        assert {row.agent_name for row in rows} == set(agents)
        assert len(rows) == 4
        for row in rows:
            agent_name, expected_digest = expected[row.run_id]
            assert row.status == "succeeded"
            assert row.agent_name == agent_name
            assert row.agent_spec_version == f"{agent_name}-spec-v1"
            assert row.prompt_version == f"{agent_name}-prompt-v2"
            assert row.output_schema_id.startswith(f"{_Output.__module__}.{_Output.__qualname__}:")
            assert row.model_requested == "requested/model-alias"
            assert row.model_actual == "provider/served-revision-2026-07"
            assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (29, 11, 40)
            assert row.attempts == 1
            assert row.output_digest == expected_digest
            assert row.error_code is None
            values = " ".join(str(getattr(row, column.name)) for column in row.__table__.columns)
            assert "Alice" not in values
            assert "api-key" not in values

        forbidden_columns = {
            "prompt",
            "messages",
            "raw_response",
            "input_payload",
            "output_payload",
            "clinical_text",
        }
        assert forbidden_columns.isdisjoint(ModelRunAudit.__table__.columns.keys())
    finally:
        await _cleanup(session_id)


async def test_failure_is_durable_and_sanitized() -> None:
    session_id = await _seed_session()
    run = _run(session_id, "intake")
    try:
        with pytest.raises(RuntimeErrorBase):
            await AgentRuntime(
                _FailingGateway(),
                PostgresModelRunRecorder(get_session_factory()),
            ).run(
                _spec("intake"),
                run,
                {"request": "private clinical failure input"},
                [{"role": "user", "content": "private clinical failure input"}],
            )

        async with get_session_factory()() as db:
            row = await db.get(ModelRunAudit, run.run_id)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "MODEL_GATEWAY_UNAVAILABLE"
        assert row.output_digest is None
        assert row.model_actual is None
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (0, 0, 0)
        assert "sensitive provider detail" not in " ".join(
            str(getattr(row, column.name)) for column in row.__table__.columns
        )
    finally:
        await _cleanup(session_id)


async def test_new_recorder_instance_cannot_duplicate_or_downgrade_terminal_run() -> None:
    session_id = await _seed_session()
    run = _run(session_id, "formula")
    recorder = PostgresModelRunRecorder(get_session_factory())
    runtime = AgentRuntime(_ObservedGateway(), recorder)
    try:
        artifact = await runtime.run(
            _spec("formula"),
            run,
            {"request": "safe"},
            [{"role": "user", "content": "safe"}],
        )
        started_payload: dict[str, Any] = {
            "run_id": str(run.run_id),
            "session_id": str(session_id),
            "agent_name": "formula",
            "stage": "formula",
            "agent_spec_version": "formula-spec-v1",
            "prompt_version": "formula-prompt-v2",
            "output_schema_id": AgentRuntime._output_schema_id(_Output),
            "model_requested": "requested/model-alias",
            "model_actual": None,
            "attempts": 0,
            "latency_ms": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "output_digest": None,
            "trace_id": run.trace_id,
        }
        restarted_recorder = PostgresModelRunRecorder(get_session_factory())
        await restarted_recorder.record("started", started_payload)

        async with get_session_factory()() as db:
            row = await db.get(ModelRunAudit, run.run_id)
            count = await db.scalar(
                select(func.count()).select_from(ModelRunAudit).where(ModelRunAudit.run_id == run.run_id)
            )
        assert row is not None
        assert count == 1
        assert row.status == "succeeded"
        assert row.model_actual == artifact.model_actual
        assert row.output_digest == model_output_digest(artifact.output)
    finally:
        await _cleanup(session_id)
