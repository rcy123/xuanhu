"""Create the first LangGraph intake question inside the session transaction.

The create-session path already owns a deterministic, clinical-only
``InitialDomainSeed``.  This module projects that seed through the same triage,
completeness, gap-selection, and template question contracts used by later
intake turns, without inventing a duplicate patient message or opening a
second transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.completeness_policy import (
    completeness_to_gate_result_schema,
    evaluate_completeness_policy,
)
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION
from app.agent_runtime.triage_policy import to_gate_result_schema
from app.agents.question_composer import compose_question
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import GateResult, GraphRun, GraphRunStep
from app.schemas.completeness import (
    CompletenessDomainSnapshot,
    CompletenessObservationFact,
    CompletenessPolicyInput,
    CompletenessProgress,
    CompletenessSafetyProfile,
)
from app.schemas.domain import ObservationStatus
from app.schemas.domain_seed import InitialDomainSeed
from app.schemas.question import QuestionCompositionStatus
from app.schemas.triage import TriagePolicyResult

INITIAL_INTAKE_QUESTION_VERSION = "initial-intake-question.v1"


def _stable_id(session_id: uuid.UUID, kind: str) -> uuid.UUID:
    return uuid.uuid5(session_id, f"{INITIAL_INTAKE_QUESTION_VERSION}:{kind}")


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _domain_snapshot(
    session: ConsultSession,
    seed: InitialDomainSeed,
) -> CompletenessDomainSnapshot:
    observations = tuple(
        CompletenessObservationFact(
            observation_id=item.observation_id,
            session_id=session.id,
            fact_key=item.fact_key,
            value_fingerprint=_fingerprint(item.normalized_value),
            normalized_code=None,
            status=ObservationStatus.ACTIVE,
        )
        for item in seed.observations
    )
    safety = seed.safety_profile
    safety_profile = CompletenessSafetyProfile(
        session_id=session.id,
        allergy_collection_status=safety.allergy_collection_status,
        allergen_count=len(safety.allergens or ()),
        pregnancy_collection_status=safety.pregnancy_collection_status,
        lactation_collection_status=safety.lactation_collection_status,
        medications_collection_status=safety.medications_collection_status,
        medication_count=len(safety.medications or ()),
        major_conditions_collection_status=safety.major_conditions_collection_status,
        major_condition_count=len(safety.major_conditions or ()),
        contraindications_collection_status=safety.contraindications_collection_status,
        contraindication_count=len(safety.contraindications or ()),
    )
    return CompletenessDomainSnapshot(
        session_id=session.id,
        state_version=session.state_version,
        observations=observations,
        safety_profile=safety_profile,
    )


def _gate_row(
    *,
    gate_id: uuid.UUID,
    session_id: uuid.UUID,
    graph_run_id: uuid.UUID,
    gate: Any,
) -> GateResult:
    return GateResult(
        id=gate_id,
        session_id=session_id,
        graph_run_id=graph_run_id,
        gate_name=gate.gate_name,
        policy_version=gate.policy_version,
        input_state_version=gate.input_state_version,
        decision=gate.decision.value,
        details=gate.details,
    )


async def create_initial_intake_question(
    db: AsyncSession,
    session: ConsultSession,
    seed: InitialDomainSeed,
    triage_result: TriagePolicyResult,
    *,
    trace_id: str,
) -> ConsultMessage | None:
    """Persist one deterministic first question without changing Domain version."""

    if triage_result.disposition.value != "continue" or session.status != "active":
        return None
    if not any(item.fact_key == "chief_complaint.symptom" for item in seed.observations):
        return None

    question_id = _stable_id(session.id, "question")
    existing = await db.get(ConsultMessage, question_id)
    if existing is not None:
        return existing

    progress = CompletenessProgress()
    completeness = evaluate_completeness_policy(
        CompletenessPolicyInput(
            input_state_version=session.state_version,
            domain_snapshot=_domain_snapshot(session, seed),
            triage_gate=triage_result.gate_result,
            progress=progress,
        )
    )
    outcome = await compose_question(completeness_result=completeness)
    if outcome.status is QuestionCompositionStatus.NO_QUESTION:
        return None
    if outcome.status is not QuestionCompositionStatus.SUCCEEDED or outcome.result is None:
        raise RuntimeError("initial intake question composition failed")

    question_result = outcome.result
    message = ConsultMessage(
        id=question_id,
        session_id=session.id,
        role="agent",
        stage="inquiry",
        agent_name="question_composer",
        content=question_result.question,
        structured_delta=question_result.model_dump(mode="json"),
        trace_id=trace_id[:64],
    )
    db.add(message)

    triage_gate = to_gate_result_schema(triage_result)
    completeness_gate = completeness_to_gate_result_schema(completeness)
    graph_run_id = _stable_id(session.id, "bootstrap-run")
    completed_at = datetime.now(UTC)
    db.add(
        GraphRun(
            id=graph_run_id,
            session_id=session.id,
            graph_version=DEFAULT_GRAPH_VERSION,
            command_id=f"bootstrap:{session.id}",
            input_state_version=session.state_version,
            status="completed",
            completed_at=completed_at,
        )
    )
    for step_index, step_name in enumerate(
        ("initial_domain_seed", "triage_gate", "completeness_gate", "compose_question")
    ):
        db.add(
            GraphRunStep(
                id=_stable_id(session.id, f"bootstrap-step:{step_index}"),
                graph_run_id=graph_run_id,
                step_index=step_index,
                step_name=step_name,
                status="completed",
                step_metadata={"bootstrap_version": INITIAL_INTAKE_QUESTION_VERSION},
            )
        )
    for kind, gate in (("triage-gate", triage_gate), ("completeness-gate", completeness_gate)):
        gate_id = _stable_id(session.id, kind)
        if await db.scalar(select(GateResult.id).where(GateResult.id == gate_id)) is None:
            db.add(
                _gate_row(
                    gate_id=gate_id,
                    session_id=session.id,
                    graph_run_id=graph_run_id,
                    gate=gate,
                )
            )

    next_progress = progress.model_copy(update={"followup_rounds": 1})
    existing_snapshot = dict(session.state_snapshot or {})
    existing_snapshot.update(
        {
            "agent_runtime": "langgraph",
            "current_stage": session.current_stage,
            "state_version": session.state_version,
            "recovery_status": session.recovery_status,
            "blocked_reason": session.blocked_reason,
            "sufficiency_report": {
                "sufficient": completeness.disposition.value == "ready",
                "covered": [item.value for item in completeness.covered_dimensions],
                "missing": [item.value for item in completeness.missing_required],
                "suggestions": [],
            },
            "langgraph_intake": {
                "version": "intake-subgraph.v1",
                "last_run_id": str(graph_run_id),
                "last_patient_message_id": str(seed.source_message_id),
                "last_question_message_id": str(question_id),
                "triage": {
                    "decision": triage_gate.decision.value,
                    "policy_version": triage_gate.policy_version,
                    "disposition": (triage_gate.details or {}).get("disposition"),
                },
                "completeness": {
                    "decision": completeness_gate.decision.value,
                    "policy_version": completeness_gate.policy_version,
                    "disposition": (completeness_gate.details or {}).get("disposition"),
                },
                "progress": next_progress.model_dump(mode="json"),
                "dialogue_status": "questioning",
                "pending_safety_dimensions": [],
                "trace_id": trace_id,
                "bootstrap_version": INITIAL_INTAKE_QUESTION_VERSION,
            },
        }
    )
    session.state_snapshot = existing_snapshot

    db.add(
        AuditEvent(
            session_id=session.id,
            event_type="message.created",
            actor_type="agent",
            actor_id="question_composer",
            payload={
                "message_id": str(question_id),
                "role": "agent",
                "agent_name": "question_composer",
                "stage": "inquiry",
                "content_length": len(message.content),
                "bootstrap_version": INITIAL_INTAKE_QUESTION_VERSION,
                "selected_dimension": question_result.selected_dimension.value,
                "state_version": session.state_version,
            },
            trace_id=trace_id[:64],
        )
    )
    await db.flush()
    await db.refresh(message)
    return message


__all__ = [
    "INITIAL_INTAKE_QUESTION_VERSION",
    "create_initial_intake_question",
]
