"""PostgreSQL acceptance for minimal, restart-safe model-run auditing."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.agent_runtime.runtime as runtime_module
from app.agent_runtime.runtime import AgentRuntime, RuntimeErrorBase
from app.agent_runtime.specs import AgentSpec, ModelPolicy, RunSpec, model_input_digest, model_output_digest
from app.core.exceptions import ModelGatewayUnavailableError
from app.core.gateway import ModelTokenUsage, StructuredChatResponse
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.models.model_run_audit import ModelRunAudit
from app.services.model_run_audit import (
    ModelRunAuditAlreadyFinalizedError,
    ModelRunAuditProvenanceConflictError,
    ModelRunAuditTerminalConflictError,
    PostgresModelRunRecorder,
)

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


class _BlockingGateway:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> BaseModel:
        del messages, output_schema, kwargs
        self.entered.set()
        await self.release.wait()
        raise AssertionError("cancelled model run must not resume")


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
        policy_version=f"{agent_name}-policy-v3",
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        total_attempt_budget=1,
        idempotency_key=f"audit:{agent_name}:{uuid4()}",
        trace_id=f"trace:{agent_name}:{uuid4()}",
    )


_AuditEvent = Literal["succeeded", "failed", "cancelled"]


def _started_audit_payload(run: RunSpec) -> dict[str, Any]:
    return {
        "run_id": str(run.run_id),
        "session_id": str(run.session_id),
        "agent_name": run.stage,
        "stage": run.stage,
        "agent_spec_version": run.agent_spec_version,
        "prompt_version": run.prompt_version,
        "policy_version": run.policy_version,
        "input_digest": "a" * 64,
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
        "error_code": None,
    }


def _terminal_audit_payload(
    run: RunSpec,
    event: _AuditEvent,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = _started_audit_payload(run)
    if event == "succeeded":
        values.update(
            model_actual="provider/served-revision-1",
            attempts=1,
            latency_ms=17,
            prompt_tokens=3,
            completion_tokens=2,
            total_tokens=5,
            output_digest="b" * 64,
        )
    elif event == "failed":
        values.update(
            attempts=1,
            latency_ms=17,
            error_code="MODEL_GATEWAY_UNAVAILABLE",
        )
    else:
        values.update(attempts=1, latency_ms=17)
    values.update(overrides or {})
    return values


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


async def test_00_policy_and_input_digest_migration_round_trip() -> None:
    await asyncio.sleep(0)
    database_url = os.environ["DB_URL"]
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    legacy_session_id = await _seed_session()
    legacy_run_id = uuid4()
    async with get_session_factory()() as db, db.begin():
        db.add(
            ModelRunAudit(
                run_id=legacy_run_id,
                session_id=legacy_session_id,
                agent_name="legacy-audit",
                stage="inquiry",
                agent_spec_version="legacy-spec-v1",
                prompt_version="legacy-prompt-v1",
                policy_version="pre-migration-policy",
                input_digest="a" * 64,
                output_schema_id="tests.LegacyOutput:deadbeef",
                model_requested="legacy-model",
                model_actual=None,
                status="started",
                attempts=0,
                latency_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                output_digest=None,
                error_code=None,
                trace_id="legacy-migration-test",
            )
        )
    try:
        command.downgrade(config, "20260715_0010")
        with psycopg.connect(database_url) as connection:
            legacy_columns = {
                row[0]
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'model_run_audits'"
                )
            }
        assert {"policy_version", "input_digest"}.isdisjoint(legacy_columns)

        command.upgrade(config, "head")
        with psycopg.connect(database_url) as connection:
            upgraded_columns = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'model_run_audits'"
                )
            }
            constraints = {
                row[0]
                for row in connection.execute(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'model_run_audits'::regclass"
                )
            }
            legacy_provenance = connection.execute(
                "SELECT policy_version, input_digest FROM model_run_audits WHERE run_id = %s",
                (legacy_run_id,),
            ).fetchone()
        assert upgraded_columns["policy_version"] == "NO"
        assert upgraded_columns["input_digest"] == "NO"
        assert any(name.endswith("chk_model_run_audits_input_digest") for name in constraints)
        assert legacy_provenance == (
            "pre-input-provenance-unavailable.v1",
            hashlib.sha256(f"xuanhu:model-input:unavailable:v1:{legacy_run_id}".encode()).hexdigest(),
        )
    finally:
        command.upgrade(config, "head")
        await _cleanup(legacy_session_id)


async def test_default_production_runtime_persists_all_four_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session()
    secret_text = "patient Alice has a private clinical narrative and api-key=never-store"
    agents = ("intake", "question", "syndrome", "formula")
    expected: dict[UUID, tuple[str, str, str]] = {}
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
            expected[run.run_id] = (
                agent_name,
                model_input_digest(
                    _Input(request=secret_text),
                    [{"role": "user", "content": secret_text}],
                ),
                model_output_digest(artifact.output),
            )

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
            agent_name, expected_input_digest, expected_output_digest = expected[row.run_id]
            assert row.status == "succeeded"
            assert row.agent_name == agent_name
            assert row.agent_spec_version == f"{agent_name}-spec-v1"
            assert row.prompt_version == f"{agent_name}-prompt-v2"
            assert row.policy_version == f"{agent_name}-policy-v3"
            assert row.input_digest == expected_input_digest
            assert row.output_schema_id.startswith(f"{_Output.__module__}.{_Output.__qualname__}:")
            assert row.model_requested == "requested/model-alias"
            assert row.model_actual == "provider/served-revision-2026-07"
            assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (29, 11, 40)
            assert row.attempts == 1
            assert row.output_digest == expected_output_digest
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
        assert row.policy_version == "intake-policy-v3"
        assert row.input_digest == model_input_digest(
            _Input(request="private clinical failure input"),
            [{"role": "user", "content": "private clinical failure input"}],
        )
        assert row.error_code == "MODEL_GATEWAY_UNAVAILABLE"
        assert row.output_digest is None
        assert row.model_actual is None
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (0, 0, 0)
        assert "sensitive provider detail" not in " ".join(
            str(getattr(row, column.name)) for column in row.__table__.columns
        )
    finally:
        await _cleanup(session_id)


async def test_cancellation_is_durable_with_the_same_input_provenance() -> None:
    session_id = await _seed_session()
    run = _run(session_id, "question")
    gateway = _BlockingGateway()
    task = asyncio.create_task(
        AgentRuntime(gateway, PostgresModelRunRecorder(get_session_factory())).run(
            _spec("question"),
            run,
            {"request": "private cancellation input"},
            [{"role": "user", "content": "private cancellation input"}],
        )
    )
    try:
        await gateway.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with get_session_factory()() as db:
            row = await db.get(ModelRunAudit, run.run_id)
        assert row is not None
        assert row.status == "cancelled"
        assert row.policy_version == "question-policy-v3"
        assert row.input_digest == model_input_digest(
            _Input(request="private cancellation input"),
            [{"role": "user", "content": "private cancellation input"}],
        )
        assert row.output_digest is None
        assert row.error_code is None
        assert "private cancellation input" not in " ".join(
            str(getattr(row, column.name)) for column in row.__table__.columns
        )
    finally:
        gateway.release.set()
        if not task.done():
            task.cancel()
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
            "policy_version": "forged-policy-must-not-replace-terminal",
            "input_digest": "f" * 64,
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
        with pytest.raises(
            ModelRunAuditProvenanceConflictError,
            match="provenance conflict",
        ):
            await restarted_recorder.record("started", started_payload)

        async with get_session_factory()() as db:
            row = await db.get(ModelRunAudit, run.run_id)
            count = await db.scalar(
                select(func.count()).select_from(ModelRunAudit).where(ModelRunAudit.run_id == run.run_id)
            )
        assert row is not None
        assert count == 1
        assert row.status == "succeeded"
        assert row.policy_version == run.policy_version
        assert row.input_digest == model_input_digest(
            _Input(request="safe"),
            [{"role": "user", "content": "safe"}],
        )
        assert row.model_actual == artifact.model_actual
        assert row.output_digest == model_output_digest(artifact.output)
    finally:
        await _cleanup(session_id)


@pytest.mark.parametrize("event", ["succeeded", "failed", "cancelled"])
async def test_identical_concurrent_terminal_replay_is_idempotent(
    event: _AuditEvent,
) -> None:
    session_id = await _seed_session()
    run = _run(session_id, "terminal-replay")
    recorder = PostgresModelRunRecorder(get_session_factory())
    started = _started_audit_payload(run)
    terminal = _terminal_audit_payload(run, event)
    try:
        await recorder.record("started", started)

        results = await asyncio.gather(
            recorder.record(event, dict(terminal)),
            recorder.record(event, dict(terminal)),
            return_exceptions=True,
        )

        assert all(result is None for result in results)
        async with get_session_factory()() as db:
            row = await db.get(ModelRunAudit, run.run_id)
            count = await db.scalar(
                select(func.count()).select_from(ModelRunAudit).where(ModelRunAudit.run_id == run.run_id)
            )
        assert row is not None
        assert count == 1
        assert row.status == event
        assert row.model_actual == terminal["model_actual"]
        assert row.output_digest == terminal["output_digest"]
        assert row.error_code == terminal["error_code"]
        assert (
            row.prompt_tokens,
            row.completion_tokens,
            row.total_tokens,
        ) == (
            terminal["prompt_tokens"],
            terminal["completion_tokens"],
            terminal["total_tokens"],
        )
    finally:
        await _cleanup(session_id)


async def test_started_replay_of_terminal_run_fails_before_new_execution() -> None:
    session_id = await _seed_session()
    run = _run(session_id, "already-finalized")
    recorder = PostgresModelRunRecorder(get_session_factory())
    started = _started_audit_payload(run)
    terminal = _terminal_audit_payload(run, "succeeded")
    try:
        await recorder.record("started", started)
        await recorder.record("succeeded", terminal)

        with pytest.raises(
            ModelRunAuditAlreadyFinalizedError,
            match="^model-run audit already finalized$",
        ):
            await recorder.record("started", started)

        async with get_session_factory()() as db:
            row = await db.get(ModelRunAudit, run.run_id)
        assert row is not None
        assert row.status == "succeeded"
        assert row.output_digest == terminal["output_digest"]
    finally:
        await _cleanup(session_id)


@pytest.mark.parametrize(
    (
        "first_event",
        "first_overrides",
        "second_event",
        "second_overrides",
    ),
    [
        ("succeeded", {}, "failed", {}),
        (
            "succeeded",
            {"output_digest": "c" * 64},
            "succeeded",
            {"output_digest": "d" * 64},
        ),
        (
            "succeeded",
            {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "succeeded",
            {"prompt_tokens": 6, "completion_tokens": 3, "total_tokens": 9},
        ),
        (
            "succeeded",
            {"model_actual": "provider/private-revision-alpha"},
            "succeeded",
            {"model_actual": "provider/private-revision-beta"},
        ),
        (
            "failed",
            {"error_code": "PRIVATE_PROVIDER_FAILURE_ALPHA"},
            "failed",
            {"error_code": "PRIVATE_PROVIDER_FAILURE_BETA"},
        ),
    ],
)
async def test_conflicting_concurrent_terminal_replay_fails_closed(
    first_event: _AuditEvent,
    first_overrides: dict[str, Any],
    second_event: _AuditEvent,
    second_overrides: dict[str, Any],
) -> None:
    session_id = await _seed_session()
    run = _run(session_id, "terminal-conflict")
    recorder = PostgresModelRunRecorder(get_session_factory())
    first = _terminal_audit_payload(run, first_event, first_overrides)
    second = _terminal_audit_payload(run, second_event, second_overrides)
    try:
        await recorder.record("started", _started_audit_payload(run))

        results = await asyncio.gather(
            recorder.record(first_event, first),
            recorder.record(second_event, second),
            return_exceptions=True,
        )

        conflicts = [result for result in results if isinstance(result, ModelRunAuditTerminalConflictError)]
        assert len(conflicts) == 1
        assert sum(result is None for result in results) == 1
        assert str(conflicts[0]) == "model-run audit terminal conflict"
        assert "PRIVATE_PROVIDER" not in str(conflicts[0])
        assert "private-revision" not in str(conflicts[0])
        async with get_session_factory()() as db:
            row = await db.get(ModelRunAudit, run.run_id)
            count = await db.scalar(
                select(func.count()).select_from(ModelRunAudit).where(ModelRunAudit.run_id == run.run_id)
            )
        assert row is not None
        assert count == 1
        assert row.status in {first_event, second_event}
    finally:
        await _cleanup(session_id)


async def test_database_rejects_non_sha256_input_digest() -> None:
    session_id = await _seed_session()
    try:
        with pytest.raises(IntegrityError):
            async with get_session_factory()() as db, db.begin():
                db.add(
                    ModelRunAudit(
                        run_id=uuid4(),
                        session_id=session_id,
                        agent_name="intake",
                        stage="inquiry",
                        agent_spec_version="intake-spec-v1",
                        prompt_version="intake-prompt-v1",
                        policy_version="intake-policy-v1",
                        input_digest="not-a-sha256-digest",
                        output_schema_id="tests.Output:deadbeef",
                        model_requested="requested-model",
                        model_actual=None,
                        status="started",
                        attempts=0,
                        latency_ms=0,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        output_digest=None,
                        error_code=None,
                        trace_id="constraint-test",
                    )
                )
                await db.flush()
    finally:
        await _cleanup(session_id)
