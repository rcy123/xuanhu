"""L6-2 authority and consistency verifier adversarial regressions."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator

import pytest

import app.agent_runtime.sandbox_record as record_module
from app.agent_runtime.sandbox_recheck import (
    SandboxRecheckCoordinator,
    SandboxRecheckError,
    SandboxRecheckSnapshotV1,
    SandboxRevisionRecordV1,
)
from app.agent_runtime.sandbox_record import (
    SandboxMedicalRecordData,
    SandboxRecordAssembler,
    SandboxRecordConsistencyVerifier,
    SandboxRecordError,
    SandboxRecordNarration,
    SandboxRecordPipeline,
    SandboxRecordStore,
    serialize_record,
)
from app.agent_runtime.sandbox_review import (
    SandboxResumeCommandV1,
    SandboxReviewAction,
    SandboxSignatureVerifier,
    canonical_review_bytes,
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
    _accepted_modify_snapshot,
    _coordinator,
    _revision_command,
    _subject_and_bundle,
    _submission,
)
from tests.test_sandbox_record_l6_1 import (
    _assemble,
    _confirm_review_required_state,
    _confirm_with_revocable_authority,
    _RevocableRuleBundleAuthorizer,
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
def test_l6_2_verifier_rejects_all_raw_snapshot_forms(
    raw_factory: Callable[[SandboxRecheckSnapshotV1], object],
) -> None:
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
    coordinator._revisions[-1] = current.model_copy(
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


def test_l6_2_projection_override_and_uninitialized_exact_type_fail_closed() -> None:
    coordinator = _confirm_review_required_state()
    expected = _assemble(coordinator)
    projection = coordinator.authorized_record_projection()
    forged_item = projection.subject.formula_items[0].model_copy(
        update={"component": "forged-by-instance-method"}
    )
    forged_subject = projection.subject.model_copy(
        update={
            "formula_items": (
                forged_item,
                *projection.subject.formula_items[1:],
            )
        }
    )
    forged_projection = projection.model_copy(
        update={"subject": forged_subject}
    )
    coordinator.authorized_record_projection = (  # type: ignore[method-assign]
        lambda: forged_projection
    )

    rebuilt = _assemble(coordinator)

    assert rebuilt == expected
    assert all(
        item.component != "forged-by-instance-method"
        for item in rebuilt.reviewed_formula
    )

    uninitialized = object.__new__(SandboxRecheckCoordinator)
    uninitialized.authorized_record_projection = (  # type: ignore[method-assign]
        lambda: forged_projection
    )
    with pytest.raises(SandboxRecordError):
        SandboxRecordAssembler().assemble(
            uninitialized,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )


def test_l6_2_nested_completion_authority_override_cannot_bypass_revocation() -> None:
    coordinator, authorizer, bundle = _confirm_with_revocable_authority()
    authority = coordinator._completion_authority(
        expected_scope=(_NAMESPACE, _SESSION, _THREAD, _NEW_CHECKPOINT),
    )
    assert authority is not None
    authorizer.revoke(bundle)
    coordinator._completion_authority = (  # type: ignore[method-assign]
        lambda **_: authority
    )

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


def test_l6_2_public_completion_scope_rejects_none_and_non_exact_strings() -> None:
    coordinator = _confirm_review_required_state()

    class _Scope(str):
        pass

    assert coordinator.completion_eligibility(
        namespace=None,  # type: ignore[arg-type]
        test_session_id=None,  # type: ignore[arg-type]
        thread_id=None,  # type: ignore[arg-type]
        checkpoint_id=None,  # type: ignore[arg-type]
    ).status == "blocked"
    assert coordinator.completion_eligibility(
        namespace=None,  # type: ignore[arg-type]
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"
    assert coordinator.completion_eligibility(
        namespace=_Scope(_NAMESPACE),
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"


@pytest.mark.parametrize(
    "field",
    ("namespace", "session_id", "thread_id", "checkpoint_id"),
)
def test_l6_2_record_scope_rejects_hostile_string_subclasses(field: str) -> None:
    coordinator = _confirm_review_required_state()

    class _AlwaysEqualStr(str):
        def __eq__(self, other: object) -> bool:
            del other
            return True

        def __ne__(self, other: object) -> bool:
            del other
            return False

        def __hash__(self) -> int:
            return str.__hash__(self)

    namespace: str = _NAMESPACE
    session_id: str = _SESSION
    thread_id: str = _THREAD
    checkpoint_id: str = _NEW_CHECKPOINT
    hostile = _AlwaysEqualStr("wrong-scope")
    if field == "namespace":
        namespace = hostile
    elif field == "session_id":
        session_id = hostile
    elif field == "thread_id":
        thread_id = hostile
    else:
        checkpoint_id = hostile

    with pytest.raises(SandboxRecordError):
        SandboxRecordAssembler().assemble(
            coordinator,
            namespace=namespace,
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
        )
    with pytest.raises(SandboxRecordError):
        SandboxRecordPipeline(recheck_coordinator=coordinator).run(
            namespace=namespace,
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
        )


def test_l6_2_nested_review_eligibility_override_cannot_bypass_revocation() -> None:
    coordinator, authorizer, bundle = _confirm_with_revocable_authority()
    eligible = coordinator._review.eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    )
    assert eligible.status == "eligible"
    authorizer.revoke(bundle)
    coordinator._review.eligibility = (  # type: ignore[method-assign]
        lambda **_: eligible
    )

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


def test_l6_2_authorizer_reentry_cannot_project_corrupted_current_ref() -> None:
    coordinator, authorizer, _ = _confirm_with_revocable_authority()
    original_authorize = authorizer.authorize

    def corrupt_current_ref_on_authorize(*, rule_bundle: object) -> bool:
        allowed = original_authorize(rule_bundle=rule_bundle)  # type: ignore[arg-type]
        coordinator._current_revision_ref = coordinator._revisions[0].revision_ref
        return allowed

    authorizer.authorize = corrupt_current_ref_on_authorize  # type: ignore[method-assign]

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)
    with pytest.raises(SandboxRecheckError):
        coordinator.snapshot()


@pytest.mark.parametrize("mutation", ("current-ref", "hidden-model-state"))
def test_l6_2_signature_verifier_reentry_cannot_escape_final_state_check(
    mutation: str,
) -> None:
    class _ReentrantVerifier:
        def __init__(self, delegate: SandboxSignatureVerifier) -> None:
            self._delegate = delegate
            self.calls = 0
            self.trigger_at: int | None = None
            self.callback: Callable[[], None] | None = None

        def verify(
            self,
            *,
            signed_payload_digest: str,
            sandbox_test_signature_scheme: str,
            sandbox_test_key_id: str,
            sandbox_test_signature: str,
        ) -> bool:
            allowed = self._delegate.verify(
                signed_payload_digest=signed_payload_digest,
                sandbox_test_signature_scheme=sandbox_test_signature_scheme,
                sandbox_test_key_id=sandbox_test_key_id,
                sandbox_test_signature=sandbox_test_signature,
            )
            self.calls += 1
            if self.calls == self.trigger_at and self.callback is not None:
                self.callback()
            return allowed

    review_snapshot, clock, nonce_factory, delegate = _accepted_modify_snapshot()
    verifier = _ReentrantVerifier(delegate)
    authorizer = _RevocableRuleBundleAuthorizer()
    coordinator = SandboxRecheckCoordinator(
        review_snapshot=review_snapshot,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=verifier,
        rule_bundle_authorizer=authorizer,
    )
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=23,
        dataset_version="reentrant-verifier-record.2",
    )
    outcome = coordinator.apply_revision(
        _revision_command(coordinator, candidate=candidate, bundle=bundle)
    )
    assert outcome.delivery is not None
    staged = coordinator.stage_current_review(
        _submission(outcome.delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    resumed = coordinator.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    assert resumed.status == "applied"
    attempt_count = len(coordinator.snapshot().review_snapshot.attempts)
    verifier.trigger_at = verifier.calls + attempt_count + 1

    def mutate_coordinator() -> None:
        if mutation == "current-ref":
            coordinator._current_revision_ref = coordinator._revisions[0].revision_ref
            return
        current = coordinator._revisions[-1]
        coordinator._revisions[-1] = current.model_copy(
            update={"hidden_callback_state": "verifier-hidden-state"}
        )

    verifier.callback = mutate_coordinator

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)
    with pytest.raises(SandboxRecheckError):
        coordinator.snapshot()


def test_l6_2_container_iteration_cannot_mutate_after_final_capture() -> None:
    coordinator = _confirm_review_required_state()
    current = coordinator._revisions[-1]
    forged_item = current.subject.formula_items[0].model_copy(
        update={"component": "pure-capture-reentry"}
    )
    forged_subject = current.subject.model_copy(
        update={
            "formula_items": (
                forged_item,
                *current.subject.formula_items[1:],
            )
        }
    )
    forged_revision = current.model_copy(update={"subject": forged_subject})

    class _FourthIterationMutation(list[object]):
        calls = 0

        def __iter__(self) -> Iterator[object]:
            self.calls += 1
            if self.calls == 4:
                coordinator._revisions[-1] = forged_revision
            return super().__iter__()

    receipts = _FourthIterationMutation(coordinator._receipts)
    coordinator._receipts = receipts  # type: ignore[assignment]

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)
    assert receipts.calls == 0


def test_l6_2_lock_context_cannot_swap_revoked_authority() -> None:
    coordinator, authorizer, bundle = _confirm_with_revocable_authority()
    authorizer.revoke(bundle)
    accepted = _AcceptAllRuleBundleAuthorizer()

    class _AuthoritySwappingContext:
        def __enter__(self) -> None:
            coordinator._rule_bundle_authorizer = accepted
            coordinator._review._rule_bundle_authorizer = accepted

        def __exit__(self, *_: object) -> None:
            coordinator._rule_bundle_authorizer = authorizer
            coordinator._review._rule_bundle_authorizer = authorizer

    coordinator._lock = _AuthoritySwappingContext()  # type: ignore[assignment]

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


def test_l6_2_authorizer_reentry_cannot_replace_exact_lock_identity() -> None:
    coordinator, authorizer, _ = _confirm_with_revocable_authority()
    original_authorize = authorizer.authorize

    def replace_lock_on_authorize(*, rule_bundle: object) -> bool:
        allowed = original_authorize(rule_bundle=rule_bundle)  # type: ignore[arg-type]
        coordinator._lock = threading.RLock()
        return allowed

    authorizer.authorize = replace_lock_on_authorize  # type: ignore[method-assign]

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


def test_l6_2_projection_does_not_index_executable_revision_container() -> None:
    coordinator, authorizer, bundle = _confirm_with_revocable_authority()
    authorizer.revoke(bundle)
    accepted = _AcceptAllRuleBundleAuthorizer()

    class _FirstIndexMutation(list[SandboxRevisionRecordV1]):
        calls = 0

        def __getitem__(  # type: ignore[override]
            self,
            index: int,
        ) -> SandboxRevisionRecordV1:
            self.calls += 1
            coordinator._revisions = list(self)
            coordinator._rule_bundle_authorizer = accepted
            coordinator._review._rule_bundle_authorizer = accepted
            return coordinator._revisions[index]

    revisions = _FirstIndexMutation(coordinator._revisions)
    coordinator._revisions = revisions

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)
    assert revisions.calls == 0


@pytest.mark.parametrize(
    "target",
    ("revision", "review-event", "scalar-subclass"),
)
def test_l6_2_authorizer_reentry_hidden_model_state_fails_closed(
    target: str,
) -> None:
    coordinator, authorizer, _ = _confirm_with_revocable_authority()
    original_authorize = authorizer.authorize

    def inject_hidden_state(*, rule_bundle: object) -> bool:
        allowed = original_authorize(rule_bundle=rule_bundle)  # type: ignore[arg-type]
        if target == "revision":
            current = coordinator._revisions[-1]
            coordinator._revisions[-1] = current.model_copy(
                update={"hidden_callback_state": "must-not-be-normalized-away"}
            )
        elif target == "review-event":
            event = coordinator._review_store._events[-1]
            coordinator._review_store._events[-1] = event.model_copy(
                update={"hidden_callback_state": "must-not-be-normalized-away"}
            )
        else:
            class _Namespace(str):
                pass

            current = coordinator._revisions[-1]
            coordinator._revisions[-1] = current.model_copy(
                update={"namespace": _Namespace(current.namespace)}
            )
        return allowed

    authorizer.authorize = inject_hidden_state  # type: ignore[method-assign]

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


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
