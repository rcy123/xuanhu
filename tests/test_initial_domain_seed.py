"""Unit contracts for identity-free initial LangGraph Domain State seeding."""

from __future__ import annotations

from uuid import uuid4

from app.schemas.domain import CollectionStatus, LactationValue, PregnancyValue
from app.schemas.session import PatientInfo, SessionCreateRequest
from app.services.initial_domain_seed import build_initial_domain_seed


def test_seed_maps_only_clinical_observations_and_excludes_identity() -> None:
    session_id = uuid4()
    request = SessionCreateRequest(
        agent_runtime="langgraph",
        chief_complaint="反复头痛",
        patient_info=PatientInfo(
            name="identity-name-must-not-enter-domain",
            patient_ref="identity-ref-must-not-enter-domain",
            age=38,
            gender="female",
        ),
    )
    seed = build_initial_domain_seed(session_id, request)
    facts = {item.fact_key: item.value for item in seed.observations}
    assert facts == {
        "chief_complaint.symptom": "反复头痛",
        "patient.age": 38,
        "patient.sex": "female",
    }
    serialized = seed.model_dump_json()
    assert "identity-name-must-not-enter-domain" not in serialized
    assert "identity-ref-must-not-enter-domain" not in serialized


def test_omitted_safety_fields_remain_unknown() -> None:
    seed = build_initial_domain_seed(uuid4(), SessionCreateRequest(agent_runtime="langgraph"))
    safety = seed.safety_profile
    assert safety.allergy_collection_status is CollectionStatus.UNKNOWN
    assert safety.medications_collection_status is CollectionStatus.UNKNOWN
    assert safety.major_conditions_collection_status is CollectionStatus.UNKNOWN
    assert safety.pregnancy_collection_status is CollectionStatus.UNKNOWN
    assert safety.lactation_collection_status is CollectionStatus.UNKNOWN


def test_explicit_course_is_seeded_from_clear_chief_complaint_duration() -> None:
    for complaint, expected in (
        ("感冒三天", "三天"),
        ("咳嗽 3 天，伴咽痛", "3天"),
        ("头痛半月", "半月"),
    ):
        seed = build_initial_domain_seed(
            uuid4(),
            SessionCreateRequest(agent_runtime="langgraph", chief_complaint=complaint),
        )
        facts = {item.fact_key: item.value for item in seed.observations}
        assert facts["chief_complaint.course"] == expected


def test_ambiguous_change_timing_is_not_seeded_as_whole_course() -> None:
    seed = build_initial_domain_seed(
        uuid4(),
        SessionCreateRequest(agent_runtime="langgraph", chief_complaint="头痛，3天前加重"),
    )
    assert "chief_complaint.course" not in {item.fact_key for item in seed.observations}


def test_explicit_empty_and_nonempty_safety_lists_are_distinct() -> None:
    seed = build_initial_domain_seed(
        uuid4(),
        SessionCreateRequest(
            agent_runtime="langgraph",
            patient_info=PatientInfo(
                allergies=[],
                current_medications=["  阿司匹林 ", "阿司匹林"],
                major_conditions=[],
            ),
        ),
    )
    safety = seed.safety_profile
    assert safety.allergy_collection_status is CollectionStatus.EXPLICITLY_NONE
    assert safety.allergens is None
    assert safety.medications_collection_status is CollectionStatus.COLLECTED
    assert safety.medications == ["阿司匹林"]
    assert safety.major_conditions_collection_status is CollectionStatus.EXPLICITLY_NONE


def test_pregnancy_and_lactation_do_not_infer_each_other() -> None:
    pregnant = build_initial_domain_seed(
        uuid4(),
        SessionCreateRequest(
            agent_runtime="langgraph",
            patient_info=PatientInfo(pregnancy_status="possible"),
        ),
    ).safety_profile
    assert pregnant.pregnancy_value is PregnancyValue.POSSIBLE
    assert pregnant.lactation_collection_status is CollectionStatus.UNKNOWN

    lactating = build_initial_domain_seed(
        uuid4(),
        SessionCreateRequest(
            agent_runtime="langgraph",
            patient_info=PatientInfo(lactation_status=LactationValue.LACTATING),
        ),
    ).safety_profile
    assert lactating.lactation_value is LactationValue.LACTATING
    assert lactating.pregnancy_collection_status is CollectionStatus.UNKNOWN


def test_seed_ids_and_digest_are_deterministic_for_replay() -> None:
    session_id = uuid4()
    request = SessionCreateRequest(
        agent_runtime="langgraph",
        chief_complaint="咳嗽",
        patient_info=PatientInfo(age=42, allergies=["青霉素"]),
    )
    first = build_initial_domain_seed(session_id, request)
    second = build_initial_domain_seed(session_id, request)
    assert first == second
