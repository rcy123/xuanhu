from __future__ import annotations

import ast
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.agent_runtime.sandbox_recheck import (
    SandboxRecheckCoordinator,
    SandboxRecheckError,
    SandboxRevisionCommandV1,
)
from app.agent_runtime.sandbox_review import (
    SandboxInMemoryReviewStore,
    SandboxResumeCommandV1,
    SandboxResumeSubmissionV1,
    SandboxReviewAction,
    SandboxReviewCoordinator,
    SandboxReviewSourceV1,
    SandboxTestReviewProofV1,
    review_signed_payload_digest,
)
from app.agent_runtime.sandbox_safety import (
    SandboxEvaluationCaseV1,
    SandboxEvaluatorAuthorityV1,
    SandboxFormulaItemV1,
    SandboxProfileFactV1,
    SandboxRuleBundleV1,
    SandboxRuleV1,
    SandboxSafetyDecision,
    SandboxSafetyEvaluationV1,
    SandboxSafetyIssueV1,
    SandboxSafetyRuleAdapter,
    SandboxSafetySeverity,
    SandboxSafetySubjectV1,
    SandboxSyntheticManifestV1,
    canonical_json_bytes,
)

_NAMESPACE = "sandbox.recheck.local"
_SESSION = "sandbox-recheck-session-001"
_THREAD = "sandbox-recheck-thread-001"
_OLD_CHECKPOINT = "sandbox-recheck-checkpoint-007"
_OLD_INTERRUPT = "sandbox-recheck-interrupt-007"
_NEW_CHECKPOINT = "sandbox-recheck-checkpoint-008"
_NEW_INTERRUPT = "sandbox-recheck-interrupt-008"
_NONCE = bytes(range(32))
_SCHEME = "sandbox-test-sha256.v1"
_KEY_ID = "sandbox-test-key-001"


class _FakeClock:
    def __init__(self) -> None:
        self.value = 2_000_000_000

    def __call__(self) -> int:
        return self.value


class _NonceFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.value = _NONCE

    def __call__(self) -> bytes:
        self.calls += 1
        return self.value


class _SignatureVerifier:
    @staticmethod
    def signature(payload_digest: str) -> str:
        return hashlib.sha256(f"sandbox-signature:{payload_digest}".encode()).hexdigest()

    def verify(
        self,
        *,
        signed_payload_digest: str,
        sandbox_test_signature_scheme: str,
        sandbox_test_key_id: str,
        sandbox_test_signature: str,
    ) -> bool:
        return (
            sandbox_test_signature_scheme == _SCHEME
            and sandbox_test_key_id == _KEY_ID
            and sandbox_test_signature == self.signature(signed_payload_digest)
        )


def _subject_and_bundle(
    *,
    domain_state_version: int,
    formula_revision: int,
    amount_milliunits: int,
    dataset_version: str,
    decision: SandboxSafetyDecision = SandboxSafetyDecision.ALLOW,
    formula_count: int = 1,
    issue_count: int = 0,
    profile_revision: int = 2,
    profile_value: str = "bounded-test-value",
    graph_version: str = "sandbox-recheck-graph.v1",
    bundle_version: str | None = None,
    rule_revision: int | None = None,
) -> tuple[SandboxSafetySubjectV1, SandboxRuleBundleV1]:
    formula_items = tuple(
        SandboxFormulaItemV1(
            item_id=f"synthetic-item-{index:03d}",
            component="fixed-fictitious-component",
            amount_milliunits=amount_milliunits + index,
            unit="synthetic_unit",
        )
        for index in range(1, formula_count + 1)
    )
    profile_facts = (
        SandboxProfileFactV1(
            fact_id="synthetic-profile-fact-001",
            name="fixed-fictitious-technical-profile",
            value=profile_value,
        ),
    )
    manifest = SandboxSyntheticManifestV1.build(
        dataset_name="fixed-fictitious-recheck-fixture",
        dataset_version=dataset_version,
        formula_items=formula_items,
        profile_facts=profile_facts,
    )
    evaluation = SandboxSafetyEvaluationV1(
        decision=decision,
        issues=tuple(
            SandboxSafetyIssueV1(
                issue_id=f"sandbox.recheck.issue.{index:03d}",
                rule_id="sandbox.recheck.rule.001",
                severity=SandboxSafetySeverity.HIGH,
                execution_order=index,
            )
            for index in range(issue_count)
        ),
    )
    case = SandboxEvaluationCaseV1.build(
        case_id="fixed-fictitious-recheck-case-001",
        formula_items=formula_items,
        profile_facts=profile_facts,
        manifest=manifest,
        evaluation=evaluation,
    )
    authority = SandboxEvaluatorAuthorityV1.build(cases=(case,))
    bundle = SandboxRuleBundleV1.build(
        rule_bundle_version=(
            f"sandbox-recheck-rules.{formula_revision}"
            if bundle_version is None
            else bundle_version
        ),
        rules=(
            SandboxRuleV1(
                rule_id="sandbox.recheck.rule.001",
                rule_revision=(formula_revision if rule_revision is None else rule_revision),
                parameters=(),
            ),
        ),
        evaluator_authority=authority,
    )
    subject = SandboxSafetySubjectV1.build(
        test_session_id=_SESSION,
        domain_state_version=domain_state_version,
        formula_artifact_id="synthetic-recheck-formula-001",
        formula_revision=formula_revision,
        formula_items=formula_items,
        profile_artifact_id="synthetic-recheck-profile-001",
        profile_revision=profile_revision,
        profile_facts=profile_facts,
        graph_version=graph_version,
        rule_bundle_version=bundle.rule_bundle_version,
        rule_bundle_digest=bundle.rule_bundle_digest,
        evaluator_authority_digest=authority.authority_digest,
        synthetic_manifest=manifest,
    )
    return subject, bundle


def _submission(delivery, action: SandboxReviewAction) -> SandboxResumeSubmissionV1:
    assert delivery.plaintext_nonce is not None
    payload_digest = review_signed_payload_digest(
        challenge=delivery.challenge,
        action=action,
        plaintext_nonce=delivery.plaintext_nonce,
        sandbox_test_reviewer_id="sandbox-reviewer-fixture-001",
        sandbox_test_role="sandbox_reviewer_test_role",
        sandbox_test_organization_label="local_synthetic_sandbox",
        sandbox_test_qualification_label="not_a_medical_credential",
        sandbox_test_signature_scheme=_SCHEME,
        sandbox_test_key_id=_KEY_ID,
    )
    return SandboxResumeSubmissionV1(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        challenge=delivery.challenge,
        action=action,
        plaintext_nonce=delivery.plaintext_nonce,
        proof=SandboxTestReviewProofV1(
            sandbox_test_reviewer_id="sandbox-reviewer-fixture-001",
            sandbox_test_role="sandbox_reviewer_test_role",
            sandbox_test_organization_label="local_synthetic_sandbox",
            sandbox_test_qualification_label="not_a_medical_credential",
            sandbox_test_signature_scheme=_SCHEME,
            sandbox_test_key_id=_KEY_ID,
            sandbox_test_signed_payload_digest=payload_digest,
            sandbox_test_signature=_SignatureVerifier.signature(payload_digest),
        ),
    )


def _accepted_modify_snapshot(
    action: SandboxReviewAction | None = SandboxReviewAction.MODIFY_FIXTURE,
):
    subject, bundle = _subject_and_bundle(
        domain_state_version=7,
        formula_revision=3,
        amount_milliunits=1,
        dataset_version="1.0.0",
    )
    result = SandboxSafetyRuleAdapter().evaluate(
        subject,
        bundle,
        command_id="sandbox-recheck-old-command-001",
        run_id="sandbox-recheck-old-run-001",
        trace_id="sandbox-recheck-old-trace-001",
    )
    source = SandboxReviewSourceV1.build(
        safety_subject=subject,
        safety_result=result,
        explanation_result=None,
    )
    clock = _FakeClock()
    nonce_factory = _NonceFactory()
    verifier = _SignatureVerifier()
    store = SandboxInMemoryReviewStore()
    review = SandboxReviewCoordinator(
        store=store,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=verifier,
    )
    delivery = review.create_single_use_challenge(
        source,
        namespace=_NAMESPACE,
        thread_id=_THREAD,
        checkpoint_id=_OLD_CHECKPOINT,
        interrupt_id=_OLD_INTERRUPT,
    )
    if action is not None:
        staged = review.stage_verified_resume_attempt(_submission(delivery, action))
        assert staged.resume_attempt_ref is not None
        assert review.resume(
            SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
        ).status == "applied"
    return store.snapshot(), clock, nonce_factory, verifier


def _coordinator():
    review_snapshot, clock, nonce_factory, verifier = _accepted_modify_snapshot()
    return (
        SandboxRecheckCoordinator(
            review_snapshot=review_snapshot,
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=verifier,
        ),
        review_snapshot,
        clock,
        nonce_factory,
        verifier,
    )


def _revision_command(
    coordinator: SandboxRecheckCoordinator,
    *,
    candidate: SandboxSafetySubjectV1 | None = None,
    bundle: SandboxRuleBundleV1 | None = None,
    suffix: str = "008",
) -> SandboxRevisionCommandV1:
    if candidate is None or bundle is None:
        candidate, bundle = _subject_and_bundle(
            domain_state_version=8,
            formula_revision=4,
            amount_milliunits=2,
            dataset_version="2.0.0",
        )
    return SandboxRevisionCommandV1(
        expected_current_revision_ref=coordinator.current_revision_ref,
        command_id=f"sandbox-recheck-command-{suffix}",
        run_id=f"sandbox-recheck-run-{suffix}",
        trace_id=f"sandbox-recheck-trace-{suffix}",
        candidate_subject=candidate,
        rule_bundle=bundle,
        checkpoint_id=f"sandbox-recheck-checkpoint-{suffix}",
        interrupt_id=f"sandbox-recheck-interrupt-{suffix}",
    )


def _derived_mapping_ref(prefix: str, value: dict[str, object], field: str) -> str:
    body = dict(value)
    body.pop(field, None)
    return prefix + hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _refresh_mapping_ref(
    value: dict[str, object], *, prefix: str, field: str
) -> str:
    refreshed = _derived_mapping_ref(prefix, value, field)
    value[field] = refreshed
    return refreshed


def test_l5_4_normal_modify_runs_full_recheck_and_requires_new_review() -> None:
    coordinator, review_snapshot, *_ = _coordinator()
    command = _revision_command(coordinator)
    candidate = command.candidate_subject

    outcome = coordinator.apply_revision(command)
    snapshot = coordinator.snapshot()

    assert outcome.status == "review_required"
    assert outcome.delivery is not None
    assert outcome.delivery.plaintext_nonce == _NONCE
    assert len(snapshot.revisions) == 2
    assert len(snapshot.runs) == len(snapshot.invalidations) == 1
    assert snapshot.current_revision_ref == outcome.current_revision_ref
    assert snapshot.revisions[-1].subject == candidate
    assert snapshot.revisions[-1].result is not None
    assert snapshot.revisions[-1].result.decision_subject_digest == hashlib.sha256(
        canonical_json_bytes(candidate)
    ).hexdigest()
    assert snapshot.invalidations[0].old_challenge_refs == (
        review_snapshot.challenges[-1].challenge_ref,
    )
    assert snapshot.invalidations[0].old_event_refs == (
        review_snapshot.events[-1].event_ref,
    )
    assert len(snapshot.review_snapshot.challenges) == 2
    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"


@pytest.mark.parametrize(
    "action",
    (None, SandboxReviewAction.CONFIRM, SandboxReviewAction.REJECT),
)
def test_l5_4_initial_authority_requires_one_applied_modify_action(
    action: SandboxReviewAction | None,
) -> None:
    review_snapshot, clock, nonce_factory, verifier = _accepted_modify_snapshot(action)

    with pytest.raises(SandboxRecheckError) as raised:
        SandboxRecheckCoordinator(
            review_snapshot=review_snapshot,
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=verifier,
        )
    assert str(raised.value) == "SANDBOX_RECHECK_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l5_4_current_confirm_is_required_and_old_review_stays_blocked() -> None:
    coordinator, _, *_ = _coordinator()
    outcome = coordinator.apply_revision(_revision_command(coordinator))
    assert outcome.delivery is not None
    staged = coordinator.stage_current_review(
        _submission(outcome.delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    assert coordinator.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    ).status == "applied"

    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "eligible"
    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_OLD_CHECKPOINT,
    ).status == "blocked"
    for name in ("complete", "record", "done", "export"):
        assert not hasattr(coordinator, name)


def test_l5_4_block_result_invalidates_old_authority_without_new_review() -> None:
    coordinator, review_snapshot, *_ = _coordinator()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="2.0.0",
        decision=SandboxSafetyDecision.BLOCK,
        issue_count=1,
    )
    outcome = coordinator.apply_revision(
        _revision_command(coordinator, candidate=candidate, bundle=bundle)
    )
    snapshot = coordinator.snapshot()

    assert outcome.status == "blocked"
    assert outcome.delivery is None
    assert len(snapshot.review_snapshot.challenges) == len(review_snapshot.challenges)
    assert snapshot.revisions[-1].result is not None
    assert snapshot.revisions[-1].result.decision is SandboxSafetyDecision.BLOCK
    assert snapshot.invalidations[-1].old_challenge_refs
    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"


@pytest.mark.parametrize(
    "changes",
    (
        {"amount_milliunits": 2},
        {"profile_value": "changed-bounded-value"},
        {"graph_version": "sandbox-recheck-graph.v2"},
        {"bundle_version": "sandbox-recheck-rules.changed", "rule_revision": 9},
        {"dataset_version": "2.0.0"},
    ),
)
def test_l5_4_each_supported_authority_change_runs_a_new_full_evaluation(
    changes: dict[str, object],
) -> None:
    coordinator, *_ = _coordinator()
    values: dict[str, object] = {
        "domain_state_version": 8,
        "formula_revision": 4,
        "amount_milliunits": 1,
        "dataset_version": "1.0.0",
        "bundle_version": "sandbox-recheck-rules.3",
        "rule_revision": 3,
    }
    values.update(changes)
    candidate, bundle = _subject_and_bundle(**values)  # type: ignore[arg-type]
    outcome = coordinator.apply_revision(
        _revision_command(coordinator, candidate=candidate, bundle=bundle)
    )

    assert outcome.status == "review_required"
    assert coordinator.snapshot().invalidations[-1].old_challenge_refs


def test_l5_4_unknown_adapter_version_commits_failed_revision_and_stays_blocked() -> None:
    coordinator, *_ = _coordinator()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=1,
        dataset_version="1.0.0",
        bundle_version="sandbox-recheck-rules.3",
        rule_revision=3,
    )
    candidate = candidate.model_copy(
        update={"adapter_version": "sandbox-safety-adapter.v2"}
    )
    bundle = bundle.model_copy(
        update={"adapter_version": "sandbox-safety-adapter.v2"}
    )
    outcome = coordinator.apply_revision(
        _revision_command(coordinator, candidate=candidate, bundle=bundle)
    )
    snapshot = coordinator.snapshot()

    assert outcome.status == "recheck_failed"
    assert len(snapshot.revisions) == 2
    assert len(snapshot.invalidations) == 1
    assert snapshot.revisions[-1].result is None
    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"


def test_l5_4_invalid_commands_leave_snapshot_byte_for_byte_unchanged() -> None:
    coordinator, *_ = _coordinator()
    before = coordinator.snapshot()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=9,
        formula_revision=5,
        amount_milliunits=2,
        dataset_version="2.0.0",
    )
    with pytest.raises(SandboxRecheckError) as raised:
        coordinator.apply_revision(
            _revision_command(coordinator, candidate=candidate, bundle=bundle)
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert coordinator.snapshot() == before


def test_l5_4_schema_digest_and_bundle_mismatches_are_zero_write() -> None:
    coordinator, *_ = _coordinator()
    before = coordinator.snapshot()
    command = _revision_command(coordinator)
    dumped = command.model_dump(mode="python")
    extra = {**dumped, "caller_claimed_valid": True}
    missing = dict(dumped)
    missing.pop("trace_id")

    for invalid in (extra, missing):
        with pytest.raises(SandboxRecheckError) as raised:
            coordinator.apply_revision(invalid)  # type: ignore[arg-type]
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert coordinator.snapshot() == before

    _, mismatched_bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=9,
        dataset_version="mismatch.1",
    )
    with pytest.raises(SandboxRecheckError):
        coordinator.apply_revision(
            command.model_copy(update={"rule_bundle": mismatched_bundle})
        )
    assert coordinator.snapshot() == before

    corrupted_subject = command.candidate_subject.model_copy(
        update={"rule_bundle_digest": "0" * 64}
    )
    with pytest.raises(SandboxRecheckError):
        coordinator.apply_revision(
            command.model_copy(update={"candidate_subject": corrupted_subject})
        )
    assert coordinator.snapshot() == before

    conflict = _revision_command(coordinator).model_copy(
        update={"expected_current_revision_ref": "sandbox-recheck-revision-" + "0" * 64}
    )
    assert coordinator.apply_revision(conflict).status == "replayed_or_conflict"
    assert coordinator.snapshot() == before

    same_subject, same_bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=1,
        dataset_version="1.0.0",
        bundle_version="sandbox-recheck-rules.3",
        rule_revision=3,
    )
    with pytest.raises(SandboxRecheckError):
        coordinator.apply_revision(
            _revision_command(
                coordinator, candidate=same_subject, bundle=same_bundle, suffix="same"
            )
        )
    assert coordinator.snapshot() == before


def test_l5_4_runtime_evaluation_failure_commits_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, *_ = _coordinator()

    def fail_evaluate(*args: object, **kwargs: object) -> object:
        raise RuntimeError("fixed-local-test-failure")

    monkeypatch.setattr(SandboxSafetyRuleAdapter, "evaluate", fail_evaluate)
    outcome = coordinator.apply_revision(_revision_command(coordinator))
    snapshot = coordinator.snapshot()

    assert outcome.status == "recheck_failed"
    assert len(snapshot.revisions) == 2
    assert len(snapshot.runs) == len(snapshot.invalidations) == 1
    assert snapshot.revisions[-1].result is None


def test_l5_4_review_setup_failure_keeps_old_authority_invalidated() -> None:
    coordinator, _, _, nonce_factory, _ = _coordinator()
    nonce_factory.value = b"invalid"
    outcome = coordinator.apply_revision(_revision_command(coordinator))
    snapshot = coordinator.snapshot()

    assert outcome.status == "review_setup_failed"
    assert outcome.delivery is None
    assert len(snapshot.revisions) == 2
    assert len(snapshot.invalidations) == 1
    assert snapshot.revisions[-1].result is not None
    assert snapshot.revisions[-1].challenge_ref is None


def test_l5_4_exact_retry_and_thirty_two_concurrent_modifications_are_single_use() -> None:
    coordinator, _, _, nonce_factory, _ = _coordinator()
    command = _revision_command(coordinator)
    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = tuple(pool.map(lambda _: coordinator.apply_revision(command), range(32)))

    assert [outcome.status for outcome in outcomes].count("review_required") == 1
    assert [outcome.status for outcome in outcomes].count("replayed_or_conflict") == 31
    assert sum(outcome.delivery is not None for outcome in outcomes) == 1
    snapshot = coordinator.snapshot()
    assert len(snapshot.revisions) == 2
    assert len(snapshot.runs) == len(snapshot.invalidations) == len(snapshot.receipts) == 1
    assert nonce_factory.calls == 2
    before = snapshot
    retry = coordinator.apply_revision(command)
    assert retry.status == "replayed_or_conflict"
    assert retry.delivery is None
    assert coordinator.snapshot() == before
    assert nonce_factory.calls == 2


def test_l5_4_restart_preserves_pending_and_confirmed_current_review() -> None:
    coordinator, _, clock, nonce_factory, verifier = _coordinator()
    command = _revision_command(coordinator)
    outcome = coordinator.apply_revision(command)
    assert outcome.delivery is not None
    pending_snapshot = coordinator.snapshot()
    restarted = SandboxRecheckCoordinator(
        snapshot=pending_snapshot,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=verifier,
    )
    assert restarted.snapshot() == pending_snapshot
    assert nonce_factory.calls == 2
    assert restarted.apply_revision(command).status == "replayed_or_conflict"
    assert restarted.snapshot() == pending_snapshot
    staged = restarted.stage_current_review(
        _submission(outcome.delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    assert restarted.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    ).status == "applied"
    confirmed = restarted.snapshot()
    restarted_again = SandboxRecheckCoordinator(
        snapshot=confirmed,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=verifier,
    )
    assert restarted_again.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "eligible"
    assert nonce_factory.calls == 2


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    (
        ("blocked", "blocked"),
        ("unknown_adapter", "recheck_failed"),
        ("review_setup", "review_setup_failed"),
    ),
)
def test_l5_4_restart_preserves_terminal_blocked_states_and_receipts(
    mode: str, expected_status: str
) -> None:
    coordinator, _, clock, nonce_factory, verifier = _coordinator()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="restart.2",
        decision=(
            SandboxSafetyDecision.BLOCK
            if mode == "blocked"
            else SandboxSafetyDecision.ALLOW
        ),
        issue_count=1 if mode == "blocked" else 0,
    )
    if mode == "unknown_adapter":
        candidate = candidate.model_copy(
            update={"adapter_version": "sandbox-safety-adapter.v2"}
        )
        bundle = bundle.model_copy(
            update={"adapter_version": "sandbox-safety-adapter.v2"}
        )
    elif mode == "review_setup":
        nonce_factory.value = b"invalid"
    command = _revision_command(
        coordinator, candidate=candidate, bundle=bundle, suffix=f"restart-{mode}"
    )
    outcome = coordinator.apply_revision(command)
    snapshot = coordinator.snapshot()
    calls = nonce_factory.calls

    restarted = SandboxRecheckCoordinator(
        snapshot=snapshot,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=verifier,
    )
    assert outcome.status == expected_status
    assert restarted.snapshot() == snapshot
    assert restarted.apply_revision(command).status == "replayed_or_conflict"
    assert restarted.snapshot() == snapshot
    assert nonce_factory.calls == calls
    assert restarted.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"


def test_l5_4_next_revision_requires_a_current_modify_review() -> None:
    coordinator, *_ = _coordinator()
    first = coordinator.apply_revision(_revision_command(coordinator))
    assert first.delivery is not None
    staged = coordinator.stage_current_review(
        _submission(first.delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    coordinator.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    before = coordinator.snapshot()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=9,
        formula_revision=5,
        amount_milliunits=3,
        dataset_version="3.0.0",
    )
    with pytest.raises(SandboxRecheckError):
        coordinator.apply_revision(
            _revision_command(
                coordinator, candidate=candidate, bundle=bundle, suffix="009"
            )
        )
    assert coordinator.snapshot() == before


def test_l5_4_true_max_full_recheck_preserves_all_issues() -> None:
    coordinator, *_ = _coordinator()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=10,
        dataset_version="max.1",
        decision=SandboxSafetyDecision.BLOCK,
        formula_count=64,
        issue_count=256,
    )
    outcome = coordinator.apply_revision(
        _revision_command(coordinator, candidate=candidate, bundle=bundle, suffix="max")
    )
    result = coordinator.snapshot().revisions[-1].result
    assert outcome.status == "blocked"
    assert result is not None
    assert len(result.issues) == 256
    assert result.decision_subject_digest == hashlib.sha256(
        canonical_json_bytes(candidate)
    ).hexdigest()


def test_l5_4_combined_snapshot_rejects_semantic_mismatches_chainlessly() -> None:
    coordinator, _, clock, nonce_factory, verifier = _coordinator()
    coordinator.apply_revision(_revision_command(coordinator))
    baseline = coordinator.snapshot().model_dump(mode="python")
    bad = dict(baseline)
    bad_runs = [dict(item) for item in baseline["runs"]]
    bad_receipts = [dict(item) for item in baseline["receipts"]]
    bad_runs[0]["command_id"] = "sandbox-recheck-command-different"
    bad_runs[0]["run_ref"] = _derived_mapping_ref(
        "sandbox-recheck-run-", bad_runs[0], "run_ref"
    )
    bad_receipts[0]["command_id"] = bad_runs[0]["command_id"]
    bad_receipts[0]["receipt_ref"] = _derived_mapping_ref(
        "sandbox-recheck-receipt-", bad_receipts[0], "receipt_ref"
    )
    bad["runs"] = bad_runs
    bad["receipts"] = bad_receipts

    with pytest.raises(SandboxRecheckError) as raised:
        SandboxRecheckCoordinator(
            snapshot=bad,
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=verifier,
        )
    assert str(raised.value) == "SANDBOX_RECHECK_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "mutation",
    (
        "current_pointer",
        "missing_run",
        "run_sequence",
        "receipt_link",
        "invalidation_refs",
    ),
)
def test_l5_4_combined_snapshot_rejects_each_outer_integrity_mismatch(
    mutation: str,
) -> None:
    coordinator, _, clock, nonce_factory, verifier = _coordinator()
    coordinator.apply_revision(_revision_command(coordinator))
    bad = copy.deepcopy(coordinator.snapshot().model_dump(mode="python"))
    if mutation == "current_pointer":
        bad["current_revision_ref"] = bad["revisions"][0]["revision_ref"]
    elif mutation == "missing_run":
        bad["runs"] = []
    elif mutation == "run_sequence":
        bad["runs"][0]["sequence"] = 1
        bad["runs"][0]["run_ref"] = _derived_mapping_ref(
            "sandbox-recheck-run-", bad["runs"][0], "run_ref"
        )
    elif mutation == "receipt_link":
        bad["receipts"][0]["new_revision_ref"] = bad["revisions"][0][
            "revision_ref"
        ]
        bad["receipts"][0]["receipt_ref"] = _derived_mapping_ref(
            "sandbox-recheck-receipt-", bad["receipts"][0], "receipt_ref"
        )
    else:
        bad["invalidations"][0]["old_challenge_refs"] = []
        bad["invalidations"][0]["invalidation_ref"] = _derived_mapping_ref(
            "sandbox-recheck-invalidation-",
            bad["invalidations"][0],
            "invalidation_ref",
        )

    with pytest.raises(SandboxRecheckError) as raised:
        SandboxRecheckCoordinator(
            snapshot=bad,
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=verifier,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("drift", ("terminal_schema", "middle_namespace"))
def test_l5_4_restart_rejects_rederived_revision_authority_drift(
    drift: str,
) -> None:
    coordinator, _, clock, nonce_factory, verifier = _coordinator()
    if drift == "terminal_schema":
        candidate, bundle = _subject_and_bundle(
            domain_state_version=8,
            formula_revision=4,
            amount_milliunits=2,
            dataset_version="authority.2",
            decision=SandboxSafetyDecision.BLOCK,
            issue_count=1,
        )
        coordinator.apply_revision(
            _revision_command(
                coordinator, candidate=candidate, bundle=bundle, suffix="authority-008"
            )
        )
        bad = copy.deepcopy(coordinator.snapshot().model_dump(mode="python"))
        bad["revisions"][1]["review_schema_version"] = "forged-review-schema.v9"
        new_revision_ref = _refresh_mapping_ref(
            bad["revisions"][1],
            prefix="sandbox-recheck-revision-",
            field="revision_ref",
        )
        bad["current_revision_ref"] = new_revision_ref
        for key, prefix, field in (
            ("runs", "sandbox-recheck-run-", "run_ref"),
            ("invalidations", "sandbox-recheck-invalidation-", "invalidation_ref"),
            ("receipts", "sandbox-recheck-receipt-", "receipt_ref"),
        ):
            bad[key][0]["new_revision_ref"] = new_revision_ref
            _refresh_mapping_ref(bad[key][0], prefix=prefix, field=field)
    else:
        first = coordinator.apply_revision(_revision_command(coordinator))
        assert first.delivery is not None
        staged = coordinator.stage_current_review(
            _submission(first.delivery, SandboxReviewAction.MODIFY_FIXTURE)
        )
        assert staged.resume_attempt_ref is not None
        assert coordinator.resume_current_review(
            SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
        ).status == "applied"
        candidate, bundle = _subject_and_bundle(
            domain_state_version=9,
            formula_revision=5,
            amount_milliunits=3,
            dataset_version="authority.3",
            decision=SandboxSafetyDecision.BLOCK,
            issue_count=1,
        )
        coordinator.apply_revision(
            _revision_command(
                coordinator, candidate=candidate, bundle=bundle, suffix="authority-009"
            )
        )
        snapshot = coordinator.snapshot()
        bad = copy.deepcopy(snapshot.model_dump(mode="python"))
        bad["revisions"][1]["namespace"] = "sandbox.recheck.forged"
        middle_ref = _refresh_mapping_ref(
            bad["revisions"][1],
            prefix="sandbox-recheck-revision-",
            field="revision_ref",
        )
        for key, prefix, field in (
            ("runs", "sandbox-recheck-run-", "run_ref"),
            ("invalidations", "sandbox-recheck-invalidation-", "invalidation_ref"),
            ("receipts", "sandbox-recheck-receipt-", "receipt_ref"),
        ):
            bad[key][0]["new_revision_ref"] = middle_ref
            _refresh_mapping_ref(bad[key][0], prefix=prefix, field=field)

        final_revision = bad["revisions"][2]
        final_run = bad["runs"][1]
        final_bundle = snapshot.revisions[2].rule_bundle
        assert final_bundle is not None
        reconstructed = SandboxRevisionCommandV1(
            expected_current_revision_ref=middle_ref,
            command_id=final_run["command_id"],
            run_id=final_run["run_id"],
            trace_id=final_run["trace_id"],
            candidate_subject=snapshot.revisions[2].subject,
            rule_bundle=final_bundle,
            checkpoint_id=final_revision["checkpoint_id"],
            interrupt_id=final_revision["interrupt_id"],
        )
        command_digest = hashlib.sha256(canonical_json_bytes(reconstructed)).hexdigest()
        final_revision["parent_revision_ref"] = middle_ref
        final_revision["accepted_command_digest"] = command_digest
        final_ref = _refresh_mapping_ref(
            final_revision,
            prefix="sandbox-recheck-revision-",
            field="revision_ref",
        )
        final_run["command_digest"] = command_digest
        final_run["old_revision_ref"] = middle_ref
        final_run["new_revision_ref"] = final_ref
        _refresh_mapping_ref(
            final_run, prefix="sandbox-recheck-run-", field="run_ref"
        )
        bad["invalidations"][1]["old_revision_ref"] = middle_ref
        bad["invalidations"][1]["new_revision_ref"] = final_ref
        _refresh_mapping_ref(
            bad["invalidations"][1],
            prefix="sandbox-recheck-invalidation-",
            field="invalidation_ref",
        )
        bad["receipts"][1]["command_digest"] = command_digest
        bad["receipts"][1]["old_revision_ref"] = middle_ref
        bad["receipts"][1]["new_revision_ref"] = final_ref
        _refresh_mapping_ref(
            bad["receipts"][1],
            prefix="sandbox-recheck-receipt-",
            field="receipt_ref",
        )
        bad["current_revision_ref"] = final_ref

    with pytest.raises(SandboxRecheckError) as raised:
        SandboxRecheckCoordinator(
            snapshot=bad,
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=verifier,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l5_4_source_build_failure_commits_fixed_review_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, review_snapshot, clock, nonce_factory, verifier = _coordinator()
    command = _revision_command(coordinator, suffix="source-build-failure")

    def fail_source_build(*args: object, **kwargs: object) -> object:
        raise RuntimeError("dynamic-review-build-detail")

    monkeypatch.setattr(SandboxReviewSourceV1, "build", fail_source_build)
    outcome = coordinator.apply_revision(command)
    snapshot = coordinator.snapshot()

    assert outcome.status == "review_setup_failed"
    assert outcome.delivery is None
    assert len(snapshot.revisions) == 2
    assert len(snapshot.runs) == len(snapshot.invalidations) == len(snapshot.receipts) == 1
    assert snapshot.revisions[-1].result is not None
    assert snapshot.revisions[-1].review_render_digest is None
    assert snapshot.revisions[-1].challenge_ref is None
    assert snapshot.invalidations[-1].old_challenge_refs == (
        review_snapshot.challenges[-1].challenge_ref,
    )
    assert snapshot.invalidations[-1].old_event_refs == (
        review_snapshot.events[-1].event_ref,
    )
    assert b"dynamic-review-build-detail" not in canonical_json_bytes(snapshot)
    assert coordinator.completion_eligibility(
        namespace=_NAMESPACE,
        test_session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    ).status == "blocked"

    restarted = SandboxRecheckCoordinator(
        snapshot=snapshot,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=verifier,
    )
    assert restarted.snapshot() == snapshot
    assert restarted.apply_revision(command).status == "replayed_or_conflict"
    assert restarted.snapshot() == snapshot


def test_l5_4_snapshot_and_inputs_are_isolated_immutable_copies() -> None:
    coordinator, *_ = _coordinator()
    command = _revision_command(coordinator)
    coordinator.apply_revision(command)
    expected = coordinator.snapshot()
    dumped = expected.model_dump(mode="python")
    dumped["revisions"][-1]["checkpoint_id"] = "changed-outside-store"
    assert coordinator.snapshot() == expected
    assert expected.revisions[-1].subject is not command.candidate_subject


def test_l5_4_production_has_only_offline_reference_capabilities() -> None:
    path = Path("app/agent_runtime/sandbox_recheck.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not imported.intersection(
        {
            "os",
            "socket",
            "requests",
            "httpx",
            "aiohttp",
            "subprocess",
            "sqlalchemy",
            "redis",
            "pymilvus",
            "langgraph",
        }
    )
    source = path.read_text(encoding="utf-8")
    for token in ("getenv", ".env", "MODEL_GATEWAY", "DB_URL", "http://", "https://"):
        assert token not in source


def test_l5_4_public_contract_is_importable() -> None:
    assert SandboxRecheckCoordinator is not None
    assert SandboxRecheckError is not None
    assert SandboxRevisionCommandV1 is not None
