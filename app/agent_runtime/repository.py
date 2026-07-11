"""PostgreSQL Domain Repository and transactional Outbox for L2-5."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_runtime.reducer import (
    DomainDelta,
    DomainReducerError,
    DomainState,
    domain_delta_digest,
    reduce_domain_state,
)
from app.agent_runtime.verifiers import VerificationContext
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    ArtifactRevision,
    DomainCommandCommit,
    GateResult,
    GraphRun,
    GraphRunStep,
    Observation,
    OutboxEvent,
    SafetyProfile,
)
from app.schemas.domain import ArtifactRevisionSchema, GateResultSchema, ObservationSchema, SafetyProfileSchema

DOMAIN_STATE_COMMITTED = "domain.state_committed.v1"
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class RepositoryErrorCode(StrEnum):
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    ARTIFACT_PARENT_INVALID = "ARTIFACT_PARENT_INVALID"
    UNSAFE_METADATA = "UNSAFE_METADATA"
    TRANSACTION_FAILED = "TRANSACTION_FAILED"


class RepositoryError(RuntimeError):
    """A payload-free repository failure with a stable code."""

    def __init__(self, code: RepositoryErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class OutboxErrorCode(StrEnum):
    PUBLISH_TIMEOUT = "PUBLISH_TIMEOUT"
    PUBLISH_UNAVAILABLE = "PUBLISH_UNAVAILABLE"
    PUBLISH_REJECTED = "PUBLISH_REJECTED"
    PUBLISH_UNKNOWN = "PUBLISH_UNKNOWN"


class CommitResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_id: UUID
    session_id: UUID
    graph_run_id: UUID
    outbox_event_id: UUID
    input_state_version: int = Field(ge=1)
    output_state_version: int = Field(ge=1)
    changed: bool


class GraphStepSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step_name: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern=r"^(started|completed|failed|skipped)$")
    metadata: dict[str, object] | None = None


class ConsultMessageSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: UUID
    role: str = Field(pattern=r"^(doctor|patient_proxy|agent|system)$")
    stage: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=5000)
    agent_name: str | None = Field(default=None, max_length=32)
    structured_delta: dict[str, object] | None = None
    trace_id: str | None = Field(default=None, max_length=64)


class OutboxMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    event_type: str
    session_id: UUID
    graph_run_id: UUID
    state_version: int = Field(ge=1)
    trace_id: str
    payload: dict[str, object]
    status: str
    attempt_count: int = Field(ge=0)
    leased_by: str | None


class DomainRepository(Protocol):
    async def get_state(self, session_id: UUID) -> DomainState: ...

    async def commit(
        self,
        delta: DomainDelta,
        context: VerificationContext,
        *,
        graph_version: str,
    ) -> CommitResult: ...


class OutboxRepository(Protocol):
    async def claim(self, *, worker_id: str, limit: int, lease_seconds: int) -> tuple[OutboxMessage, ...]: ...

    async def acknowledge(self, event_id: UUID, *, worker_id: str) -> bool: ...

    async def release_failed(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
        retry_after_seconds: int,
    ) -> bool: ...


class PostgresDomainRepository(DomainRepository, OutboxRepository):
    """Async SQLAlchemy repository whose correctness relies on PostgreSQL locks."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_state(self, session_id: UUID) -> DomainState:
        async with self._session_factory() as session:
            session_row = await session.get(ConsultSession, session_id)
            if session_row is None:
                raise RepositoryError(RepositoryErrorCode.SESSION_NOT_FOUND)
            return await self._load_state(session, session_row)

    async def commit(
        self,
        delta: DomainDelta,
        context: VerificationContext,
        *,
        graph_version: str,
        gate_results: Sequence[GateResultSchema] = (),
        graph_steps: Sequence[GraphStepSpec] = (),
        consult_messages: Sequence[ConsultMessageSpec] = (),
        session_updates: dict[str, object] | None = None,
        outbox_event_type: str = DOMAIN_STATE_COMMITTED,
        outbox_payload: dict[str, object] | None = None,
    ) -> CommitResult:
        self._validate_metadata(context, graph_version)
        digest = domain_delta_digest(delta)
        idempotency_ref = self._stable_ref("command", context.run_spec.idempotency_key)
        try:
            async with self._session_factory() as session, session.begin():
                locked = await session.scalar(
                    select(ConsultSession).where(ConsultSession.id == delta.session_id).with_for_update()
                )
                if locked is None:
                    raise RepositoryError(RepositoryErrorCode.SESSION_NOT_FOUND)

                existing = await session.scalar(
                    select(DomainCommandCommit).where(
                        DomainCommandCommit.session_id == delta.session_id,
                        DomainCommandCommit.idempotency_key == idempotency_ref,
                        DomainCommandCommit.input_state_version == delta.expected_state_version,
                        DomainCommandCommit.agent_spec_version == context.agent_spec.version,
                    )
                )
                if existing is not None:
                    if existing.delta_digest != digest:
                        raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
                    return self._commit_result(existing)

                if locked.state_version != delta.expected_state_version:
                    raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)

                state = await self._load_state(session, locked)
                next_state = reduce_domain_state(state, delta, context)
                await self._persist_state(session, state, next_state, delta)
                locked.state_version = next_state.state_version

                with session.no_autoflush:
                    graph_run = await session.get(GraphRun, delta.run_id)
                if graph_run is None:
                    graph_run = GraphRun(
                        id=delta.run_id,
                        session_id=delta.session_id,
                        graph_version=graph_version,
                        command_id=idempotency_ref,
                        input_state_version=delta.expected_state_version,
                        status="completed",
                        completed_at=func.now(),
                    )
                    session.add(graph_run)
                    await session.flush([graph_run])
                else:
                    graph_run.status = "completed"
                    graph_run.completed_at = func.now()
                session.add(
                    GraphRunStep(
                        id=uuid4(),
                        graph_run_id=delta.run_id,
                        step_index=0,
                        step_name="domain_commit",
                        status="completed",
                        step_metadata={"state_version": next_state.state_version},
                    )
                )
                for index, step in enumerate(graph_steps, start=1):
                    session.add(
                        GraphRunStep(
                            id=uuid4(),
                            graph_run_id=delta.run_id,
                            step_index=index,
                            step_name=step.step_name,
                            status=step.status,
                            step_metadata=step.metadata,
                        )
                    )
                session.add(
                    GateResult(
                        id=uuid4(),
                        session_id=delta.session_id,
                        graph_run_id=delta.run_id,
                        gate_name="canonical_verifier_chain",
                        policy_version="l2-4-v1",
                        input_state_version=delta.expected_state_version,
                        decision="passed",
                        details={"subject_digest": digest},
                    )
                )
                for gate in gate_results:
                    session.add(
                        GateResult(
                            id=uuid4(),
                            session_id=delta.session_id,
                            graph_run_id=delta.run_id,
                            gate_name=gate.gate_name,
                            policy_version=gate.policy_version,
                            input_state_version=gate.input_state_version,
                            decision=gate.decision.value,
                            details=gate.details,
                        )
                    )
                for message in consult_messages:
                    session.add(
                        ConsultMessage(
                            id=message.message_id,
                            session_id=delta.session_id,
                            role=message.role,
                            stage=message.stage,
                            agent_name=message.agent_name,
                            content=message.content,
                            structured_delta=message.structured_delta,
                            trace_id=message.trace_id,
                        )
                    )
                if session_updates:
                    self._apply_session_updates(locked, session_updates)
                # Explicit flush phases make FK ordering unambiguous without
                # introducing ORM relationships into the persistence contract.
                # All phases remain inside this one transaction.
                await session.flush()

                outbox_id = uuid4()
                session.add(
                    OutboxEvent(
                        id=outbox_id,
                        event_type=outbox_event_type,
                        session_id=delta.session_id,
                        graph_run_id=delta.run_id,
                        state_version=next_state.state_version,
                        trace_id=self._stable_ref("trace", context.run_spec.trace_id),
                        payload=outbox_payload or self._event_payload(delta, next_state),
                        status="pending",
                        attempt_count=0,
                    )
                )
                await session.flush()
                commit_row = DomainCommandCommit(
                    id=uuid4(),
                    session_id=delta.session_id,
                    idempotency_key=idempotency_ref,
                    input_state_version=delta.expected_state_version,
                    agent_spec_version=context.agent_spec.version,
                    delta_digest=digest,
                    output_state_version=next_state.state_version,
                    changed=next_state.state_version != state.state_version,
                    graph_run_id=delta.run_id,
                    outbox_event_id=outbox_id,
                )
                session.add(commit_row)
                await session.flush()
                return self._commit_result(commit_row)
        except (RepositoryError, DomainReducerError):
            raise
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def get_outbox(self, event_id: UUID) -> OutboxMessage | None:
        async with self._session_factory() as session:
            row = await session.get(OutboxEvent, event_id)
            return None if row is None else self._outbox_message(row)

    async def claim(self, *, worker_id: str, limit: int, lease_seconds: int) -> tuple[OutboxMessage, ...]:
        self._validate_worker(worker_id)
        if limit < 1 or lease_seconds < 1:
            raise ValueError("limit and lease_seconds must be positive")
        try:
            async with self._session_factory() as session, session.begin():
                now = func.now()
                rows = (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            or_(
                                and_(OutboxEvent.status == "pending", OutboxEvent.available_at <= now),
                                and_(OutboxEvent.status == "leased", OutboxEvent.leased_until <= now),
                            )
                        )
                        .order_by(OutboxEvent.created_at, OutboxEvent.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.status = "leased"
                    row.leased_by = worker_id
                    row.leased_until = func.now() + timedelta(seconds=lease_seconds)
                    row.attempt_count += 1
                await session.flush()
                return tuple(self._outbox_message(row) for row in rows)
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def acknowledge(self, event_id: UUID, *, worker_id: str) -> bool:
        self._validate_worker(worker_id)
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event_id,
                        OutboxEvent.status == "leased",
                        OutboxEvent.leased_by == worker_id,
                    )
                    .values(
                        status="published",
                        leased_by=None,
                        leased_until=None,
                        last_error_code=None,
                        published_at=func.now(),
                    )
                    .returning(OutboxEvent.id)
                )
                return result.scalar_one_or_none() is not None
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def release_failed(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
        retry_after_seconds: int,
    ) -> bool:
        self._validate_worker(worker_id)
        if not isinstance(error_code, OutboxErrorCode):
            raise TypeError("error_code must be OutboxErrorCode")
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event_id,
                        OutboxEvent.status == "leased",
                        OutboxEvent.leased_by == worker_id,
                    )
                    .values(
                        status="pending",
                        leased_by=None,
                        leased_until=None,
                        last_error_code=error_code.value,
                        available_at=func.now() + timedelta(seconds=retry_after_seconds),
                    )
                    .returning(OutboxEvent.id)
                )
                return result.scalar_one_or_none() is not None
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def _load_state(self, session: AsyncSession, session_row: ConsultSession) -> DomainState:
        observations = (
            await session.scalars(
                select(Observation)
                .where(Observation.session_id == session_row.id)
                .order_by(Observation.created_at, Observation.id)
            )
        ).all()
        safety = await session.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_row.id))
        artifacts = (
            await session.scalars(
                select(ArtifactRevision)
                .where(ArtifactRevision.session_id == session_row.id)
                .order_by(ArtifactRevision.artifact_id, ArtifactRevision.revision)
            )
        ).all()
        return DomainState(
            session_id=session_row.id,
            state_version=session_row.state_version,
            observations=tuple(self._observation_schema(row) for row in observations),
            safety_profile=None if safety is None else self._safety_schema(safety),
            artifacts=tuple(self._artifact_schema(row) for row in artifacts),
        )

    async def _persist_state(
        self,
        session: AsyncSession,
        previous: DomainState,
        current: DomainState,
        delta: DomainDelta,
    ) -> None:
        previous_observations = {item.observation_id for item in previous.observations}
        for observation_item in current.observations:
            if observation_item.observation_id not in previous_observations:
                session.add(
                    Observation(
                        id=observation_item.observation_id,
                        session_id=observation_item.session_id,
                        fact_key=observation_item.fact_key,
                        value=observation_item.value,
                        normalized_value=observation_item.normalized_value,
                        source_message_id=observation_item.source_message_id,
                        status=observation_item.status.value,
                        confidence=observation_item.confidence,
                        supersedes_observation_id=observation_item.supersedes_observation_id,
                        created_at=observation_item.created_at,
                    )
                )

        if delta.safety_profile is not None:
            safety_row = await session.scalar(
                select(SafetyProfile).where(SafetyProfile.session_id == current.session_id)
            )
            values = self._safety_values(delta.safety_profile)
            if safety_row is None:
                session.add(SafetyProfile(id=uuid4(), session_id=current.session_id, **values))
            else:
                for name, value in values.items():
                    setattr(safety_row, name, value)

        rows = (
            await session.scalars(select(ArtifactRevision).where(ArtifactRevision.session_id == current.session_id))
        ).all()
        by_key = {(artifact_row.artifact_id, artifact_row.revision): artifact_row for artifact_row in rows}
        next_by_key = {
            (artifact_item.artifact_id, artifact_item.revision): artifact_item for artifact_item in current.artifacts
        }
        for key, artifact_row in by_key.items():
            artifact_row.status = next_by_key[key].status.value

        incoming_keys = {
            (artifact_item.artifact_id, artifact_item.revision) for artifact_item in delta.artifact_revisions
        }
        for artifact_item in current.artifacts:
            key = (artifact_item.artifact_id, artifact_item.revision)
            if key not in incoming_keys or key in by_key:
                continue
            if artifact_item.revision > 1:
                parent = by_key.get((artifact_item.artifact_id, artifact_item.revision - 1))
                if (
                    parent is None
                    or artifact_item.parent_revision_id != parent.id
                    or parent.session_id != artifact_item.session_id
                ):
                    raise RepositoryError(RepositoryErrorCode.ARTIFACT_PARENT_INVALID)
            session.add(
                ArtifactRevision(
                    id=uuid4(),
                    artifact_id=artifact_item.artifact_id,
                    artifact_type=artifact_item.artifact_type,
                    revision=artifact_item.revision,
                    session_id=artifact_item.session_id,
                    input_state_version=artifact_item.input_state_version,
                    status=artifact_item.status.value,
                    produced_by_run_id=artifact_item.produced_by_run_id,
                    parent_revision_id=artifact_item.parent_revision_id,
                    parent_revision=artifact_item.parent_revision,
                    created_at=artifact_item.created_at,
                )
            )

    @staticmethod
    def _observation_schema(row: Observation) -> ObservationSchema:
        return ObservationSchema.model_validate(
            {
                "observation_id": row.id,
                "session_id": row.session_id,
                "fact_key": row.fact_key,
                "value": row.value,
                "normalized_value": row.normalized_value,
                "source_message_id": row.source_message_id,
                "status": row.status,
                "confidence": row.confidence,
                "supersedes_observation_id": row.supersedes_observation_id,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    def _safety_schema(row: SafetyProfile) -> SafetyProfileSchema:
        return SafetyProfileSchema.model_validate(
            {"session_id": row.session_id, **PostgresDomainRepository._safety_values(row)}
        )

    @staticmethod
    def _safety_values(profile: SafetyProfileSchema | SafetyProfile) -> dict[str, object]:
        names = (
            "allergy_collection_status",
            "allergens",
            "pregnancy_collection_status",
            "pregnancy_value",
            "lactation_collection_status",
            "lactation_value",
            "medications_collection_status",
            "medications",
            "major_conditions_collection_status",
            "major_conditions",
            "contraindications_collection_status",
            "contraindications",
        )
        return {
            name: value.value if isinstance((value := getattr(profile, name)), StrEnum) else value for name in names
        }

    @staticmethod
    def _artifact_schema(row: ArtifactRevision) -> ArtifactRevisionSchema:
        return ArtifactRevisionSchema.model_validate(
            {
                "artifact_id": row.artifact_id,
                "artifact_type": row.artifact_type,
                "revision": row.revision,
                "session_id": row.session_id,
                "input_state_version": row.input_state_version,
                "status": row.status,
                "produced_by_run_id": row.produced_by_run_id,
                "parent_revision_id": row.parent_revision_id,
                "parent_revision": row.parent_revision,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    def _event_payload(delta: DomainDelta, state: DomainState) -> dict[str, object]:
        return {
            "session_id": str(delta.session_id),
            "input_state_version": delta.expected_state_version,
            "output_state_version": state.state_version,
            "observation_ids": [str(item.observation_id) for item in delta.observations],
            "artifact_ids": sorted({str(item.artifact_id) for item in delta.artifact_revisions}),
        }

    @staticmethod
    def _apply_session_updates(session_row: ConsultSession, updates: dict[str, object]) -> None:
        allowed = {
            "current_stage",
            "status",
            "recovery_status",
            "blocked_reason",
            "blocked_at",
            "state_snapshot",
        }
        if set(updates) - allowed:
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
        for name, value in updates.items():
            setattr(session_row, name, value)

    @staticmethod
    def _commit_result(row: DomainCommandCommit) -> CommitResult:
        return CommitResult(
            commit_id=row.id,
            session_id=row.session_id,
            graph_run_id=row.graph_run_id,
            outbox_event_id=row.outbox_event_id,
            input_state_version=row.input_state_version,
            output_state_version=row.output_state_version,
            changed=row.changed,
        )

    @staticmethod
    def _outbox_message(row: OutboxEvent) -> OutboxMessage:
        return OutboxMessage(
            event_id=row.id,
            event_type=row.event_type,
            session_id=row.session_id,
            graph_run_id=row.graph_run_id,
            state_version=row.state_version,
            trace_id=row.trace_id,
            payload=dict(row.payload),
            status=row.status,
            attempt_count=row.attempt_count,
            leased_by=row.leased_by,
        )

    @staticmethod
    def _validate_metadata(context: VerificationContext, graph_version: str) -> None:
        refs: Sequence[tuple[str, int]] = (
            (graph_version, 64),
            (context.agent_spec.version, 100),
        )
        if any(len(value) > maximum or _SAFE_REF.fullmatch(value) is None for value, maximum in refs):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

    @staticmethod
    def _stable_ref(kind: str, value: str) -> str:
        return f"{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _validate_worker(worker_id: str) -> None:
        if len(worker_id) > 128 or _SAFE_REF.fullmatch(worker_id) is None:
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
