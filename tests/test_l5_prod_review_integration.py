from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.formula_consistency import FORMULA_CONSISTENCY_POLICY_VERSION
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.reducer import DomainDelta
from app.agent_runtime.repository import (
    ArtifactPayloadSpec,
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
    artifact_payload_digest,
)
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.state import default_state
from app.core.config import get_settings
from app.core.exceptions import ModelGatewayUnavailableError, SafetyReviewBlockedError
from app.db.session import get_session_factory
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.domain import (
    ArtifactRevision,
    GateResult,
    GraphRun,
    IntakeCommandClaim,
    SafetyProfile,
)
from app.models.http_command import HttpCommandClaim
from app.models.knowledge import DosageUnit, Herb
from app.models.review import DoctorReview
from app.models.safety import SafetyRuleRun
from app.schemas.domain import ArtifactRevisionSchema, ArtifactStatus, GateDecision, GateResultSchema
from app.schemas.formula import (
    FORMULA_EVIDENCE_MODE,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaFactClaim,
    HerbItem,
)
from app.schemas.review import FormulaOverride, HerbOverrideItem, ReviewRequest
from app.services.langgraph_review import (
    DOCTOR_REVIEW_ARTIFACT_TYPE,
    FORMULA_ARTIFACT_TYPE,
    FORMULA_PAYLOAD_SCHEMA_VERSION,
    REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE,
    SAFETY_ARTIFACT_TYPE,
    SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE,
    LangGraphReviewService,
    _session_updates,
    _verification_context,
    prepare_review_interrupt,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@dataclass(frozen=True, slots=True)
class _SeededReview:
    session_id: uuid.UUID
    command_id: str
    run_id: uuid.UUID
    herb_name: str
    unit_name: str


def _draft(herb_name: str, unit_name: str) -> FormulaDraft:
    fact_id = uuid.uuid4()
    basis = (FormulaFactClaim(claim="integration authority", fact_ids=(fact_id,)),)
    formula = FormulaComposition(
        name="integration formula",
        composition=(HerbItem(herb=herb_name, dose=3, unit=unit_name),),
        rationale="integration-only deterministic formula",
        basis=basis,
    )
    return FormulaDraft(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=formula,
        candidate_formula=formula,
        rationale="integration-only deterministic formula",
        confidence=0.5,
        evidence_mode=FORMULA_EVIDENCE_MODE,
        review_required=True,
    )


async def _seed_safety_stage(*, create_claim: bool = True) -> _SeededReview:
    factory = get_session_factory()
    session_id = uuid.uuid4()
    suffix = session_id.hex[:6]
    herb_name = f"testherb{suffix}"
    unit_name = f"u{suffix}"
    async with factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={"name": "integration-only"},
                chief_complaint="integration-only",
                current_stage="prescription",
                status="active",
                agent_runtime="langgraph",
                pending_review=False,
                rollback_counts={},
                state_snapshot={"agent_runtime": "langgraph"},
                state_version=1,
                recovery_status="normal",
            )
        )
        db.add(
            SafetyProfile(
                id=uuid.uuid4(),
                session_id=session_id,
                allergy_collection_status="explicitly_none",
                allergens=None,
                pregnancy_collection_status="explicitly_none",
                pregnancy_value=None,
                lactation_collection_status="explicitly_none",
                lactation_value=None,
                medications_collection_status="explicitly_none",
                medications=None,
                major_conditions_collection_status="explicitly_none",
                major_conditions=None,
                contraindications_collection_status="explicitly_none",
                contraindications=None,
            )
        )
        db.add(
            Herb(
                id=uuid.uuid4(),
                name=herb_name,
                aliases=[],
                meridians=[],
                contraindications=[],
                eighteen_incompatibilities=[],
                nineteen_fears=[],
                pregnancy_contraindication="none",
                incompatibilities=[],
                max_dose=30,
                doc_text="integration-only herb",
            )
        )
        db.add(
            DosageUnit(
                id=uuid.uuid4(),
                unit_name=unit_name,
                aliases=[],
                to_grams=1,
                conversion_type="standard",
                is_standard=True,
                enabled=True,
            )
        )

    repository = PostgresDomainRepository(factory)
    state = await repository.get_state(session_id)
    formula_run_id = uuid.uuid4()
    artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{FORMULA_ARTIFACT_TYPE}:{session_id}")
    artifact = ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type=FORMULA_ARTIFACT_TYPE,
        revision=1,
        session_id=session_id,
        input_state_version=state.state_version,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=formula_run_id,
        created_at=datetime.now(UTC),
    )
    draft = _draft(herb_name, unit_name)
    payload: dict[str, object] = {
        "kind": FORMULA_ARTIFACT_TYPE,
        "output": draft.model_dump(mode="json"),
        "input_payload": {"state_version": state.state_version},
        "run_spec": {},
        "run_artifact": {},
        "verification": {"passed": True},
        "consistency": {"passed": True},
    }
    digest = artifact_payload_digest(FORMULA_PAYLOAD_SCHEMA_VERSION, payload)
    delta = DomainDelta(
        delta_id=uuid.uuid4(),
        run_id=formula_run_id,
        session_id=session_id,
        expected_state_version=state.state_version,
        artifact_revisions=(artifact,),
    )
    await repository.commit(
        delta,
        _verification_context(
            delta,
            state,
            stage="formula",
            idempotency_key=f"integration-formula:{session_id}",
            trace_id=f"seed-{suffix}",
        ),
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(
            GateResultSchema(
                gate_name="formula_consistency",
                policy_version=FORMULA_CONSISTENCY_POLICY_VERSION,
                input_state_version=state.state_version,
                decision=GateDecision.PASSED,
                details={"artifact_digest": digest},
            ),
        ),
        artifact_payloads=(
            ArtifactPayloadSpec(
                session_id=session_id,
                artifact_id=artifact_id,
                revision=1,
                payload_schema_version=FORMULA_PAYLOAD_SCHEMA_VERSION,
                payload=payload,
                content_digest=digest,
            ),
        ),
        session_updates=_session_updates(
            current_stage="safety",
            status="active",
            pending_review=False,
            state_version=state.state_version + 1,
            route="ready_for_safety",
        ),
        outbox_event_type="integration.formula_ready.v1",
        outbox_payload={"session_id": str(session_id)},
    )

    command_id = f"advance:{uuid.uuid4()}"
    run_id = uuid.uuid4()
    if create_claim:
        async with factory() as db, db.begin():
            db.add(
                GraphRun(
                    id=run_id,
                    session_id=session_id,
                    graph_version=DEFAULT_GRAPH_VERSION,
                    command_id=command_id,
                    input_state_version=2,
                    status="running",
                )
            )
            db.add(
                IntakeCommandClaim(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    idempotency_key=command_id,
                    payload_digest="0" * 64,
                    input_state_version=2,
                    status="running",
                    run_id=run_id,
                    intermediate_payload={"advance": {"from_stage": "safety"}},
                )
            )
    return _SeededReview(session_id, command_id, run_id, herb_name, unit_name)


async def _prepare_interrupt(seed: _SeededReview) -> None:
    state = default_state(
        session_id=str(seed.session_id),
        command=XuanhuCommand.REVIEW.value,
        command_id=seed.command_id,
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(seed.run_id),
    )
    state["domain_state_version"] = 2
    config = make_run_config(str(seed.session_id), graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        await GraphRunner(graph, timeout_seconds=30).ainvoke(dict(state), config=config)
        snapshot = await graph.aget_state(config, subgraphs=True)  # type: ignore[arg-type]
        assert snapshot.tasks and snapshot.tasks[0].state is not None
        nested = snapshot.tasks[0].state
        assert not isinstance(nested, dict)
        graph_values = {
            key: value for key, value in nested.values.items() if not key.startswith("__")
        }
        interrupt_values = [item.value for item in nested.values.get("__interrupt__", ())]
        serialized = json.dumps(
            {"state": graph_values, "interrupts": interrupt_values},
            ensure_ascii=False,
        )
        assert seed.herb_name not in serialized
        assert "integration-only" not in serialized
        assert "composition" not in serialized


@pytest.mark.asyncio
async def test_safety_interrupt_survives_restart_and_confirm_resumes_exact_checkpoint() -> None:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        claim = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == seed.session_id)
        )
        assert session is not None and session.current_stage == "review"
        assert session.status == "pending_review" and session.pending_review
        assert session.state_version == 3
        assert claim is not None and claim.status == "completed"
        assert await db.scalar(
            select(func.count()).select_from(SafetyRuleRun).where(SafetyRuleRun.session_id == seed.session_id)
        ) == 1

    # _prepare_interrupt has closed its saver.  The service opens a fresh
    # checkpointer, proving that resume is not process-local memory.
    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            ReviewRequest(action="confirm"),
            doctor_id="integration-doctor",
            trace_id="integration-confirm",
            x_state_version=3,
            idempotency_key=f"confirm:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )
    assert response.current_stage == "record"
    assert response.pending_review is False
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None and session.current_stage == "record"
        assert session.state_version == 5 and not session.pending_review
        assert await db.scalar(
            select(func.count()).select_from(DoctorReview).where(DoctorReview.session_id == seed.session_id)
        ) == 1


@pytest.mark.asyncio
async def test_pending_review_without_checkpoint_repairs_interrupt_fail_closed() -> None:
    seed = await _seed_safety_stage()
    state = default_state(
        session_id=str(seed.session_id),
        command=XuanhuCommand.REVIEW.value,
        command_id=seed.command_id,
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(seed.run_id),
    )
    state["domain_state_version"] = 2

    # Commit the authoritative Safety/pending_review transition directly,
    # deliberately leaving this thread with no LangGraph checkpoint.
    prepared_update = await prepare_review_interrupt(state)
    assert prepared_update["pending_interrupt"] is not None

    factory = get_session_factory()
    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            ReviewRequest(action="confirm"),
            doctor_id="repair-doctor",
            trace_id="repair-no-checkpoint",
            x_state_version=3,
            idempotency_key=f"repair-confirm:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )

    assert response.current_stage == "record"
    assert response.pending_review is False
    repository = PostgresDomainRepository(factory)
    doctor_artifact = await repository.get_artifact_payload(
        seed.session_id,
        artifact_type=DOCTOR_REVIEW_ARTIFACT_TYPE,
        status="current",
    )
    assert doctor_artifact is not None
    assert doctor_artifact.payload["reviewed_by"] == "repair-doctor"
    assert isinstance(doctor_artifact.payload["reviewed_at"], str)
    assert doctor_artifact.payload["feedback"] is None
    assert doctor_artifact.payload["formula_override"] is None
    assert isinstance(doctor_artifact.payload["original_formula"], dict)
    assert isinstance(doctor_artifact.payload["formula_ref"], dict)

    async with factory() as db:
        event_types = set(
            await db.scalars(
                select(AuditEvent.event_type).where(
                    AuditEvent.session_id == seed.session_id,
                )
            )
        )
    assert "langgraph.safety_prepared" in event_types
    assert "langgraph.review_applied" in event_types


@pytest.mark.asyncio
async def test_staged_submission_crash_before_resume_is_idempotently_continued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    idempotency_key = f"crash-before-resume:{uuid.uuid4()}"
    request = ReviewRequest(action="confirm", feedback="same logical request")
    factory = get_session_factory()

    async with factory() as db:
        interrupted_service = LangGraphReviewService(db)

        async def crash_before_resume(**_kwargs: object) -> None:
            raise ModelGatewayUnavailableError("injected post-commit crash")

        monkeypatch.setattr(interrupted_service, "_resume", crash_before_resume)
        with pytest.raises(ModelGatewayUnavailableError):
            await interrupted_service.review(
                str(seed.session_id),
                request,
                doctor_id="crash-retry-doctor",
                trace_id="crash-before-resume",
                x_state_version=3,
                idempotency_key=idempotency_key,
                shared_runtime=None,
                allow_request_local_runtime=True,
            )

    async with factory() as db:
        staged = await db.get(ConsultSession, seed.session_id)
        assert staged is not None and staged.pending_review
        assert staged.state_version == 4

    # Retry with the original client version and idempotency key.  The service
    # must recognize the durable submission before performing version checks.
    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            request,
            doctor_id="crash-retry-doctor",
            trace_id="crash-retry",
            x_state_version=3,
            idempotency_key=idempotency_key,
            shared_runtime=None,
            allow_request_local_runtime=True,
        )

    assert response.current_stage == "record"
    assert response.state_version == 5
    async with factory() as db:
        review_count = await db.scalar(
            select(func.count())
            .select_from(DoctorReview)
            .where(DoctorReview.session_id == seed.session_id)
        )
        assert cast(int, review_count) == 1


@pytest.mark.parametrize("failure_mode", ["failed", "ambiguous"])
async def test_public_review_repairs_failed_or_ambiguous_http_claim_from_durable_submission(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    idempotency_key = f"http-repair-{failure_mode}-{uuid.uuid4()}"
    original_resume = LangGraphReviewService._resume

    async def fail_after_submission(
        self: LangGraphReviewService,
        **_kwargs: object,
    ) -> None:
        del self
        if failure_mode == "failed":
            raise ModelGatewayUnavailableError("injected after durable submission")
        raise RuntimeError("injected ambiguous crash")

    fallback_was_set = hasattr(app.state, "allow_request_local_langgraph_test_runtime")
    fallback_previous = getattr(app.state, "allow_request_local_langgraph_test_runtime", None)
    app.state.allow_request_local_langgraph_test_runtime = True
    monkeypatch.setattr(LangGraphReviewService, "_resume", fail_after_submission)
    headers = {
        "X-Idempotency-Key": idempotency_key,
        "X-State-Version": "3",
        "X-Doctor-Id": "http-repair-doctor",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            first = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/review",
                json={"action": "confirm", "feedback": "durable request"},
                headers=headers,
            )
            assert first.status_code in {500, 503}

            monkeypatch.setattr(LangGraphReviewService, "_resume", original_resume)
            conflict = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/review",
                json={"action": "confirm", "feedback": "forged different request"},
                headers=headers,
            )
            assert conflict.status_code == 409
            replay = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/review",
                json={"action": "confirm", "feedback": "durable request"},
                headers=headers,
            )
    finally:
        monkeypatch.setattr(LangGraphReviewService, "_resume", original_resume)
        if fallback_was_set:
            app.state.allow_request_local_langgraph_test_runtime = fallback_previous
        else:
            delattr(app.state, "allow_request_local_langgraph_test_runtime")

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["current_stage"] == "record"
    factory = get_session_factory()
    async with factory() as db:
        review_count = await db.scalar(
            select(func.count())
            .select_from(DoctorReview)
            .where(DoctorReview.session_id == seed.session_id)
        )
        claims = tuple(
            await db.scalars(
                select(HttpCommandClaim).where(
                    HttpCommandClaim.operation == "session.review.v1",
                    HttpCommandClaim.scope_key == f"session:{seed.session_id}",
                )
            )
        )
    assert cast(int, review_count) == 1
    assert len(claims) == 1
    assert claims[0].status == "completed"
    assert claims[0].http_status == 200


async def test_durable_review_resolver_rejects_tampered_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    idempotency_key = f"tampered-projection:{uuid.uuid4()}"
    request = ReviewRequest(action="confirm", feedback="untampered")
    factory = get_session_factory()

    async with factory() as db:
        interrupted_service = LangGraphReviewService(db)

        async def stop_after_submission(**_kwargs: object) -> None:
            raise ModelGatewayUnavailableError("injected after submission")

        monkeypatch.setattr(interrupted_service, "_resume", stop_after_submission)
        with pytest.raises(ModelGatewayUnavailableError):
            await interrupted_service.review(
                str(seed.session_id),
                request,
                doctor_id="projection-doctor",
                trace_id="projection-stage",
                x_state_version=3,
                idempotency_key=idempotency_key,
                shared_runtime=None,
                allow_request_local_runtime=True,
            )

    async with factory() as db, db.begin():
        projection = await db.scalar(
            select(DoctorReview).where(DoctorReview.session_id == seed.session_id)
        )
        assert projection is not None
        projection.feedback = "forged"

    async with factory() as db:
        resolver = LangGraphReviewService(db)
        with pytest.raises(RepositoryError) as exc_info:
            await resolver.resolve_durable_outcome(
                str(seed.session_id),
                request,
                doctor_id="projection-doctor",
                idempotency_key=idempotency_key,
                shared_runtime=None,
                allow_request_local_runtime=True,
            )
    assert exc_info.value.code is RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED


@pytest.mark.asyncio
async def test_failed_modify_is_auditable_and_can_still_return_for_more_information() -> None:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    factory = get_session_factory()
    async with factory() as db:
        with pytest.raises(SafetyReviewBlockedError):
            await LangGraphReviewService(db).review(
                str(seed.session_id),
                ReviewRequest(
                    action="modify",
                    formula_override=FormulaOverride(
                        name="blocked override",
                        composition=[HerbOverrideItem(herb="unknown-integration-herb", dose=3, unit=seed.unit_name)],
                    ),
                ),
                doctor_id="integration-doctor",
                trace_id="integration-blocked-modify",
                x_state_version=3,
                idempotency_key=f"blocked:{uuid.uuid4()}",
                shared_runtime=None,
                allow_request_local_runtime=True,
            )

    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        current_types = set(
            await db.scalars(
                select(ArtifactRevision.artifact_type).where(
                    ArtifactRevision.session_id == seed.session_id,
                    ArtifactRevision.status == "current",
                )
            )
        )
        assert session is not None and session.current_stage == "review" and session.pending_review
        assert FORMULA_ARTIFACT_TYPE in current_types and SAFETY_ARTIFACT_TYPE in current_types
        assert REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE in current_types
        assert SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE in current_types
        assert await db.scalar(
            select(func.count()).select_from(SafetyRuleRun).where(SafetyRuleRun.session_id == seed.session_id)
        ) == 2
        blocked_version = session.state_version

    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            ReviewRequest(action="request_more_info", feedback="collect one more fact"),
            doctor_id="integration-doctor",
            trace_id="integration-more-info",
            x_state_version=blocked_version,
            idempotency_key=f"more-info:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )
    assert response.current_stage == "inquiry"
    assert response.pending_review is False
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None and session.current_stage == "inquiry"
        assert not session.pending_review
        passed_gate = await db.scalar(
            select(func.count())
            .select_from(GateResult)
            .where(
                GateResult.session_id == seed.session_id,
                GateResult.gate_name == "doctor_review",
                GateResult.decision == "blocked",
            )
        )
        assert cast(int, passed_gate) == 1


@pytest.mark.parametrize(
    ("action", "expected_stage"),
    [
        ("confirm", "record"),
        ("modify", "record"),
        ("reject", "syndrome"),
        ("request_more_info", "inquiry"),
    ],
)
async def test_all_four_review_actions_remain_atomic_and_resumable(
    action: str,
    expected_stage: str,
) -> None:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    request = (
        ReviewRequest(
            action="modify",
            feedback="safe override",
            formula_override=FormulaOverride(
                name="safe override",
                composition=[
                    HerbOverrideItem(
                        herb=seed.herb_name,
                        dose=4,
                        unit=seed.unit_name,
                    )
                ],
            ),
        )
        if action == "modify"
        else ReviewRequest(action=action, feedback=f"{action} integration")
    )
    factory = get_session_factory()
    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            request,
            doctor_id="four-action-doctor",
            trace_id=f"four-action-{action}",
            x_state_version=3,
            idempotency_key=f"four-action:{action}:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )

    assert response.current_stage == expected_stage
    assert response.pending_review is False
    async with factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(DoctorReview)
            .where(DoctorReview.session_id == seed.session_id)
        )
    assert cast(int, count) == 1


async def test_concurrent_review_writers_commit_exactly_one_decision() -> None:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    factory = get_session_factory()

    async def submit(suffix: str) -> object:
        async with factory() as db:
            return await LangGraphReviewService(db).review(
                str(seed.session_id),
                ReviewRequest(action="confirm", feedback=f"writer-{suffix}"),
                doctor_id=f"concurrent-doctor-{suffix}",
                trace_id=f"concurrent-{suffix}",
                x_state_version=3,
                idempotency_key=f"concurrent:{suffix}:{uuid.uuid4()}",
                shared_runtime=None,
                allow_request_local_runtime=True,
            )

    outcomes = await asyncio.gather(submit("a"), submit("b"), return_exceptions=True)
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, BaseException) for item in outcomes) == 1
    async with factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(DoctorReview)
            .where(DoctorReview.session_id == seed.session_id)
        )
    assert cast(int, count) == 1


@pytest.mark.asyncio
async def test_public_review_api_dispatches_langgraph_session_to_checkpoint_resume() -> None:
    seed = await _seed_safety_stage(create_claim=False)
    fallback_was_set = hasattr(app.state, "allow_request_local_langgraph_test_runtime")
    fallback_previous = getattr(app.state, "allow_request_local_langgraph_test_runtime", None)
    app.state.allow_request_local_langgraph_test_runtime = True
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            advance_response = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/advance",
                json={"force": False},
                headers={
                    "X-Idempotency-Key": f"api-advance-{uuid.uuid4()}",
                    "X-State-Version": "2",
                    "X-Doctor-Id": "integration-api-doctor",
                },
            )
            assert advance_response.status_code == 200, advance_response.text
            advance_body = advance_response.json()
            assert advance_body["code"] == "SUCCESS"
            assert advance_body["data"]["current_stage"] == "review"
            assert advance_body["data"]["pending_review"] is True

            response = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/review",
                json={"action": "confirm"},
                headers={
                    "X-Idempotency-Key": f"api-confirm-{uuid.uuid4()}",
                    "X-State-Version": "3",
                    "X-Doctor-Id": "integration-api-doctor",
                },
            )
    finally:
        if fallback_was_set:
            app.state.allow_request_local_langgraph_test_runtime = fallback_previous
        else:
            delattr(app.state, "allow_request_local_langgraph_test_runtime")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "SUCCESS"
    assert body["data"]["current_stage"] == "record"
    assert body["data"]["pending_review"] is False
