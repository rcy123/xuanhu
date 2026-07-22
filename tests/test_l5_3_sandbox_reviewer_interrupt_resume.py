from __future__ import annotations

import ast
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_runtime.sandbox_explanation import (
    SandboxExplanationAllowlistBundleV1,
    SandboxExplanationAllowlistEntryV1,
    SandboxExplanationCandidateStatementV1,
    SandboxExplanationCandidateV1,
    SandboxExplanationPortInputV1,
    SandboxExplanationResultV1,
    SandboxSafetyExplanationAgent,
)
from app.agent_runtime.sandbox_review import (
    MAX_RESUME_SUBMISSION_BYTES,
    SandboxInMemoryReviewStore,
    SandboxResumeCommandV1,
    SandboxResumeSubmissionV1,
    SandboxReviewAction,
    SandboxReviewCoordinator,
    SandboxReviewError,
    SandboxReviewSourceV1,
    SandboxTestReviewProofV1,
    canonical_review_bytes,
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
)

_NAMESPACE = "sandbox.review.local"
_THREAD_ID = "sandbox-thread-001"
_CHECKPOINT_ID = "sandbox-checkpoint-001"
_INTERRUPT_ID = "sandbox-interrupt-001"
_REVIEWER_ID = "sandbox-reviewer-fixture-001"
_SCHEME = "sandbox-test-sha256.v1"
_KEY_ID = "sandbox-test-key-001"
_NONCE = bytes(range(32))


class _FakeClock:
    def __init__(self, value: int = 2_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _FakeNonceFactory:
    def __init__(self, value: bytes = _NONCE) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        return self.value


def _raise_nested_review_error(secret: str) -> None:
    try:
        raise RuntimeError(secret)
    except RuntimeError as cause:
        try:
            raise ValueError(f"outer:{secret}") from cause
        except ValueError:
            raise SandboxReviewError() from None


class _NestedReviewErrorNonceFactory:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        _raise_nested_review_error(self.secret)
        raise AssertionError("unreachable")


class _NestedReviewErrorStore(SandboxInMemoryReviewStore):
    def recover_challenge(self, **kwargs: object) -> object:
        del kwargs
        _raise_nested_review_error("nested-store-secret")
        raise AssertionError("unreachable")


class _FakeSignatureVerifier:
    def __init__(self, *, raises: str | None = None) -> None:
        self.calls = 0
        self.raises = raises

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
        self.calls += 1
        if self.raises is not None:
            raise RuntimeError(self.raises)
        return (
            sandbox_test_signature_scheme == _SCHEME
            and sandbox_test_key_id == _KEY_ID
            and sandbox_test_signature == self.signature(signed_payload_digest)
        )


class _ExplanationPort:
    def generate(self, request: SandboxExplanationPortInputV1) -> object:
        entry = request.allowlist_entries[0]
        issue = request.issue_refs[0]
        return SandboxExplanationCandidateV1(
            statements=(
                SandboxExplanationCandidateStatementV1(
                    issue_id=issue.issue_id,
                    rule_id=entry.rule_id,
                    text=entry.text,
                ),
            )
        )


def _accepted_source(
    *,
    test_session_id: str = "sandbox-review-session-001",
    graph_version: str = "sandbox-review-graph.v1",
    domain_state_version: int = 7,
    formula_revision: int = 3,
    decision: SandboxSafetyDecision = SandboxSafetyDecision.ALLOW,
) -> SandboxReviewSourceV1:
    formula_items = (
        SandboxFormulaItemV1(
            item_id="synthetic-item-001",
            component="fixed-fictitious-component",
            amount_milliunits=1,
            unit="synthetic_unit",
        ),
    )
    profile_facts = (
        SandboxProfileFactV1(
            fact_id="synthetic-profile-fact-001",
            name="fixed-fictitious-technical-profile",
            value="bounded-test-value",
        ),
    )
    manifest = SandboxSyntheticManifestV1.build(
        dataset_name="fixed-fictitious-review-fixture",
        dataset_version="1.0.0",
        formula_items=formula_items,
        profile_facts=profile_facts,
    )
    issue = SandboxSafetyIssueV1(
        issue_id="sx.review.issue.001",
        rule_id="sx.review.rule.001",
        severity=SandboxSafetySeverity.HIGH,
        execution_order=0,
    )
    issues = () if decision is SandboxSafetyDecision.ALLOW else (issue,)
    evaluation_case = SandboxEvaluationCaseV1.build(
        case_id="fixed-fictitious-review-case-001",
        formula_items=formula_items,
        profile_facts=profile_facts,
        manifest=manifest,
        evaluation=SandboxSafetyEvaluationV1(
            decision=decision,
            issues=issues,
        ),
    )
    authority = SandboxEvaluatorAuthorityV1.build(cases=(evaluation_case,))
    bundle = SandboxRuleBundleV1.build(
        rule_bundle_version="sandbox-review-rules.v1",
        rules=(
            ()
            if decision is SandboxSafetyDecision.ALLOW
            else (
                SandboxRuleV1(
                    rule_id=issue.rule_id,
                    rule_revision=1,
                    parameters=(),
                ),
            )
        ),
        evaluator_authority=authority,
    )
    subject = SandboxSafetySubjectV1.build(
        test_session_id=test_session_id,
        domain_state_version=domain_state_version,
        formula_artifact_id="synthetic-review-formula-001",
        formula_revision=formula_revision,
        formula_items=formula_items,
        profile_artifact_id="synthetic-review-profile-001",
        profile_revision=2,
        profile_facts=profile_facts,
        graph_version=graph_version,
        rule_bundle_version=bundle.rule_bundle_version,
        rule_bundle_digest=bundle.rule_bundle_digest,
        evaluator_authority_digest=authority.authority_digest,
        synthetic_manifest=manifest,
    )
    result = SandboxSafetyRuleAdapter().evaluate(
        subject,
        bundle,
        command_id="sandbox-review-command-001",
        run_id="sandbox-review-run-001",
        trace_id="sandbox-review-trace-001",
    )
    allowlist = SandboxExplanationAllowlistBundleV1.build(
        entries=(
            ()
            if decision is SandboxSafetyDecision.ALLOW
            else (
                SandboxExplanationAllowlistEntryV1(
                    rule_id=issue.rule_id,
                    text="fixed sandbox explanation for review rule 001",
                ),
            )
        )
    )
    explanation = SandboxSafetyExplanationAgent(_ExplanationPort()).explain(
        result,
        allowlist,
    )
    assert isinstance(explanation, SandboxExplanationResultV1)
    return SandboxReviewSourceV1.build(
        safety_subject=subject,
        safety_result=result,
        explanation_result=explanation,
    )


def _coordinator(
    *,
    store: SandboxInMemoryReviewStore | None = None,
    clock: _FakeClock | None = None,
    nonce_factory: _FakeNonceFactory | None = None,
    verifier: _FakeSignatureVerifier | None = None,
) -> tuple[
    SandboxReviewCoordinator,
    SandboxInMemoryReviewStore,
    _FakeClock,
    _FakeNonceFactory,
    _FakeSignatureVerifier,
]:
    actual_store = SandboxInMemoryReviewStore() if store is None else store
    actual_clock = _FakeClock() if clock is None else clock
    actual_nonce_factory = (
        _FakeNonceFactory() if nonce_factory is None else nonce_factory
    )
    actual_verifier = _FakeSignatureVerifier() if verifier is None else verifier
    return (
        SandboxReviewCoordinator(
            store=actual_store,
            clock=actual_clock,
            nonce_factory=actual_nonce_factory,
            signature_verifier=actual_verifier,
        ),
        actual_store,
        actual_clock,
        actual_nonce_factory,
        actual_verifier,
    )


def _issue(
    coordinator: SandboxReviewCoordinator,
    source: SandboxReviewSourceV1 | None = None,
):
    return coordinator.create_single_use_challenge(
        _accepted_source() if source is None else source,
        namespace=_NAMESPACE,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
        interrupt_id=_INTERRUPT_ID,
    )


def _submission(delivery, action: SandboxReviewAction) -> SandboxResumeSubmissionV1:
    assert delivery.plaintext_nonce is not None
    payload_digest = review_signed_payload_digest(
        challenge=delivery.challenge,
        action=action,
        plaintext_nonce=delivery.plaintext_nonce,
        sandbox_test_reviewer_id=_REVIEWER_ID,
        sandbox_test_role="sandbox_reviewer_test_role",
        sandbox_test_organization_label="local_synthetic_sandbox",
        sandbox_test_qualification_label="not_a_medical_credential",
        sandbox_test_signature_scheme=_SCHEME,
        sandbox_test_key_id=_KEY_ID,
    )
    proof = SandboxTestReviewProofV1(
        sandbox_test_reviewer_id=_REVIEWER_ID,
        sandbox_test_role="sandbox_reviewer_test_role",
        sandbox_test_organization_label="local_synthetic_sandbox",
        sandbox_test_qualification_label="not_a_medical_credential",
        sandbox_test_signature_scheme=_SCHEME,
        sandbox_test_key_id=_KEY_ID,
        sandbox_test_signed_payload_digest=payload_digest,
        sandbox_test_signature=_FakeSignatureVerifier.signature(payload_digest),
    )
    return SandboxResumeSubmissionV1(
        namespace=_NAMESPACE,
        test_session_id=delivery.challenge.test_session_id,
        challenge=delivery.challenge,
        action=action,
        plaintext_nonce=delivery.plaintext_nonce,
        proof=proof,
    )


def _stage_and_resume(
    coordinator: SandboxReviewCoordinator,
    delivery,
    action: SandboxReviewAction,
):
    staged = coordinator.stage_verified_resume_attempt(_submission(delivery, action))
    assert staged.status == "staged"
    assert staged.resume_attempt_ref is not None
    resumed = coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    return staged, resumed


@pytest.mark.parametrize(
    ("action", "expected_eligibility"),
    (
        (SandboxReviewAction.CONFIRM, "eligible"),
        (SandboxReviewAction.REJECT, "blocked"),
    ),
)
def test_l5_3_valid_confirm_and_reject_apply_exactly_once_with_all_bindings(
    action: SandboxReviewAction,
    expected_eligibility: str,
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    _, resumed = _stage_and_resume(coordinator, delivery, action)

    assert resumed.status == "applied"
    assert coordinator.resume(resumed.command).status == "replayed_or_conflict"
    snapshot = store.snapshot()
    assert len(snapshot.events) == 1
    event = snapshot.events[0]
    challenge = delivery.challenge
    for field in (
        "sandbox_schema_version",
        "adapter_version",
        "graph_version",
        "namespace",
        "test_session_id",
        "thread_id",
        "checkpoint_id",
        "interrupt_id",
        "domain_state_version",
        "formula_revision",
        "input_digest",
        "result_digest",
        "rule_bundle_digest",
        "synthetic_dataset_digest",
        "review_render_digest",
    ):
        assert getattr(event, field) == getattr(challenge, field)
    assert event.action is action
    assert event.sandbox_test_reviewer_id == _REVIEWER_ID
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == expected_eligibility


def test_l5_3_resume_command_contains_only_resume_attempt_ref() -> None:
    assert set(SandboxResumeCommandV1.model_fields) == {"resume_attempt_ref"}
    command = SandboxResumeCommandV1(resume_attempt_ref="sandbox-attempt-001")
    assert command.model_dump() == {"resume_attempt_ref": "sandbox-attempt-001"}
    with pytest.raises(ValidationError):
        SandboxResumeCommandV1.model_validate(
            {"resume_attempt_ref": "sandbox-attempt-001", "action": "confirm"}
        )
    with pytest.raises(ValidationError):
        SandboxResumeCommandV1.model_validate({"resume_attempt_ref": 1})


def test_l5_3_test_identity_fields_are_exact_and_non_credentialing() -> None:
    assert set(SandboxTestReviewProofV1.model_fields) == {
        "sandbox_test_reviewer_id",
        "sandbox_test_role",
        "sandbox_test_organization_label",
        "sandbox_test_qualification_label",
        "sandbox_test_signature_scheme",
        "sandbox_test_key_id",
        "sandbox_test_signed_payload_digest",
        "sandbox_test_signature",
    }
    coordinator, *_ = _coordinator()
    proof = _submission(_issue(coordinator), SandboxReviewAction.CONFIRM).proof
    rendered = canonical_review_bytes(proof).decode()
    assert "sandbox_reviewer_test_role" in rendered
    assert "local_synthetic_sandbox" in rendered
    assert "not_a_medical_credential" in rendered
    for prohibited in ("doctor", "physician", "clinician", "license", "credential-approved"):
        assert prohibited not in rendered.lower()


def test_l5_3_plaintext_nonce_is_returned_once_and_never_persisted_or_rendered() -> None:
    coordinator, store, _, nonce_factory, _ = _coordinator()
    first = _issue(coordinator)
    retried = _issue(coordinator)
    restarted = SandboxReviewCoordinator(
        store=store,
        clock=_FakeClock(),
        nonce_factory=nonce_factory,
        signature_verifier=_FakeSignatureVerifier(),
    ).create_single_use_challenge(
        _accepted_source(),
        namespace=_NAMESPACE,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
        interrupt_id=_INTERRUPT_ID,
    )

    assert first.plaintext_nonce == _NONCE
    assert retried.plaintext_nonce is None
    assert restarted.plaintext_nonce is None
    assert first.challenge.challenge_ref == retried.challenge.challenge_ref
    assert retried.challenge.challenge_ref == restarted.challenge.challenge_ref
    assert nonce_factory.calls == 1
    assert _NONCE.hex() not in repr(first)
    assert repr(_NONCE) not in repr(first)
    assert _NONCE.hex() not in repr(first.challenge)
    assert repr(_NONCE) not in repr(first.challenge)
    persisted = canonical_review_bytes(store.snapshot())
    assert _NONCE.hex().encode() not in persisted
    assert repr(_NONCE) not in repr(store.snapshot())


def test_l5_3_blocked_safety_result_cannot_create_challenge_or_review() -> None:
    coordinator, store, _, nonce_factory, _ = _coordinator()
    blocked_source = _accepted_source(decision=SandboxSafetyDecision.BLOCK)

    with pytest.raises(SandboxReviewError) as raised:
        _issue(coordinator, blocked_source)

    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert nonce_factory.calls == 0
    assert store.operation_count == 0
    snapshot = store.snapshot()
    assert snapshot.challenges == ()
    assert snapshot.checkpoints == ()
    assert snapshot.events == ()
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=blocked_source.safety_subject.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == "blocked"


@pytest.mark.parametrize("bad_nonce", (b"x" * 31, b"x" * 33))
def test_l5_3_nonce_factory_requires_exactly_256_bits_before_any_store_write(
    bad_nonce: bytes,
) -> None:
    store = SandboxInMemoryReviewStore()
    coordinator, _, _, _, _ = _coordinator(
        store=store,
        nonce_factory=_FakeNonceFactory(bad_nonce),
    )
    with pytest.raises(SandboxReviewError) as raised:
        _issue(coordinator)
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert store.operation_count == 0
    assert store.snapshot().challenges == ()


@pytest.mark.parametrize(
    "field",
    (
        "sandbox_schema_version",
        "adapter_version",
        "graph_version",
        "thread_id",
        "checkpoint_id",
        "interrupt_id",
        "domain_state_version",
        "formula_revision",
        "input_digest",
        "result_digest",
        "rule_bundle_digest",
        "synthetic_dataset_digest",
        "review_render_digest",
    ),
)
def test_l5_3_stale_versions_and_every_bound_digest_are_fixed_rejected(
    field: str,
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)
    value = getattr(delivery.challenge, field)
    replacement = value + 1 if isinstance(value, int) else (
        "f" * 64 if value != "f" * 64 and field.endswith("digest") else f"{value}.stale"
    )
    changed = delivery.challenge.model_copy(update={field: replacement})
    submission = submission.model_copy(update={"challenge": changed})
    before = store.snapshot()

    rejected = coordinator.stage_verified_resume_attempt(submission)

    assert rejected.status == "resume_rejected"
    assert rejected.resume_attempt_ref is None
    after = store.snapshot()
    assert after.checkpoints == before.checkpoints
    assert after.events == before.events


def test_l5_3_replay_cross_session_namespace_expiry_nonce_and_signature_are_rejected() -> None:
    def assert_stage_rejected(change: str) -> None:
        coordinator, store, clock, *_ = _coordinator()
        delivery = _issue(coordinator)
        submission = _submission(delivery, SandboxReviewAction.CONFIRM)
        if change == "namespace":
            submission = submission.model_copy(update={"namespace": "sandbox.other"})
        elif change == "session":
            submission = submission.model_copy(update={"test_session_id": "sandbox-other"})
        elif change == "nonce":
            submission = submission.model_copy(update={"plaintext_nonce": b"z" * 32})
        elif change == "signature":
            proof = submission.proof.model_copy(
                update={"sandbox_test_signature": "0" * 64}
            )
            submission = submission.model_copy(update={"proof": proof})
        elif change == "expiry":
            clock.value = delivery.challenge.expires_at + 1
        before = store.snapshot()
        rejected = coordinator.stage_verified_resume_attempt(submission)
        assert rejected.status == "resume_rejected"
        after = store.snapshot()
        assert after.events == before.events
        assert after.checkpoints == before.checkpoints

    for change in ("namespace", "session", "nonce", "signature", "expiry"):
        assert_stage_rejected(change)

    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    staged, first = _stage_and_resume(
        coordinator,
        delivery,
        SandboxReviewAction.CONFIRM,
    )
    assert first.status == "applied"
    assert staged.resume_attempt_ref is not None
    before = store.snapshot()
    replay = coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    assert replay.status == "replayed_or_conflict"
    assert store.snapshot().events == before.events


def test_l5_3_expired_nonce_does_not_revive_when_fake_clock_moves_back() -> None:
    clock = _FakeClock()
    coordinator, store, *_ = _coordinator(clock=clock)
    delivery = _issue(coordinator)
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)
    clock.value = delivery.challenge.expires_at + 1
    assert coordinator.stage_verified_resume_attempt(submission).status == "resume_rejected"
    assert store.snapshot().challenges[0].state == "expired"

    clock.value = delivery.challenge.issued_at
    assert coordinator.stage_verified_resume_attempt(submission).status == "resume_rejected"
    assert store.snapshot().challenges[0].state == "expired"
    assert store.snapshot().events == ()


def test_l5_3_exact_expiry_is_rejected_during_stage_and_resume() -> None:
    stage_clock = _FakeClock()
    stage_coordinator, stage_store, *_ = _coordinator(clock=stage_clock)
    stage_delivery = _issue(stage_coordinator)
    stage_submission = _submission(stage_delivery, SandboxReviewAction.CONFIRM)
    stage_clock.value = stage_delivery.challenge.expires_at
    stage_result = stage_coordinator.stage_verified_resume_attempt(stage_submission)
    stage_snapshot = stage_store.snapshot()
    stage_clock.value = stage_delivery.challenge.issued_at
    stage_after_rollback = stage_coordinator.stage_verified_resume_attempt(
        stage_submission
    )

    resume_clock = _FakeClock()
    resume_coordinator, resume_store, *_ = _coordinator(clock=resume_clock)
    resume_delivery = _issue(resume_coordinator)
    staged = resume_coordinator.stage_verified_resume_attempt(
        _submission(resume_delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    resume_clock.value = resume_delivery.challenge.expires_at
    resume_result = resume_coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    resume_snapshot = resume_store.snapshot()

    assert (stage_result.status, resume_result.status) == (
        "resume_rejected",
        "resume_rejected",
    )
    assert stage_after_rollback.status == "resume_rejected"
    assert stage_snapshot.challenges[0].state == "expired"
    assert stage_snapshot.checkpoints[0].state == "review_pending"
    assert stage_snapshot.attempts == ()
    assert stage_snapshot.events == ()
    assert resume_snapshot.challenges[0].state == "expired"
    assert resume_snapshot.checkpoints[0].state == "review_pending"
    assert resume_snapshot.attempts[0].state == "sealed"
    assert resume_snapshot.events == ()


def test_l5_3_thirty_two_concurrent_resumes_have_exactly_one_success() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    staged = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    command = SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = tuple(pool.map(lambda _: coordinator.resume(command).status, range(32)))

    assert outcomes.count("applied") == 1
    assert outcomes.count("replayed_or_conflict") == 31
    snapshot = store.snapshot()
    assert len(snapshot.events) == 1
    assert sum(
        transition.to_state == "review_applied"
        for transition in snapshot.transitions
    ) == 1


def test_l5_3_fake_restart_recovers_exact_checkpoint_without_reissuing_challenge() -> None:
    first, store, clock, nonce_factory, _ = _coordinator()
    delivery = _issue(first)
    restarted = SandboxReviewCoordinator(
        store=store,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=_FakeSignatureVerifier(),
    )
    recovered = _issue(restarted)
    assert recovered.challenge == delivery.challenge
    assert recovered.plaintext_nonce is None
    assert nonce_factory.calls == 1

    staged = restarted.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    result = restarted.resume(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    assert result.status == "applied"
    assert len(store.snapshot().events) == 1


@pytest.mark.parametrize(
    "tamper",
    (
        "event_action",
        "event_ref",
        "attempt_ref",
        "transition_ref",
        "source_ref",
        "duplicate_event",
        "reordered_transitions",
        "missing_challenge",
        "event_identity",
        "missing_current_authority",
    ),
)
def test_l5_3_restart_snapshot_rejects_changed_event_action_and_derived_refs(
    tamper: str,
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    _stage_and_resume(coordinator, delivery, SandboxReviewAction.REJECT)
    snapshot = store.snapshot().model_dump(mode="python")

    if tamper == "event_action":
        snapshot["events"][0]["action"] = SandboxReviewAction.CONFIRM
    elif tamper == "event_ref":
        snapshot["events"][0]["event_ref"] = "sandbox-review-event-" + "f" * 64
    elif tamper == "attempt_ref":
        snapshot["attempts"][0]["resume_attempt_ref"] = (
            "sandbox-attempt-" + "f" * 64
        )
    elif tamper == "transition_ref":
        snapshot["transitions"][-1]["transition_ref"] = (
            "sandbox-transition-" + "f" * 64
        )
    elif tamper == "source_ref":
        snapshot["sources"][0]["source_ref"] = "sandbox-source-" + "f" * 64
    elif tamper == "duplicate_event":
        snapshot["events"] = snapshot["events"] + (dict(snapshot["events"][0]),)
    elif tamper == "reordered_transitions":
        snapshot["transitions"] = tuple(reversed(snapshot["transitions"]))
    elif tamper == "missing_challenge":
        snapshot["challenges"] = ()
    elif tamper == "event_identity":
        snapshot["events"][0]["sandbox_test_reviewer_id"] = "tampered-reviewer"
    elif tamper == "missing_current_authority":
        snapshot.pop("current_authorities", None)

    with pytest.raises(SandboxReviewError) as raised:
        SandboxInMemoryReviewStore(snapshot=snapshot)
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l5_3_new_current_authority_blocks_prior_checkpoint_eligibility() -> None:
    coordinator, store, clock, nonce_factory, _ = _coordinator()
    first_delivery = _issue(coordinator)
    _stage_and_resume(coordinator, first_delivery, SandboxReviewAction.CONFIRM)
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=first_delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == "eligible"

    second_source = _accepted_source(domain_state_version=8)
    second_delivery = coordinator.create_single_use_challenge(
        second_source,
        namespace=_NAMESPACE,
        thread_id=_THREAD_ID,
        checkpoint_id="sandbox-checkpoint-002",
        interrupt_id="sandbox-interrupt-002",
    )
    assert second_delivery.challenge.domain_state_version == 8
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=first_delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == "blocked"
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=second_delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id="sandbox-checkpoint-002",
    ).status == "blocked"

    restarted_store = SandboxInMemoryReviewStore(
        snapshot=store.snapshot().model_dump(mode="python")
    )
    restarted = SandboxReviewCoordinator(
        store=restarted_store,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=_FakeSignatureVerifier(),
    )
    assert restarted.eligibility(
        namespace=_NAMESPACE,
        test_session_id=first_delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == "blocked"
    assert restarted.eligibility(
        namespace=_NAMESPACE,
        test_session_id=second_delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id="sandbox-checkpoint-002",
    ).status == "blocked"


def test_l5_3_missing_checkpoint_or_challenge_is_rejected_without_reconstruction() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)
    snapshot = store.snapshot().model_dump(mode="python")

    without_challenge = dict(snapshot)
    without_challenge["challenges"] = ()
    with pytest.raises(SandboxReviewError) as missing_challenge:
        SandboxInMemoryReviewStore(snapshot=without_challenge)
    assert missing_challenge.value.__cause__ is None
    assert missing_challenge.value.__context__ is None

    staged = coordinator.stage_verified_resume_attempt(submission)
    assert staged.resume_attempt_ref is not None
    without_checkpoint = store.snapshot().model_dump(mode="python")
    without_checkpoint["checkpoints"] = ()
    with pytest.raises(SandboxReviewError) as missing_checkpoint:
        SandboxInMemoryReviewStore(snapshot=without_checkpoint)
    assert missing_checkpoint.value.__cause__ is None
    assert missing_checkpoint.value.__context__ is None


@pytest.mark.parametrize(
    "action",
    (None, SandboxReviewAction.REJECT, SandboxReviewAction.MODIFY_FIXTURE),
)
def test_l5_3_no_current_confirm_review_blocks_completion_and_export_is_absent(
    action: SandboxReviewAction | None,
) -> None:
    coordinator, *_ = _coordinator()
    delivery = _issue(coordinator)
    if action is not None:
        _stage_and_resume(coordinator, delivery, action)
    eligibility = coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    )
    assert eligibility.status == "blocked"
    assert not hasattr(coordinator, "complete")
    assert not hasattr(coordinator, "record")
    assert not hasattr(coordinator, "export")


def test_l5_3_modify_fixture_only_appends_review_and_remains_blocked() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    _, resumed = _stage_and_resume(
        coordinator,
        delivery,
        SandboxReviewAction.MODIFY_FIXTURE,
    )
    snapshot = store.snapshot()
    assert resumed.status == "applied"
    assert len(snapshot.events) == 1
    assert snapshot.events[0].action is SandboxReviewAction.MODIFY_FIXTURE
    assert snapshot.sources[0].source.safety_subject.formula_revision == 3
    assert snapshot.checkpoints[0].state == "review_applied"
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == "blocked"


def test_l5_3_resume_payload_64kib_plus_one_is_rejected_before_signer_and_store() -> None:
    coordinator, store, _, _, verifier = _coordinator()
    delivery = _issue(coordinator)
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)
    body = submission.model_dump(mode="json")
    body["proof"]["sandbox_test_signature"] = ""
    empty = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    padding = "x" * (MAX_RESUME_SUBMISSION_BYTES + 1 - len(empty.encode()))
    body["proof"]["sandbox_test_signature"] = padding
    oversized = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert len(oversized) == MAX_RESUME_SUBMISSION_BYTES + 1
    store_calls = store.operation_count

    rejected = coordinator.stage_verified_resume_attempt(oversized)

    assert rejected.status == "resume_rejected"
    assert rejected.resume_attempt_ref is None
    assert verifier.calls == 0
    assert store.operation_count == store_calls


def test_l5_3_caller_and_store_nested_changes_cannot_change_authority() -> None:
    source = _accepted_source()
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator, source)
    original_graph = delivery.challenge.graph_version
    object.__setattr__(source.safety_subject, "graph_version", "caller-mutated")
    snapshot = store.snapshot()
    object.__setattr__(
        snapshot.sources[0].source.safety_subject,
        "graph_version",
        "snapshot-mutated",
    )

    fresh = store.snapshot()
    assert fresh.sources[0].source.safety_subject.graph_version == original_graph
    staged = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.status == "staged"


def test_l5_3_events_are_immutable_append_only_and_secret_free() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)
    staged, resumed = _stage_and_resume(
        coordinator,
        delivery,
        SandboxReviewAction.CONFIRM,
    )
    assert resumed.status == "applied"
    before = store.snapshot().events
    caller_copy = store.snapshot().events
    with pytest.raises(ValidationError):
        caller_copy[0].action = SandboxReviewAction.REJECT  # type: ignore[misc]
    object.__setattr__(caller_copy[0], "action", SandboxReviewAction.REJECT)
    assert store.snapshot().events[0].action is SandboxReviewAction.CONFIRM
    assert store.snapshot().events[: len(before)] == before
    event_bytes = canonical_review_bytes(store.snapshot().events)
    event_repr = repr(store.snapshot().events)
    for event_secret in (
        _NONCE.hex(),
        submission.proof.sandbox_test_signature,
        "fixed sandbox explanation for review rule 001",
        "fixed-fictitious-component",
    ):
        assert event_secret.encode() not in event_bytes
        assert event_secret not in event_repr
    persisted = canonical_review_bytes(store.snapshot())
    persisted_repr = repr(store.snapshot())
    for store_secret in (
        _NONCE.hex(),
        submission.proof.sandbox_test_signature,
    ):
        assert store_secret.encode() not in persisted
        assert store_secret not in persisted_repr
    assert staged.resume_attempt_ref is not None


def test_l5_3_errors_are_fixed_chainless_and_payload_free() -> None:
    secret = "should-not-escape-secret-payload"
    verifier = _FakeSignatureVerifier(raises=secret)
    coordinator, store, *_ = _coordinator(verifier=verifier)
    delivery = _issue(coordinator)
    before = store.snapshot()
    result = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    assert result.status == "resume_rejected"
    assert secret not in repr(result)
    assert store.snapshot().checkpoints == before.checkpoints
    assert store.snapshot().events == before.events

    bad_schema = coordinator.stage_verified_resume_attempt(
        b'{"plaintext_nonce":"should-not-escape-bad-json"'
    )
    missing = coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref="missing-attempt-ref")
    )
    assert bad_schema.status == "resume_rejected"
    assert missing.status == "resume_rejected"
    assert "should-not-escape" not in repr(bad_schema)
    assert "missing-attempt-ref" not in repr(missing)


def test_l5_3_injected_review_error_is_normalized_without_cause_or_context() -> None:
    secret = "nested-nonce-secret"
    nonce_factory = _NestedReviewErrorNonceFactory(secret)
    nonce_coordinator, nonce_store, *_ = _coordinator(
        nonce_factory=nonce_factory  # type: ignore[arg-type]
    )
    store_dependency = _NestedReviewErrorStore()
    store_coordinator, _, _, store_nonce_factory, _ = _coordinator(
        store=store_dependency
    )
    captured: list[SandboxReviewError] = []

    for coordinator in (nonce_coordinator, store_coordinator):
        try:
            _issue(coordinator)
        except SandboxReviewError as error:
            captured.append(error)

    assert len(captured) == 2
    for error in captured:
        assert str(error) == "SANDBOX_REVIEW_REJECTED"
        assert error.__cause__ is None
        assert error.__context__ is None
        assert secret not in repr(error)
        assert "nested-store-secret" not in repr(error)
    assert nonce_factory.calls == 1
    assert store_nonce_factory.calls == 0
    assert nonce_store.operation_count == 0
    assert store_dependency.operation_count == 0


def test_l5_3_no_settings_env_network_runtime_db_gateway_legacy_or_export_imports() -> None:
    source_path = Path("app/agent_runtime/sandbox_review.py")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden_import_fragments = {
        "config",
        "settings",
        "langgraph",
        "fastapi",
        "sqlalchemy",
        "redis",
        "milvus",
        "gateway",
        "runtime_factory",
        "http",
        "socket",
        "requests",
        "urllib",
    }
    assert not any(
        fragment in module.lower()
        for module in imported
        for fragment in forbidden_import_fragments
    )
    allowed_agent_runtime_imports = {
        "app.agent_runtime.sandbox_safety",
        "app.agent_runtime.sandbox_explanation",
    }
    assert {
        module for module in imported if module.startswith("app.agent_runtime")
    } <= allowed_agent_runtime_imports
    assert "os.environ" not in source
    assert "getenv(" not in source
    assert "time.time(" not in source
    assert "sleep(" not in source
    assert "data/" not in source
    for name in ("complete", "record", "export"):
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
            for node in ast.walk(tree)
        )
