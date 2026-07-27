"""L6-2 authority and consistency verifier adversarial regressions."""

from __future__ import annotations

import hashlib

import pytest

import app.agent_runtime.sandbox_record as record_module
from app.agent_runtime.sandbox_recheck import (
    SandboxRecheckCoordinator,
    SandboxRecheckError,
)
from app.agent_runtime.sandbox_record import (
    SandboxMedicalRecordData,
    SandboxRecordAssembler,
    SandboxRecordConsistencyVerifier,
    SandboxRecordError,
    SandboxRecordNarration,
    SandboxRecordStore,
    canonical_review_bytes,
    serialize_record,
)
from app.agent_runtime.sandbox_safety import (
    SandboxFormulaItemV1,
    SandboxSafetyDecision,
)
from tests.test_l5_4_sandbox_modify_full_recheck import (
    _NAMESPACE,
    _NEW_CHECKPOINT,
    _SESSION,
    _THREAD,
    _AcceptAllRuleBundleAuthorizer,
    _coordinator,
)
from tests.test_sandbox_record_l6_1 import (
    _assemble,
    _confirm_review_required_state,
)


def _record_id(fields: dict[str, object]) -> str:
    body = dict(fields)
    body.pop("record_id", None)
    return "sandbox-record-" + hashlib.sha256(
        canonical_review_bytes(body)
    ).hexdigest()


def _model_construct_record(
    record: SandboxMedicalRecordData,
    **updates: object,
) -> SandboxMedicalRecordData:
    fields = {
        field_name: getattr(record, field_name)
        for field_name in SandboxMedicalRecordData.model_fields
    }
    fields.update(updates)
    fields["record_id"] = _record_id(fields)
    return SandboxMedicalRecordData.model_construct(**fields)


def test_l6_2_valid_record_and_exact_narration_pass() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    narration = SandboxRecordNarration.narrate(record)

    assert SandboxRecordConsistencyVerifier().verify(
        record,
        recheck_coordinator=coordinator,
        narration=narration,
    )


def test_l6_2_tampered_nested_formula_is_rejected() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    forged_item = record.reviewed_formula[0].model_copy(
        update={"component": "forged-component"}
    )
    forged = _model_construct_record(
        record,
        reviewed_formula=(forged_item, *record.reviewed_formula[1:]),
    )

    assert not SandboxRecordConsistencyVerifier().verify(
        forged,
        recheck_coordinator=coordinator,
    )


@pytest.mark.parametrize("field", ("review_confirm_ref", "safety_result"))
def test_l6_2_tampered_authority_fields_are_rejected(field: str) -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    value: object = "sandbox-attempt-" + "0" * 64
    if field == "safety_result":
        value = record.safety_result.model_copy(
            update={"decision": SandboxSafetyDecision.BLOCK}
        )
    forged = _model_construct_record(record, **{field: value})

    assert not SandboxRecordConsistencyVerifier().verify(
        forged,
        recheck_coordinator=coordinator,
    )


def test_l6_2_equal_float_for_integer_field_is_rejected_strictly() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    forged_item = record.reviewed_formula[0].model_construct(
        **{
            **record.reviewed_formula[0].model_dump(mode="python"),
            "amount_milliunits": float(
                record.reviewed_formula[0].amount_milliunits
            ),
        }
    )
    with pytest.warns(UserWarning, match="amount_milliunits"):
        forged = _model_construct_record(
            record,
            reviewed_formula=(forged_item, *record.reviewed_formula[1:]),
        )

    assert not SandboxRecordConsistencyVerifier().verify(
        forged,
        recheck_coordinator=coordinator,
    )


def test_l6_2_hidden_extra_field_is_rejected_before_round_trip() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    hidden = record.model_copy(update={"diagnosis": "forged-hidden-field"})

    assert hasattr(hidden, "diagnosis")
    assert not SandboxRecordConsistencyVerifier().verify(
        hidden,
        recheck_coordinator=coordinator,
    )


def test_l6_2_nested_private_state_is_rejected_by_every_boundary() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    hidden_item = record.reviewed_formula[0].model_copy()
    object.__setattr__(
        hidden_item,
        "__pydantic_private__",
        {"mutable_payload": {"diagnosis": "hidden"}},
    )
    hidden = _model_construct_record(
        record,
        reviewed_formula=(hidden_item, *record.reviewed_formula[1:]),
    )

    assert not SandboxRecordConsistencyVerifier().verify(
        hidden,
        recheck_coordinator=coordinator,
    )
    with pytest.raises(SandboxRecordError):
        SandboxRecordNarration.narrate(hidden)
    with pytest.raises(SandboxRecordError):
        serialize_record(hidden)
    with pytest.raises(SandboxRecordError):
        SandboxRecordStore().put(
            hidden,
            recheck_coordinator=coordinator,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("record_version", "sandbox-medical-record.v1"),
        ("disclaimer", "caller-controlled"),
        ("assembled_at", -1),
    ),
)
def test_l6_2_invalid_metadata_is_rejected_even_with_recomputed_id(
    field: str,
    value: object,
) -> None:
    coordinator = _confirm_review_required_state()
    forged = _model_construct_record(_assemble(coordinator), **{field: value})

    assert not SandboxRecordConsistencyVerifier().verify(
        forged,
        recheck_coordinator=coordinator,
    )


def test_l6_2_altered_narration_is_rejected() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    forged_narration = (
        SandboxRecordNarration.narrate(record)
        + "Diagnosis: forged"
    )

    assert not SandboxRecordConsistencyVerifier().verify(
        record,
        recheck_coordinator=coordinator,
        narration=forged_narration,
    )


@pytest.mark.parametrize(
    "raw_factory",
    (
        lambda snapshot: snapshot,
        lambda snapshot: snapshot.model_dump(mode="python"),
        lambda snapshot: canonical_review_bytes(snapshot),
        lambda snapshot: canonical_review_bytes(snapshot).decode("utf-8"),
    ),
)
def test_l6_2_verifier_rejects_all_raw_snapshot_forms(raw_factory) -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)

    assert not SandboxRecordConsistencyVerifier().verify(
        record,
        recheck_coordinator=raw_factory(coordinator.snapshot()),
    )


def test_l6_2_forged_snapshot_instance_cannot_become_l5_capability() -> None:
    coordinator, _, clock, nonce_factory, verifier = _coordinator()
    confirmed = _confirm_review_required_state()
    snapshot = confirmed.snapshot()
    current = snapshot.revisions[-1]
    forged_item = current.subject.formula_items[0].model_copy(
        update={"component": "forged-snapshot-component"}
    )
    forged_subject = current.subject.model_copy(
        update={
            "formula_items": (
                forged_item,
                *current.subject.formula_items[1:],
            )
        }
    )
    forged_current = current.model_copy(update={"subject": forged_subject})
    forged_snapshot = snapshot.model_copy(
        update={
            "revisions": (
                *snapshot.revisions[:-1],
                forged_current,
            )
        }
    )

    with pytest.raises(SandboxRecheckError):
        SandboxRecheckCoordinator(
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=verifier,
            rule_bundle_authorizer=_AcceptAllRuleBundleAuthorizer(),
            snapshot=forged_snapshot,
        )

    with pytest.raises(SandboxRecordError):
        SandboxRecordAssembler().assemble(
            forged_snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )

    assert coordinator is not None


def test_l6_2_failed_signature_restore_never_yields_capability() -> None:
    confirmed = _confirm_review_required_state()
    _, _, clock, nonce_factory, _ = _coordinator()

    class _RejectingVerifier:
        def verify(self, **_: object) -> bool:
            return False

    with pytest.raises(SandboxRecheckError):
        SandboxRecheckCoordinator(
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=_RejectingVerifier(),
            rule_bundle_authorizer=_AcceptAllRuleBundleAuthorizer(),
            snapshot=confirmed.snapshot(),
        )


def test_l6_2_post_construction_coordinator_mutation_is_revalidated() -> None:
    coordinator = _confirm_review_required_state()
    current = coordinator.snapshot().revisions[-1]
    forged_item = current.subject.formula_items[0].model_copy(
        update={"component": "forged-after-construction"}
    )
    forged_subject = current.subject.model_copy(
        update={
            "formula_items": (
                forged_item,
                *current.subject.formula_items[1:],
            )
        }
    )
    coordinator._revisions[-1] = current.model_copy(  # type: ignore[attr-defined]
        update={"subject": forged_subject}
    )

    with pytest.raises(SandboxRecheckError):
        coordinator.snapshot()
    with pytest.raises(SandboxRecordError):
        SandboxRecordAssembler().assemble(
            coordinator,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )


def test_l6_2_instance_snapshot_override_and_uninitialized_exact_type_fail_closed() -> None:
    coordinator = _confirm_review_required_state()
    expected = _assemble(coordinator)
    snapshot = coordinator.snapshot()
    current = snapshot.revisions[-1]
    forged_item = current.subject.formula_items[0].model_copy(
        update={"component": "forged-by-instance-method"}
    )
    forged_subject = current.subject.model_copy(
        update={
            "formula_items": (
                forged_item,
                *current.subject.formula_items[1:],
            )
        }
    )
    forged_snapshot = snapshot.model_copy(
        update={
            "revisions": (
                *snapshot.revisions[:-1],
                current.model_copy(update={"subject": forged_subject}),
            )
        }
    )
    coordinator.snapshot = lambda: forged_snapshot  # type: ignore[method-assign]

    rebuilt = _assemble(coordinator)

    assert rebuilt == expected
    assert all(
        item.component != "forged-by-instance-method"
        for item in rebuilt.reviewed_formula
    )

    uninitialized = object.__new__(SandboxRecheckCoordinator)
    uninitialized.snapshot = lambda: forged_snapshot  # type: ignore[method-assign]
    with pytest.raises(SandboxRecordError):
        SandboxRecordAssembler().assemble(
            uninitialized,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )


def test_l6_2_nested_hidden_state_is_rejected_by_every_record_boundary() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    hidden_item = record.reviewed_formula[0].model_copy(
        update={"diagnosis": "hidden-nested-state"}
    )
    hidden = _model_construct_record(
        record,
        reviewed_formula=(hidden_item, *record.reviewed_formula[1:]),
    )

    assert not SandboxRecordConsistencyVerifier().verify(
        hidden,
        recheck_coordinator=coordinator,
    )
    with pytest.raises(SandboxRecordError):
        SandboxRecordNarration.narrate(hidden)
    with pytest.raises(SandboxRecordError):
        serialize_record(hidden)
    with pytest.raises(SandboxRecordError):
        SandboxRecordStore().put(
            hidden,
            recheck_coordinator=coordinator,
        )


def test_l6_2_oversized_constructed_graph_is_rejected_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _assemble()
    item_fields = {
        field_name: getattr(record.reviewed_formula[0], field_name)
        for field_name in SandboxFormulaItemV1.model_fields
    }
    oversized_item = SandboxFormulaItemV1.model_construct(
        **{
            **item_fields,
            "component": "x" * (8 * 1024 * 1024),
        }
    )
    record_fields = {
        field_name: getattr(record, field_name)
        for field_name in SandboxMedicalRecordData.model_fields
    }
    oversized = SandboxMedicalRecordData.model_construct(
        **{
            **record_fields,
            "reviewed_formula": (
                oversized_item,
                *record.reviewed_formula[1:],
            ),
        }
    )
    calls = 0

    def forbidden_serialization(_: object) -> bytes:
        nonlocal calls
        calls += 1
        raise AssertionError("oversized graph reached canonical serialization")

    monkeypatch.setattr(
        record_module,
        "canonical_review_bytes",
        forbidden_serialization,
    )

    with pytest.raises(SandboxRecordError):
        serialize_record(oversized)

    assert calls == 0


def test_l6_2_unexpected_list_graph_is_rejected_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    item_fields = {
        field_name: getattr(record.reviewed_formula[0], field_name)
        for field_name in SandboxFormulaItemV1.model_fields
    }
    hostile_item = SandboxFormulaItemV1.model_construct(
        **{
            **item_fields,
            "component": ["x" * (8 * 1024 * 1024)],
        }
    )
    record_fields = {
        field_name: getattr(record, field_name)
        for field_name in SandboxMedicalRecordData.model_fields
    }
    hostile = SandboxMedicalRecordData.model_construct(
        **{
            **record_fields,
            "reviewed_formula": (
                hostile_item,
                *record.reviewed_formula[1:],
            ),
        }
    )
    calls = 0

    def forbidden_serialization(_: object) -> bytes:
        nonlocal calls
        calls += 1
        raise AssertionError("unexpected graph reached canonical serialization")

    monkeypatch.setattr(
        record_module,
        "canonical_review_bytes",
        forbidden_serialization,
    )

    assert not SandboxRecordConsistencyVerifier().verify(
        hostile,
        recheck_coordinator=coordinator,
    )
    with pytest.raises(SandboxRecordError):
        SandboxRecordNarration.narrate(hostile)
    with pytest.raises(SandboxRecordError):
        serialize_record(hostile)
    with pytest.raises(SandboxRecordError):
        SandboxRecordStore().put(
            hostile,
            recheck_coordinator=coordinator,
        )
    assert calls == 0


def test_l6_2_verification_does_not_mutate_inputs() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    before_record = canonical_review_bytes(record)
    before_snapshot = canonical_review_bytes(coordinator.snapshot())

    assert SandboxRecordConsistencyVerifier().verify(
        record,
        recheck_coordinator=coordinator,
    )
    assert canonical_review_bytes(record) == before_record
    assert canonical_review_bytes(coordinator.snapshot()) == before_snapshot
