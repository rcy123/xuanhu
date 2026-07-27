"""L6-1 record DTO and authority-bound assembler regressions."""

from __future__ import annotations

import ast
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_runtime.sandbox_recheck import (
    SandboxRecheckCoordinator,
    SandboxRecheckError,
)
from app.agent_runtime.sandbox_record import (
    RECORD_SCHEMA_VERSION,
    SANDBOX_RECORD_DISCLAIMER,
    SandboxMedicalRecordData,
    SandboxRecordAssembler,
    SandboxRecordConsistencyVerifier,
    SandboxRecordError,
    SandboxRecordPipeline,
    SandboxRecordStore,
)
from app.agent_runtime.sandbox_review import (
    SandboxResumeCommandV1,
    SandboxReviewAction,
    canonical_review_bytes,
)
from app.agent_runtime.sandbox_safety import (
    SandboxFormulaItemV1,
    SandboxRuleBundleV1,
    SandboxSafetyResultV1,
)
from tests.test_l5_4_sandbox_modify_full_recheck import (
    _NAMESPACE,
    _NEW_CHECKPOINT,
    _SESSION,
    _THREAD,
    _accepted_modify_snapshot,
    _coordinator,
    _revision_command,
    _subject_and_bundle,
    _submission,
)


class _RevocableRuleBundleAuthorizer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pause_next = False
        self._revoked: set[str] = set()
        self.authorize_entered = threading.Event()
        self.authorize_release = threading.Event()

    def recognize(self, *, rule_bundle: SandboxRuleBundleV1) -> bool:
        return type(rule_bundle) is SandboxRuleBundleV1

    def authorize(self, *, rule_bundle: SandboxRuleBundleV1) -> bool:
        with self._lock:
            allowed = rule_bundle.rule_bundle_digest not in self._revoked
            pause = self._pause_next
            self._pause_next = False
        if pause:
            self.authorize_entered.set()
            if not self.authorize_release.wait(timeout=5):
                raise AssertionError("authorization linearization probe timed out")
        return allowed

    def revoke(self, rule_bundle: SandboxRuleBundleV1) -> None:
        with self._lock:
            self._revoked.add(rule_bundle.rule_bundle_digest)

    def pause_next_authorize(self) -> None:
        with self._lock:
            self._pause_next = True
            self.authorize_entered.clear()
            self.authorize_release.clear()


def _confirm_review_required_state(
    action: SandboxReviewAction = SandboxReviewAction.CONFIRM,
) -> SandboxRecheckCoordinator:
    coordinator, *_ = _coordinator()
    outcome = coordinator.apply_revision(_revision_command(coordinator))
    assert outcome.delivery is not None
    staged = coordinator.stage_current_review(_submission(outcome.delivery, action))
    assert staged.resume_attempt_ref is not None
    resumed = coordinator.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    assert resumed.status == "applied"
    return coordinator


def _confirm_distinct_review_required_state() -> SandboxRecheckCoordinator:
    coordinator, *_ = _coordinator()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=23,
        dataset_version="distinct-record.2",
    )
    outcome = coordinator.apply_revision(
        _revision_command(
            coordinator,
            candidate=candidate,
            bundle=bundle,
        )
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
    return coordinator


def _assemble(
    coordinator: SandboxRecheckCoordinator | None = None,
) -> SandboxMedicalRecordData:
    authority = (
        _confirm_review_required_state()
        if coordinator is None
        else coordinator
    )
    return SandboxRecordAssembler().assemble(
        authority,
        namespace=_NAMESPACE,
        session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    )


def _confirm_with_revocable_authority() -> tuple[
    SandboxRecheckCoordinator,
    _RevocableRuleBundleAuthorizer,
    SandboxRuleBundleV1,
]:
    review_snapshot, clock, nonce_factory, verifier = _accepted_modify_snapshot()
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
        dataset_version="revocable-record.2",
    )
    outcome = coordinator.apply_revision(
        _revision_command(
            coordinator,
            candidate=candidate,
            bundle=bundle,
        )
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
    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "eligible"
    return coordinator, authorizer, bundle


def test_l6_1_confirmed_coordinator_produces_typed_v2_record() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    snapshot = coordinator.snapshot()
    confirm_event = tuple(
        event
        for event in snapshot.review_snapshot.events
        if event.action is SandboxReviewAction.CONFIRM
    )[-1]

    assert record.session_id == _SESSION
    assert record.assembled_at == confirm_event.applied_at
    assert record.record_version == RECORD_SCHEMA_VERSION
    assert record.disclaimer == SANDBOX_RECORD_DISCLAIMER
    assert record.record_id.startswith("sandbox-record-")
    assert all(
        type(item) is SandboxFormulaItemV1
        for item in record.reviewed_formula
    )
    assert type(record.safety_result) is SandboxSafetyResultV1


def test_l6_1_same_confirmed_authority_is_byte_stable_across_retries() -> None:
    coordinator = _confirm_review_required_state()

    first = _assemble(coordinator)
    second = _assemble(coordinator)

    assert first == second
    assert first.record_id == second.record_id
    assert canonical_review_bytes(first) == canonical_review_bytes(second)


def test_l6_revoked_bundle_blocks_every_public_record_boundary() -> None:
    coordinator, authorizer, bundle = _confirm_with_revocable_authority()
    record = _assemble(coordinator)
    pipeline = SandboxRecordPipeline(recheck_coordinator=coordinator)

    authorizer.revoke(bundle)

    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"
    with pytest.raises(SandboxRecheckError):
        coordinator.authorized_record_projection()
    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)
    assert not SandboxRecordConsistencyVerifier().verify(
        record,
        recheck_coordinator=coordinator,
    )
    with pytest.raises(SandboxRecordError):
        SandboxRecordStore().put(
            record,
            recheck_coordinator=coordinator,
        )
    with pytest.raises(SandboxRecordError):
        pipeline.run(
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )


def test_l6_authorize_linearization_allows_only_overlapping_operation() -> None:
    coordinator, authorizer, bundle = _confirm_with_revocable_authority()
    authorizer.pause_next_authorize()

    with ThreadPoolExecutor(max_workers=1) as executor:
        overlapping = executor.submit(_assemble, coordinator)
        assert authorizer.authorize_entered.wait(timeout=5)
        authorizer.revoke(bundle)
        authorizer.authorize_release.set()
        record = overlapping.result(timeout=5)

    assert record.session_id == _SESSION
    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


def test_l6_1_nested_values_are_deeply_immutable() -> None:
    record = _assemble()

    with pytest.raises(ValidationError):
        record.reviewed_formula[0].component = "changed"
    with pytest.raises(ValidationError):
        record.safety_result.decision = "block"  # type: ignore[assignment]
    with pytest.raises(ValidationError):
        record.assembled_at = 0


@pytest.mark.parametrize("kind", ("instance", "dict", "bytes", "str"))
def test_l6_1_raw_snapshot_inputs_are_not_authority(kind: str) -> None:
    coordinator = _confirm_review_required_state()
    snapshot = coordinator.snapshot()
    raw: object
    if kind == "instance":
        raw = snapshot
    elif kind == "dict":
        raw = snapshot.model_dump(mode="python")
    elif kind == "bytes":
        raw = canonical_review_bytes(snapshot)
    else:
        raw = canonical_review_bytes(snapshot).decode("utf-8")

    with pytest.raises(SandboxRecordError):
        SandboxRecordAssembler().assemble(
            raw,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )


def test_l6_1_reject_review_cannot_produce_record() -> None:
    coordinator = _confirm_review_required_state(SandboxReviewAction.REJECT)

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


def test_l6_1_scope_mismatch_fails_fixed_closed() -> None:
    coordinator = _confirm_review_required_state()

    with pytest.raises(SandboxRecordError) as raised:
        SandboxRecordAssembler().assemble(
            coordinator,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id="wrong-checkpoint",
        )

    assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l6_1_record_has_no_free_text_diagnosis_or_symptom_fields() -> None:
    assert set(SandboxMedicalRecordData.model_fields) == {
        "record_id",
        "session_id",
        "revision_id",
        "reviewed_formula",
        "safety_result",
        "review_confirm_ref",
        "assembled_at",
        "record_version",
        "disclaimer",
    }


def test_l6_1_module_keeps_offline_import_boundary() -> None:
    source = Path("app/agent_runtime/sandbox_record.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".", maxsplit=1)[0]
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", maxsplit=1)[0])

    assert imported_roots <= {
        "__future__",
        "app",
        "hashlib",
        "json",
        "pydantic",
        "threading",
        "typing",
        "warnings",
    }
    assert not any(
        token in source
        for token in (
            "httpx",
            "requests",
            "sqlalchemy",
            "subprocess",
            "socket",
            "open(",
            ".env",
        )
    )
