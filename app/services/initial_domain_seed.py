"""Transactional initial Domain State seeding for LangGraph sessions."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.triage_policy import evaluate_triage_policy, to_gate_result_schema
from app.agent_runtime.triage_precheck import TRIAGE_PRECHECK_VERSION, evaluate_raw_text_triage_precheck
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import GateResult, Observation, SafetyProfile
from app.schemas.domain import (
    CollectionStatus,
    LactationValue,
    PregnancyValue,
    SafetyProfileSchema,
)
from app.schemas.domain_seed import INITIAL_DOMAIN_SEED_VERSION, InitialDomainSeed, SeedObservation
from app.schemas.session import PatientInfo, SessionCreateRequest
from app.schemas.triage import TriageDisposition, TriagePolicyInput
from app.schemas.types import Gender, PregnancyStatus
from app.services.safety_confirmation import SafetyConfirmationService

INITIAL_DOMAIN_SEED_AGENT = "initial_domain_seed"
INITIAL_DOMAIN_SEED_AUDIT = "initial_domain_seed.created"


@dataclass(frozen=True)
class InitialDomainSeedOutcome:
    seed: InitialDomainSeed
    triage_disposition: TriageDisposition | None


def _stable_id(session_id: uuid.UUID, kind: str) -> uuid.UUID:
    return uuid.uuid5(session_id, f"{INITIAL_DOMAIN_SEED_VERSION}:{kind}")


def _list_collection(
    patient_info: PatientInfo,
    field_name: str,
) -> tuple[CollectionStatus, list[str] | None]:
    if field_name not in patient_info.model_fields_set:
        return CollectionStatus.UNKNOWN, None
    values = [item.strip() for item in getattr(patient_info, field_name) if item.strip()]
    if not values:
        return CollectionStatus.EXPLICITLY_NONE, None
    return CollectionStatus.COLLECTED, list(dict.fromkeys(values))


def _pregnancy_collection(patient_info: PatientInfo) -> tuple[CollectionStatus, PregnancyValue | None]:
    status = PregnancyStatus(patient_info.pregnancy_status)
    if "pregnancy_status" not in patient_info.model_fields_set or status is PregnancyStatus.UNKNOWN:
        return CollectionStatus.UNKNOWN, None
    if status is PregnancyStatus.NO:
        return CollectionStatus.COLLECTED, PregnancyValue.NOT_PREGNANT
    if status is PregnancyStatus.PREGNANT:
        return CollectionStatus.COLLECTED, PregnancyValue.PREGNANT
    if status is PregnancyStatus.POSSIBLE:
        return CollectionStatus.COLLECTED, PregnancyValue.POSSIBLE
    # The legacy combined enum's LACTATING value proves lactation only; it must
    # never be interpreted as "not pregnant".
    return CollectionStatus.UNKNOWN, None


def _lactation_collection(patient_info: PatientInfo) -> tuple[CollectionStatus, LactationValue | None]:
    if patient_info.lactation_status is not None:
        return CollectionStatus.COLLECTED, LactationValue(patient_info.lactation_status)
    if (
        "pregnancy_status" in patient_info.model_fields_set
        and PregnancyStatus(patient_info.pregnancy_status) is PregnancyStatus.LACTATING
    ):
        return CollectionStatus.COLLECTED, LactationValue.LACTATING
    return CollectionStatus.UNKNOWN, None


def build_initial_domain_seed(session_id: uuid.UUID, request: SessionCreateRequest) -> InitialDomainSeed:
    """Build an identity-free deterministic seed without touching persistence."""

    source_message_id = _stable_id(session_id, "source-message")
    observations: list[SeedObservation] = []
    complaint = (request.chief_complaint or "").strip()
    if complaint:
        observations.append(
            SeedObservation(
                observation_id=_stable_id(session_id, "observation:chief_complaint.symptom"),
                fact_key="chief_complaint.symptom",
                value=complaint,
                normalized_value=complaint,
            )
        )
    if request.patient_info.age is not None:
        observations.append(
            SeedObservation(
                observation_id=_stable_id(session_id, "observation:patient.age"),
                fact_key="patient.age",
                value=request.patient_info.age,
                normalized_value=request.patient_info.age,
            )
        )
    gender = Gender(request.patient_info.gender)
    if gender is not Gender.UNKNOWN:
        observations.append(
            SeedObservation(
                observation_id=_stable_id(session_id, "observation:patient.sex"),
                fact_key="patient.sex",
                value=gender.value,
                normalized_value=gender.value,
            )
        )

    allergy_status, allergens = _list_collection(request.patient_info, "allergies")
    medication_status, medications = _list_collection(request.patient_info, "current_medications")
    conditions_status, major_conditions = _list_collection(request.patient_info, "major_conditions")
    pregnancy_status, pregnancy_value = _pregnancy_collection(request.patient_info)
    lactation_status, lactation_value = _lactation_collection(request.patient_info)
    safety_profile = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=allergy_status,
        allergens=allergens,
        pregnancy_collection_status=pregnancy_status,
        pregnancy_value=pregnancy_value,
        lactation_collection_status=lactation_status,
        lactation_value=lactation_value,
        medications_collection_status=medication_status,
        medications=medications,
        major_conditions_collection_status=conditions_status,
        major_conditions=major_conditions,
    )
    digest_payload = {
        "schema_version": INITIAL_DOMAIN_SEED_VERSION,
        "observations": [item.model_dump(mode="json") for item in observations],
        "safety_profile": safety_profile.model_dump(mode="json"),
    }
    payload_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return InitialDomainSeed(
        source_message_id=source_message_id,
        observations=tuple(observations),
        safety_profile=safety_profile,
        payload_digest=payload_digest,
    )


class InitialDomainSeeder:
    """Persist a deterministic seed inside the caller-owned transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def seed(
        self,
        session: ConsultSession,
        request: SessionCreateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> InitialDomainSeedOutcome:
        seed = build_initial_domain_seed(session.id, request)
        existing = await self._db.get(ConsultMessage, seed.source_message_id)
        if existing is not None:
            existing_digest = (existing.structured_delta or {}).get("payload_digest")
            if existing_digest != seed.payload_digest:
                raise ValueError("initial domain seed digest mismatch")
            return InitialDomainSeedOutcome(seed=seed, triage_disposition=None)

        complaint = (request.chief_complaint or "").strip()
        safety_collection_status = {
            "allergy": seed.safety_profile.allergy_collection_status.value,
            "pregnancy": seed.safety_profile.pregnancy_collection_status.value,
            "lactation": seed.safety_profile.lactation_collection_status.value,
            "medications": seed.safety_profile.medications_collection_status.value,
            "major_conditions": seed.safety_profile.major_conditions_collection_status.value,
        }
        source = ConsultMessage(
            id=seed.source_message_id,
            session_id=session.id,
            role="system",
            stage="inquiry",
            agent_name=INITIAL_DOMAIN_SEED_AGENT,
            content=complaint or "structured initial clinical form",
            structured_delta={
                "schema_version": INITIAL_DOMAIN_SEED_VERSION,
                "payload_digest": seed.payload_digest,
                "fact_keys": [item.fact_key for item in seed.observations],
                "safety_collection_status": safety_collection_status,
            },
            trace_id=trace_id[:64],
        )
        self._db.add(source)
        for item in seed.observations:
            self._db.add(
                Observation(
                    id=item.observation_id,
                    session_id=session.id,
                    fact_key=item.fact_key,
                    value=item.value,
                    normalized_value=item.normalized_value,
                    source_message_id=seed.source_message_id,
                    status="active",
                    confidence=1.0,
                )
            )
        safety = seed.safety_profile
        self._db.add(
            SafetyProfile(
                id=_stable_id(session.id, "safety-profile"),
                session_id=session.id,
                allergy_collection_status=safety.allergy_collection_status.value,
                allergens=safety.allergens,
                pregnancy_collection_status=safety.pregnancy_collection_status.value,
                pregnancy_value=safety.pregnancy_value.value if safety.pregnancy_value else None,
                lactation_collection_status=safety.lactation_collection_status.value,
                lactation_value=safety.lactation_value.value if safety.lactation_value else None,
                medications_collection_status=safety.medications_collection_status.value,
                medications=safety.medications,
                major_conditions_collection_status=safety.major_conditions_collection_status.value,
                major_conditions=safety.major_conditions,
                contraindications_collection_status=safety.contraindications_collection_status.value,
                contraindications=safety.contraindications,
            )
        )
        await SafetyConfirmationService(self._db).add_confirmed_structured_form(
            session_id=session.id,
            source_message_id=seed.source_message_id,
            safety_profile=safety,
            payload_digest=seed.payload_digest,
            actor_type="doctor" if doctor_id else "system",
            actor_id=doctor_id,
            trace_id=trace_id,
            template_version=INITIAL_DOMAIN_SEED_VERSION,
        )

        triage_disposition: TriageDisposition | None = None
        if complaint:
            precheck = evaluate_raw_text_triage_precheck(seed.source_message_id, complaint)
            triage = evaluate_triage_policy(
                TriagePolicyInput(input_state_version=session.state_version, red_flag_candidates=precheck.candidates)
            )
            triage_disposition = triage.disposition
            if triage.disposition is not TriageDisposition.CONTINUE:
                gate = to_gate_result_schema(triage)
                details = dict(gate.details or {})
                details.update(
                    {
                        "triage_precheck_version": TRIAGE_PRECHECK_VERSION,
                        "triage_precheck_rule_ids": list(precheck.matched_rule_ids),
                    }
                )
                self._db.add(
                    GateResult(
                        id=_stable_id(session.id, "gate:triage"),
                        session_id=session.id,
                        graph_run_id=None,
                        gate_name=gate.gate_name,
                        policy_version=gate.policy_version,
                        input_state_version=gate.input_state_version,
                        decision=gate.decision.value,
                        details=details,
                    )
                )
                session.current_stage = "blocked"
                session.status = "blocked"
                session.recovery_status = "manual_required"
                session.blocked_reason = f"triage:{triage.disposition.value}"
                session.blocked_at = datetime.now(UTC).replace(tzinfo=None)

        self._db.add(
            AuditEvent(
                session_id=session.id,
                event_type=INITIAL_DOMAIN_SEED_AUDIT,
                actor_type="doctor" if doctor_id else "system",
                actor_id=doctor_id,
                payload={
                    "schema_version": INITIAL_DOMAIN_SEED_VERSION,
                    "source_message_id": str(seed.source_message_id),
                    "fact_keys": [item.fact_key for item in seed.observations],
                    "safety_collection_status": safety_collection_status,
                    "payload_digest": seed.payload_digest,
                },
                trace_id=trace_id,
            )
        )
        await self._db.flush()
        # A duplicate deterministic source can only exist for this session.
        assert await self._db.scalar(select(ConsultMessage.id).where(ConsultMessage.id == seed.source_message_id))
        return InitialDomainSeedOutcome(seed=seed, triage_disposition=triage_disposition)
