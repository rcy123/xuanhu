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


class _FakeSigner:
    @staticmethod
    def signature(payload_digest: str) -> str:
        return hashlib.sha256(f"sandbox-signature:{payload_digest}".encode()).hexdigest()


class _FakeSignatureVerifier:
    def __init__(
        self, *, raises: str | None = None, result: bool | None = None
    ) -> None:
        self.calls = 0
        self.raises = raises
        self.result = result

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
        if self.result is not None:
            return self.result
        expected_signature = hashlib.sha256(
            f"sandbox-signature:{signed_payload_digest}".encode()
        ).hexdigest()
        return (
            sandbox_test_signature_scheme == _SCHEME
            and sandbox_test_key_id == _KEY_ID
            and sandbox_test_signature == expected_signature
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


def _restore_store(snapshot: object) -> SandboxInMemoryReviewStore:
    return SandboxInMemoryReviewStore(
        snapshot=snapshot,
        signature_verifier=_FakeSignatureVerifier(),
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
        sandbox_test_signature=_FakeSigner.signature(payload_digest),
    )
    return SandboxResumeSubmissionV1(
        namespace=_NAMESPACE,
        test_session_id=delivery.challenge.test_session_id,
        challenge=delivery.challenge,
        action=action,
        plaintext_nonce=delivery.plaintext_nonce,
        proof=proof,
    )


def _derived_record_ref(
    prefix: str,
    record: dict[str, object],
    *,
    ref_field: str,
) -> str:
    body = {key: value for key, value in record.items() if key != ref_field}
    return prefix + hashlib.sha256(canonical_review_bytes(body)).hexdigest()


def _rederive_review_snapshot_schema(
    snapshot: dict[str, object], *, schema_version: str
) -> str:
    challenges = snapshot["challenges"]
    assert isinstance(challenges, tuple)
    assert len(challenges) == 1
    challenge = challenges[0]
    assert isinstance(challenge, dict)
    old_challenge_ref = challenge["challenge_ref"]
    assert isinstance(old_challenge_ref, str)
    challenge["sandbox_schema_version"] = schema_version
    challenge_body = {
        key: value
        for key, value in challenge.items()
        if key not in {"challenge_ref", "state"}
    }
    new_challenge_ref = (
        "sandbox-challenge-"
        + hashlib.sha256(canonical_review_bytes(challenge_body)).hexdigest()
    )
    challenge["challenge_ref"] = new_challenge_ref

    attempts = snapshot["attempts"]
    assert isinstance(attempts, tuple)
    attempt_refs: dict[str, str] = {}
    for attempt in attempts:
        assert isinstance(attempt, dict)
        if attempt["challenge_ref"] != old_challenge_ref:
            continue
        old_attempt_ref = attempt["resume_attempt_ref"]
        assert isinstance(old_attempt_ref, str)
        attempt["challenge_ref"] = new_challenge_ref
        attempt_body = {
            key: value
            for key, value in attempt.items()
            if key not in {"resume_attempt_ref", "state"}
        }
        new_attempt_ref = (
            "sandbox-attempt-"
            + hashlib.sha256(canonical_review_bytes(attempt_body)).hexdigest()
        )
        attempt["resume_attempt_ref"] = new_attempt_ref
        attempt_refs[old_attempt_ref] = new_attempt_ref

    events = snapshot["events"]
    assert isinstance(events, tuple)
    for event in events:
        assert isinstance(event, dict)
        if event["challenge_ref"] != old_challenge_ref:
            continue
        event["challenge_ref"] = new_challenge_ref
        event["sandbox_schema_version"] = schema_version
        old_attempt_ref = event["resume_attempt_ref"]
        assert isinstance(old_attempt_ref, str)
        event["resume_attempt_ref"] = attempt_refs[old_attempt_ref]
        event["event_ref"] = _derived_record_ref(
            "sandbox-review-event-",
            event,
            ref_field="event_ref",
        )

    transitions = snapshot["transitions"]
    assert isinstance(transitions, tuple)
    for transition in transitions:
        assert isinstance(transition, dict)
        if transition["challenge_ref"] != old_challenge_ref:
            continue
        transition["challenge_ref"] = new_challenge_ref
        old_attempt_ref = transition["resume_attempt_ref"]
        if old_attempt_ref is not None:
            assert isinstance(old_attempt_ref, str)
            transition["resume_attempt_ref"] = attempt_refs[old_attempt_ref]
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-",
            transition,
            ref_field="transition_ref",
        )

    checkpoints = snapshot["checkpoints"]
    assert isinstance(checkpoints, tuple)
    for checkpoint in checkpoints:
        assert isinstance(checkpoint, dict)
        if checkpoint["challenge_ref"] == old_challenge_ref:
            checkpoint["challenge_ref"] = new_challenge_ref
    markers = snapshot["current_authorities"]
    assert isinstance(markers, tuple)
    for marker in markers:
        assert isinstance(marker, dict)
        if marker["challenge_ref"] == old_challenge_ref:
            marker["challenge_ref"] = new_challenge_ref

    return new_challenge_ref


def _rederive_review_snapshot_proof_identifier(
    snapshot: dict[str, object], *, field: str, value: str
) -> None:
    assert field in {
        "sandbox_test_reviewer_id",
        "sandbox_test_signature_scheme",
        "sandbox_test_key_id",
    }
    attempts = snapshot["attempts"]
    assert isinstance(attempts, tuple)
    attempt_refs: dict[str, str] = {}
    for attempt in attempts:
        assert isinstance(attempt, dict)
        old_attempt_ref = attempt["resume_attempt_ref"]
        assert isinstance(old_attempt_ref, str)
        attempt[field] = value
        new_attempt_ref = _derived_record_ref(
            "sandbox-attempt-",
            {
                key: item
                for key, item in attempt.items()
                if key != "state"
            },
            ref_field="resume_attempt_ref",
        )
        attempt["resume_attempt_ref"] = new_attempt_ref
        attempt_refs[old_attempt_ref] = new_attempt_ref

    events = snapshot["events"]
    assert isinstance(events, tuple)
    for event in events:
        assert isinstance(event, dict)
        old_attempt_ref = event["resume_attempt_ref"]
        assert isinstance(old_attempt_ref, str)
        if old_attempt_ref not in attempt_refs:
            continue
        event[field] = value
        event["resume_attempt_ref"] = attempt_refs[old_attempt_ref]
        event["event_ref"] = _derived_record_ref(
            "sandbox-review-event-",
            event,
            ref_field="event_ref",
        )

    transitions = snapshot["transitions"]
    assert isinstance(transitions, tuple)
    for transition in transitions:
        assert isinstance(transition, dict)
        old_attempt_ref = transition["resume_attempt_ref"]
        if old_attempt_ref not in attempt_refs:
            continue
        assert isinstance(old_attempt_ref, str)
        transition["resume_attempt_ref"] = attempt_refs[old_attempt_ref]
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-",
            transition,
            ref_field="transition_ref",
        )


def _rederive_review_snapshot_action(
    snapshot: dict[str, object],
    *,
    challenge,
    action: SandboxReviewAction,
) -> None:
    attempts = snapshot["attempts"]
    assert isinstance(attempts, tuple)
    exact_attempts = [
        attempt
        for attempt in attempts
        if attempt["challenge_ref"] == challenge.challenge_ref
    ]
    assert len(exact_attempts) == 1
    attempt = exact_attempts[0]
    assert isinstance(attempt, dict)
    old_attempt_ref = attempt["resume_attempt_ref"]
    assert isinstance(old_attempt_ref, str)
    persisted_signature = attempt.get("sandbox_test_signature")
    changed_digest = review_signed_payload_digest(
        challenge=challenge,
        action=action,
        plaintext_nonce=_NONCE,
        sandbox_test_reviewer_id=attempt["sandbox_test_reviewer_id"],
        sandbox_test_role=attempt["sandbox_test_role"],
        sandbox_test_organization_label=attempt[
            "sandbox_test_organization_label"
        ],
        sandbox_test_qualification_label=attempt[
            "sandbox_test_qualification_label"
        ],
        sandbox_test_signature_scheme=attempt["sandbox_test_signature_scheme"],
        sandbox_test_key_id=attempt["sandbox_test_key_id"],
    )
    attempt["action"] = action
    attempt["sandbox_test_signed_payload_digest"] = changed_digest
    new_attempt_ref = _derived_record_ref(
        "sandbox-attempt-",
        {
            key: value
            for key, value in attempt.items()
            if key != "state"
        },
        ref_field="resume_attempt_ref",
    )
    attempt["resume_attempt_ref"] = new_attempt_ref

    events = snapshot["events"]
    assert isinstance(events, tuple)
    exact_events = [
        event
        for event in events
        if event["resume_attempt_ref"] == old_attempt_ref
    ]
    assert len(exact_events) == 1
    event = exact_events[0]
    assert isinstance(event, dict)
    event["action"] = action
    event["sandbox_test_signed_payload_digest"] = changed_digest
    event["resume_attempt_ref"] = new_attempt_ref
    event["event_ref"] = _derived_record_ref(
        "sandbox-review-event-",
        event,
        ref_field="event_ref",
    )

    transitions = snapshot["transitions"]
    assert isinstance(transitions, tuple)
    rewritten_transitions = 0
    for transition in transitions:
        assert isinstance(transition, dict)
        if transition["resume_attempt_ref"] != old_attempt_ref:
            continue
        transition["resume_attempt_ref"] = new_attempt_ref
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-",
            transition,
            ref_field="transition_ref",
        )
        rewritten_transitions += 1
    assert rewritten_transitions == 4
    assert attempt["sandbox_test_signed_payload_digest"] == changed_digest
    assert event["sandbox_test_signed_payload_digest"] == changed_digest
    assert attempt.get("sandbox_test_signature") == persisted_signature


def _rederive_review_snapshot_signature(
    snapshot: dict[str, object], *, changed_signature: str
) -> None:
    attempts = snapshot["attempts"]
    assert isinstance(attempts, tuple)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert isinstance(attempt, dict)
    old_attempt_ref = attempt["resume_attempt_ref"]
    assert isinstance(old_attempt_ref, str)
    assert "sandbox_test_signature" in attempt
    attempt["sandbox_test_signature"] = changed_signature
    new_attempt_ref = _derived_record_ref(
        "sandbox-attempt-",
        {key: value for key, value in attempt.items() if key != "state"},
        ref_field="resume_attempt_ref",
    )
    attempt["resume_attempt_ref"] = new_attempt_ref

    events = snapshot["events"]
    assert isinstance(events, tuple)
    for event in events:
        assert isinstance(event, dict)
        if event["resume_attempt_ref"] != old_attempt_ref:
            continue
        event["resume_attempt_ref"] = new_attempt_ref
        event["event_ref"] = _derived_record_ref(
            "sandbox-review-event-", event, ref_field="event_ref"
        )

    transitions = snapshot["transitions"]
    assert isinstance(transitions, tuple)
    for transition in transitions:
        assert isinstance(transition, dict)
        if transition["resume_attempt_ref"] != old_attempt_ref:
            continue
        transition["resume_attempt_ref"] = new_attempt_ref
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-", transition, ref_field="transition_ref"
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
    "snapshot_state",
    ("issued", "applied"),
)
def test_l5_3_restore_rejects_coordinated_nonfixed_review_schema(
    snapshot_state: str,
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    if snapshot_state == "applied":
        _, resumed = _stage_and_resume(
            coordinator,
            delivery,
            SandboxReviewAction.MODIFY_FIXTURE,
        )
        assert resumed.status == "applied"
    baseline = store.snapshot()
    assert baseline.challenges[0].state == snapshot_state
    assert _restore_store(baseline).snapshot() == baseline

    changed = baseline.model_dump(mode="python")
    old_challenge_ref = changed["challenges"][0]["challenge_ref"]
    new_challenge_ref = _rederive_review_snapshot_schema(
        changed,
        schema_version="sandbox-review-challenge.v2",
    )
    assert new_challenge_ref != old_challenge_ref
    assert old_challenge_ref.encode() not in canonical_review_bytes(changed)
    before = canonical_review_bytes(changed)

    with pytest.raises(SandboxReviewError) as raised:
        _restore_store(changed)

    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canonical_review_bytes(changed) == before


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    tuple(
        (field, invalid_value)
        for field in (
            "sandbox_test_reviewer_id",
            "sandbox_test_signature_scheme",
            "sandbox_test_key_id",
        )
        for invalid_value in ("", "a" * 129, "invalid value")
    ),
    ids=(
        "reviewer-empty",
        "reviewer-too-long",
        "reviewer-pattern",
        "scheme-empty",
        "scheme-too-long",
        "scheme-pattern",
        "key-empty",
        "key-too-long",
        "key-pattern",
    ),
)
def test_l5_3_restore_rejects_coordinated_invalid_proof_identifier(
    field: str, invalid_value: str
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    _, resumed = _stage_and_resume(
        coordinator,
        delivery,
        SandboxReviewAction.MODIFY_FIXTURE,
    )
    assert resumed.status == "applied"
    baseline = store.snapshot()
    assert len(baseline.attempts) == len(baseline.events) == 1
    assert _restore_store(baseline).snapshot() == baseline

    changed = baseline.model_dump(mode="python")
    _rederive_review_snapshot_proof_identifier(
        changed,
        field=field,
        value=invalid_value,
    )
    before = canonical_review_bytes(changed)

    with pytest.raises(SandboxReviewError) as raised:
        _restore_store(changed)

    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canonical_review_bytes(changed) == before


def test_l5_3_proof_identifier_constraints_share_one_named_alias_and_roundtrip() -> None:
    source = Path("app/agent_runtime/sandbox_review.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    alias = next(
        statement
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_ReviewTestIdentifier"
            for target in statement.targets
        )
    )
    assert ast.unparse(alias.value) == (
        "Annotated[str, Field(min_length=1, max_length=128, "
        "pattern=_IDENTIFIER_PATTERN)]"
    )
    constrained_fields = {
        "sandbox_test_reviewer_id",
        "sandbox_test_signature_scheme",
        "sandbox_test_key_id",
    }
    model_names = {
        "SandboxTestReviewProofV1",
        "_SealedAttemptV1",
        "SandboxTestReviewEventV1",
    }
    annotations = tuple(
        statement.annotation
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef) and class_node.name in model_names
        for statement in class_node.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id in constrained_fields
    )
    assert len(annotations) == 9
    assert all(
        isinstance(annotation, ast.Name)
        and annotation.id == "_ReviewTestIdentifier"
        for annotation in annotations
    )

    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)
    proof_data = submission.proof.model_dump(mode="python")
    for field in constrained_fields:
        for valid_value in ("a", "a" * 128):
            candidate = dict(proof_data)
            candidate[field] = valid_value
            assert (
                SandboxTestReviewProofV1.model_validate(candidate, strict=True)
                .model_dump(mode="python")[field]
                == valid_value
            )

    staged, resumed = _stage_and_resume(
        coordinator,
        delivery,
        SandboxReviewAction.CONFIRM,
    )
    assert staged.resume_attempt_ref is not None
    assert resumed.status == "applied"
    snapshot = store.snapshot()
    assert _restore_store(snapshot).snapshot() == snapshot


def test_l5_3_signed_digest_live_and_restore_share_one_authority_helper() -> None:
    tree = ast.parse(
        Path("app/agent_runtime/sandbox_review.py").read_text(encoding="utf-8")
    )
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    helper_name = "_persisted_review_signed_authority_digest"
    assert helper_name in functions
    helper_calls = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == helper_name
    )
    assert len(helper_calls) == 2
    assert any(call in tuple(ast.walk(functions["review_signed_payload_digest"])) for call in helper_calls)
    assert any(call in tuple(ast.walk(functions["_snapshot_is_integral"])) for call in helper_calls)


def test_l5_3_signed_authority_helper_accepts_only_persisted_nonce_digest() -> None:
    tree = ast.parse(
        Path("app/agent_runtime/sandbox_review.py").read_text(encoding="utf-8")
    )
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    helper = functions["_persisted_review_signed_authority_digest"]
    helper_source = ast.unparse(helper)
    assert "nonce_digest" in helper_source
    assert "plaintext_nonce" not in helper_source
    assert "sandbox_test_signature=" not in helper_source

    live_wrapper = functions["review_signed_payload_digest"]
    live_calls = tuple(
        node
        for node in ast.walk(live_wrapper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_persisted_review_signed_authority_digest"
    )
    assert len(live_calls) == 1
    live_nonce = next(
        keyword.value
        for keyword in live_calls[0].keywords
        if keyword.arg == "nonce_digest"
    )
    assert ast.unparse(live_nonce) == "_bytes_sha256(plaintext_nonce)"

    restore = functions["_snapshot_is_integral"]
    restore_calls = tuple(
        node
        for node in ast.walk(restore)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_persisted_review_signed_authority_digest"
    )
    assert len(restore_calls) == 1
    restore_nonce = next(
        keyword.value
        for keyword in restore_calls[0].keywords
        if keyword.arg == "nonce_digest"
    )
    assert ast.unparse(restore_nonce) == "attempt_challenge.nonce_digest"


def test_l5_3_fixed_schema_guard_is_shared_before_state_branches_and_v1_roundtrips() -> None:
    source = Path("app/agent_runtime/sandbox_review.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    integrity = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_snapshot_is_integral"
    )
    challenge_loop = next(
        node
        for node in integrity.body
        if isinstance(node, ast.For)
        and ast.unparse(node.target) == "challenge"
        and ast.unparse(node.iter) == "challenges"
    )
    fixed_guard_indexes = tuple(
        index
        for index, statement in enumerate(challenge_loop.body)
        if isinstance(statement, ast.If)
        and ast.unparse(statement.test)
        == "challenge.sandbox_schema_version != _REVIEW_SCHEMA_VERSION"
    )
    state_branch_indexes = tuple(
        index
        for index, statement in enumerate(challenge_loop.body)
        if any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "challenge"
            and node.attr == "state"
            for node in ast.walk(statement)
        )
    )
    assert len(fixed_guard_indexes) == 1
    assert state_branch_indexes
    assert fixed_guard_indexes[0] < min(state_branch_indexes)

    issue_method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "create_single_use_challenge"
    )
    live_schema_values = tuple(
        keyword.value
        for call in ast.walk(issue_method)
        if isinstance(call, ast.Call)
        and ast.unparse(call.func)
        in {"SandboxReviewChallengeV1", "SandboxReviewChallengeV1.model_construct"}
        for keyword in call.keywords
        if keyword.arg == "sandbox_schema_version"
    )
    assert len(live_schema_values) == 2
    assert all(
        isinstance(value, ast.Name) and value.id == "_REVIEW_SCHEMA_VERSION"
        for value in live_schema_values
    )

    for expected_state in ("issued", "expired", "applied"):
        coordinator, store, clock, *_ = _coordinator()
        delivery = _issue(coordinator)
        if expected_state == "expired":
            clock.value = delivery.challenge.expires_at
            assert (
                coordinator.stage_verified_resume_attempt(
                    _submission(delivery, SandboxReviewAction.CONFIRM)
                ).status
                == "resume_rejected"
            )
        elif expected_state == "applied":
            _, resumed = _stage_and_resume(
                coordinator,
                delivery,
                SandboxReviewAction.MODIFY_FIXTURE,
            )
            assert resumed.status == "applied"
        snapshot = store.snapshot()
        assert snapshot.challenges[0].state == expected_state
        assert _restore_store(snapshot).snapshot() == snapshot


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
    restarted_store = _restore_store(snapshot)
    restarted = SandboxReviewCoordinator(
        store=restarted_store,
        clock=_FakeClock(),
        nonce_factory=_FakeNonceFactory(),
        signature_verifier=_FakeSignatureVerifier(),
    )
    assert restarted_store.snapshot() == snapshot
    assert restarted.eligibility(
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
        _restore_store(snapshot)
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("original_action", "changed_action", "original_status", "changed_status"),
    (
        (
            SandboxReviewAction.REJECT,
            SandboxReviewAction.CONFIRM,
            "blocked",
            "eligible",
        ),
        (
            SandboxReviewAction.CONFIRM,
            SandboxReviewAction.REJECT,
            "eligible",
            "blocked",
        ),
    ),
    ids=("reject-to-confirm", "confirm-to-reject"),
)
def test_l5_3_restart_snapshot_rejects_coordinated_attempt_and_event_action_change(
    original_action: SandboxReviewAction,
    changed_action: SandboxReviewAction,
    original_status: str,
    changed_status: str,
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    submission = _submission(delivery, original_action)
    staged = coordinator.stage_verified_resume_attempt(submission)
    assert staged.resume_attempt_ref is not None
    assert coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    ).status == "applied"
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == original_status
    snapshot = store.snapshot().model_dump(mode="python")
    original_digest = snapshot["attempts"][0]["sandbox_test_signed_payload_digest"]
    original_signature = submission.proof.sandbox_test_signature
    _rederive_review_snapshot_action(
        snapshot,
        challenge=delivery.challenge,
        action=changed_action,
    )
    assert snapshot["attempts"][0]["sandbox_test_signed_payload_digest"] != original_digest
    assert snapshot["events"][0]["sandbox_test_signed_payload_digest"] != original_digest
    assert snapshot["attempts"][0]["sandbox_test_signature"] == original_signature
    assert original_signature == submission.proof.sandbox_test_signature
    before = canonical_review_bytes(snapshot)

    with pytest.raises(SandboxReviewError) as raised:
        restored_store = SandboxInMemoryReviewStore(
            snapshot=snapshot,
            signature_verifier=_FakeSignatureVerifier(),
        )
        restored = SandboxReviewCoordinator(
            store=restored_store,
            clock=_FakeClock(),
            nonce_factory=_FakeNonceFactory(),
            signature_verifier=_FakeSignatureVerifier(),
        )
        assert restored.eligibility(
            namespace=_NAMESPACE,
            test_session_id=delivery.challenge.test_session_id,
            thread_id=_THREAD_ID,
            checkpoint_id=_CHECKPOINT_ID,
        ).status == changed_status
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canonical_review_bytes(snapshot) == before


def test_l5_3_restore_requires_verifier_for_nonempty_attempt_snapshot() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    staged = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.status == "staged"
    baseline = store.snapshot()
    assert len(baseline.attempts) == 1
    before = canonical_review_bytes(baseline)

    with pytest.raises(SandboxReviewError) as raised:
        SandboxInMemoryReviewStore(snapshot=baseline)

    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canonical_review_bytes(baseline) == before


@pytest.mark.parametrize(
    ("result", "raises"),
    ((False, None), (None, "restore-verifier-secret")),
    ids=("false", "exception"),
)
def test_l5_3_restore_rejects_failed_signature_verifier_without_mutation(
    result: bool | None, raises: str | None
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    staged = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.status == "staged"
    baseline = store.snapshot()
    before = canonical_review_bytes(baseline)
    verifier = _FakeSignatureVerifier(result=result, raises=raises)

    with pytest.raises(SandboxReviewError) as raised:
        SandboxInMemoryReviewStore(
            snapshot=baseline,
            signature_verifier=verifier,
        )

    assert verifier.calls == 1
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "restore-verifier-secret" not in repr(raised.value)
    assert canonical_review_bytes(baseline) == before


def test_l5_3_restore_rejects_original_signature_proof_drift() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    _, resumed = _stage_and_resume(
        coordinator, delivery, SandboxReviewAction.CONFIRM
    )
    assert resumed.status == "applied"
    changed = store.snapshot().model_dump(mode="python")
    original_signature = changed["attempts"][0]["sandbox_test_signature"]
    _rederive_review_snapshot_signature(
        changed,
        changed_signature="0" * len(original_signature),
    )
    before = canonical_review_bytes(changed)
    verifier = _FakeSignatureVerifier()

    with pytest.raises(SandboxReviewError) as raised:
        SandboxInMemoryReviewStore(
            snapshot=changed,
            signature_verifier=verifier,
        )

    assert changed["attempts"][0]["sandbox_test_signature"] != original_signature
    assert verifier.calls == 1
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canonical_review_bytes(changed) == before


@pytest.mark.parametrize(
    ("attempt_state", "action", "expected_eligibility"),
    (
        ("sealed", SandboxReviewAction.CONFIRM, "blocked"),
        ("applied", SandboxReviewAction.CONFIRM, "eligible"),
        ("applied", SandboxReviewAction.REJECT, "blocked"),
    ),
    ids=("sealed-confirm", "applied-confirm", "applied-reject"),
)
def test_l5_3_signature_verified_restart_accepts_sealed_and_applied_attempts(
    attempt_state: str,
    action: SandboxReviewAction,
    expected_eligibility: str,
) -> None:
    coordinator, store, clock, nonce_factory, _ = _coordinator()
    delivery = _issue(coordinator)
    staged = coordinator.stage_verified_resume_attempt(_submission(delivery, action))
    assert staged.resume_attempt_ref is not None
    if attempt_state == "applied":
        assert coordinator.resume(
            SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
        ).status == "applied"
    snapshot = store.snapshot()
    signature = snapshot.attempts[0].sandbox_test_signature
    verifier = _FakeSignatureVerifier()

    restarted_store = SandboxInMemoryReviewStore(
        snapshot=snapshot,
        signature_verifier=verifier,
    )
    restarted = SandboxReviewCoordinator(
        store=restarted_store,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=verifier,
    )

    assert verifier.calls == 1
    assert restarted_store.snapshot() == snapshot
    assert signature.encode() in canonical_review_bytes(snapshot)
    assert signature not in repr(snapshot)
    assert all(
        "sandbox_test_signature" not in type(event).model_fields
        for event in snapshot.events
    )
    assert restarted.eligibility(
        namespace=_NAMESPACE,
        test_session_id=delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == expected_eligibility
    if attempt_state == "sealed":
        assert restarted.resume(
            SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
        ).status == "applied"


def test_l5_3_empty_store_restore_does_not_require_signature_verifier() -> None:
    empty = SandboxInMemoryReviewStore().snapshot()
    assert empty.attempts == ()
    assert SandboxInMemoryReviewStore(snapshot=empty).snapshot() == empty


def test_l5_3_signature_proof_is_bounded_hidden_and_restore_verified() -> None:
    tree = ast.parse(
        Path("app/agent_runtime/sandbox_review.py").read_text(encoding="utf-8")
    )
    classes = {
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
    }
    signature_fields = []
    for model_name in ("SandboxTestReviewProofV1", "_SealedAttemptV1"):
        field = next(
            statement
            for statement in classes[model_name].body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "sandbox_test_signature"
        )
        assert isinstance(field.value, ast.Call)
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in field.value.keywords}
        assert keywords == {"min_length": "1", "max_length": "512", "repr": "False"}
        signature_fields.append(field)
    assert len(signature_fields) == 2
    assert not any(
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == "sandbox_test_signature"
        for statement in classes["SandboxTestReviewEventV1"].body
    )
    assert not hasattr(_FakeSignatureVerifier, "signature")

    store_init = next(
        node
        for node in classes["SandboxInMemoryReviewStore"].body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert "signature_verifier" in {
        argument.arg for argument in (*store_init.args.args, *store_init.args.kwonlyargs)
    }
    assert any(
        isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".verify")
        for node in ast.walk(classes["SandboxInMemoryReviewStore"])
    )

    coordinator, *_ = _coordinator()
    proof = _submission(_issue(coordinator), SandboxReviewAction.CONFIRM).proof
    body = proof.model_dump(mode="python")
    body["sandbox_test_signature"] = "x" * 512
    assert len(
        SandboxTestReviewProofV1.model_validate(body, strict=True).sandbox_test_signature
    ) == 512
    body["sandbox_test_signature"] = "x" * 513
    with pytest.raises(ValidationError):
        SandboxTestReviewProofV1.model_validate(body, strict=True)


def test_l5_3_restart_snapshot_rejects_two_applied_attempts_for_one_challenge() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    sealed = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    applied = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.REJECT)
    )
    assert sealed.resume_attempt_ref is not None
    assert applied.resume_attempt_ref is not None
    assert coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref=applied.resume_attempt_ref)
    ).status == "applied"
    snapshot = store.snapshot().model_dump(mode="python")
    sealed_attempt = next(
        attempt
        for attempt in snapshot["attempts"]
        if attempt["resume_attempt_ref"] == sealed.resume_attempt_ref
    )
    sealed_attempt["state"] = "applied"
    existing_event = snapshot["events"][0]
    second_event = dict(existing_event)
    second_event.update(
        {
            "sequence": len(snapshot["events"]),
            "resume_attempt_ref": sealed_attempt["resume_attempt_ref"],
            "action": sealed_attempt["action"],
            "sandbox_test_reviewer_id": sealed_attempt[
                "sandbox_test_reviewer_id"
            ],
            "sandbox_test_role": sealed_attempt["sandbox_test_role"],
            "sandbox_test_organization_label": sealed_attempt[
                "sandbox_test_organization_label"
            ],
            "sandbox_test_qualification_label": sealed_attempt[
                "sandbox_test_qualification_label"
            ],
            "sandbox_test_signature_scheme": sealed_attempt[
                "sandbox_test_signature_scheme"
            ],
            "sandbox_test_key_id": sealed_attempt["sandbox_test_key_id"],
            "sandbox_test_signed_payload_digest": sealed_attempt[
                "sandbox_test_signed_payload_digest"
            ],
        }
    )
    second_event["event_ref"] = _derived_record_ref(
        "sandbox-review-event-",
        second_event,
        ref_field="event_ref",
    )
    snapshot["events"] = snapshot["events"] + (second_event,)
    for from_state, to_state in (
        ("issued", "claimed"),
        ("claimed", "applied"),
        ("review_pending", "review_applied"),
    ):
        transition: dict[str, object] = {
            "transition_ref": "pending",
            "sequence": len(snapshot["transitions"]),
            "challenge_ref": delivery.challenge.challenge_ref,
            "resume_attempt_ref": sealed_attempt["resume_attempt_ref"],
            "from_state": from_state,
            "to_state": to_state,
            "observed_at": second_event["applied_at"],
        }
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-",
            transition,
            ref_field="transition_ref",
        )
        snapshot["transitions"] = snapshot["transitions"] + (transition,)

    with pytest.raises(SandboxReviewError) as raised:
        _restore_store(snapshot)
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

    restarted_store = _restore_store(store.snapshot().model_dump(mode="python"))
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


def test_l5_3_reused_checkpoint_id_resolves_current_interrupt_eligibility() -> None:
    coordinator, store, clock, nonce_factory, _ = _coordinator()
    first_delivery = _issue(coordinator)
    _stage_and_resume(coordinator, first_delivery, SandboxReviewAction.CONFIRM)
    second_source = _accepted_source(domain_state_version=8)
    second_delivery = coordinator.create_single_use_challenge(
        second_source,
        namespace=_NAMESPACE,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
        interrupt_id="sandbox-interrupt-002",
    )
    _stage_and_resume(coordinator, second_delivery, SandboxReviewAction.CONFIRM)
    live_status = coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=second_delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status

    restarted_store = _restore_store(store.snapshot().model_dump(mode="python"))
    restarted = SandboxReviewCoordinator(
        store=restarted_store,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=_FakeSignatureVerifier(),
    )
    restart_status = restarted.eligibility(
        namespace=_NAMESPACE,
        test_session_id=second_delivery.challenge.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status
    assert (live_status, restart_status) == ("eligible", "eligible")


@pytest.mark.parametrize(
    "tamper",
    (
        "staged_before_issued",
        "staged_at_expiry",
        "applied_before_staged",
        "applied_at_expiry",
    ),
)
def test_l5_3_restart_snapshot_rejects_noncausal_stage_and_apply_times(
    tamper: str,
) -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    _stage_and_resume(coordinator, delivery, SandboxReviewAction.CONFIRM)
    snapshot = store.snapshot().model_dump(mode="python")
    issued_at = delivery.challenge.issued_at
    expires_at = delivery.challenge.expires_at
    staged_transition = next(
        transition
        for transition in snapshot["transitions"]
        if transition["to_state"] == "attempt_staged"
    )
    applied_transitions = tuple(
        transition
        for transition in snapshot["transitions"]
        if transition["to_state"] in {"claimed", "applied", "review_applied"}
    )

    if tamper == "staged_before_issued":
        staged_transition["observed_at"] = issued_at - 1
    elif tamper == "staged_at_expiry":
        staged_transition["observed_at"] = expires_at
    elif tamper == "applied_before_staged":
        staged_transition["observed_at"] = issued_at + 10
        snapshot["events"][0]["applied_at"] = issued_at + 9
        for transition in applied_transitions:
            transition["observed_at"] = issued_at + 9
    elif tamper == "applied_at_expiry":
        snapshot["events"][0]["applied_at"] = expires_at
        for transition in applied_transitions:
            transition["observed_at"] = expires_at

    snapshot["events"][0]["event_ref"] = _derived_record_ref(
        "sandbox-review-event-",
        snapshot["events"][0],
        ref_field="event_ref",
    )
    for transition in snapshot["transitions"]:
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-",
            transition,
            ref_field="transition_ref",
        )

    with pytest.raises(SandboxReviewError) as raised:
        _restore_store(snapshot)
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l5_3_live_stage_and_apply_reject_predecessor_clock_without_mutation() -> None:
    stage_clock = _FakeClock()
    stage_coordinator, stage_store, *_ = _coordinator(clock=stage_clock)
    stage_delivery = _issue(stage_coordinator)
    stage_before = stage_store.snapshot()
    stage_clock.value = stage_delivery.challenge.issued_at - 1
    stage_result = stage_coordinator.stage_verified_resume_attempt(
        _submission(stage_delivery, SandboxReviewAction.CONFIRM)
    )

    apply_clock = _FakeClock()
    apply_coordinator, apply_store, *_ = _coordinator(clock=apply_clock)
    apply_delivery = _issue(apply_coordinator)
    apply_clock.value = apply_delivery.challenge.issued_at + 10
    staged = apply_coordinator.stage_verified_resume_attempt(
        _submission(apply_delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    apply_before = apply_store.snapshot()
    apply_clock.value = apply_delivery.challenge.issued_at + 9
    apply_result = apply_coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )

    assert (stage_result.status, apply_result.status) == (
        "resume_rejected",
        "resume_rejected",
    )
    stage_after = stage_store.snapshot()
    apply_after = apply_store.snapshot()
    assert stage_after == stage_before
    assert apply_after == apply_before
    assert apply_after.attempts[0].state == "sealed"
    assert apply_after.challenges[0].state == "issued"
    assert apply_after.checkpoints[0].state == "review_pending"
    assert apply_after.events == ()
    assert _restore_store(stage_after).snapshot() == stage_after
    assert _restore_store(apply_after).snapshot() == apply_after


def test_l5_3_restart_snapshot_rejects_attempt_staged_after_challenge_applied() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    loser = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.REJECT)
    )
    winner = coordinator.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.CONFIRM)
    )
    assert loser.resume_attempt_ref is not None
    assert winner.resume_attempt_ref is not None
    assert coordinator.resume(
        SandboxResumeCommandV1(resume_attempt_ref=winner.resume_attempt_ref)
    ).status == "applied"
    snapshot = store.snapshot().model_dump(mode="python")
    loser_staged = next(
        transition
        for transition in snapshot["transitions"]
        if transition["resume_attempt_ref"] == loser.resume_attempt_ref
        and transition["to_state"] == "attempt_staged"
    )
    reordered = tuple(
        transition
        for transition in snapshot["transitions"]
        if transition is not loser_staged
    ) + (loser_staged,)
    for sequence, transition in enumerate(reordered):
        transition["sequence"] = sequence
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-",
            transition,
            ref_field="transition_ref",
        )
    snapshot["transitions"] = reordered

    with pytest.raises(SandboxReviewError) as raised:
        _restore_store(snapshot)
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l5_3_restart_snapshot_rejects_event_order_opposite_review_applied_order() -> None:
    clock = _FakeClock(value=2_000_000_100)
    coordinator, store, *_ = _coordinator(clock=clock)
    first_delivery = _issue(coordinator)
    first_staged, first_resumed = _stage_and_resume(
        coordinator,
        first_delivery,
        SandboxReviewAction.CONFIRM,
    )
    assert first_resumed.status == "applied"
    assert first_staged.resume_attempt_ref is not None

    clock.value = first_delivery.challenge.issued_at - 100
    second_delivery = coordinator.create_single_use_challenge(
        _accepted_source(domain_state_version=8),
        namespace=_NAMESPACE,
        thread_id=_THREAD_ID,
        checkpoint_id="sandbox-checkpoint-002",
        interrupt_id="sandbox-interrupt-002",
    )
    second_staged, second_resumed = _stage_and_resume(
        coordinator,
        second_delivery,
        SandboxReviewAction.CONFIRM,
    )
    assert second_resumed.status == "applied"
    assert second_staged.resume_attempt_ref is not None

    live_snapshot = store.snapshot()
    review_applied_attempts = tuple(
        transition.resume_attempt_ref
        for transition in live_snapshot.transitions
        if transition.to_state == "review_applied"
    )
    assert tuple(
        event.resume_attempt_ref for event in live_snapshot.events
    ) == review_applied_attempts == (
        first_staged.resume_attempt_ref,
        second_staged.resume_attempt_ref,
    )
    assert live_snapshot.events[0].applied_at > live_snapshot.events[1].applied_at
    assert (
        _restore_store(live_snapshot).snapshot()
        == live_snapshot
    )

    tampered = live_snapshot.model_dump(mode="python")
    reversed_events = tuple(reversed(tampered["events"]))
    for sequence, event in enumerate(reversed_events):
        event["sequence"] = sequence
        event["event_ref"] = _derived_record_ref(
            "sandbox-review-event-",
            event,
            ref_field="event_ref",
        )
    tampered["events"] = reversed_events

    with pytest.raises(SandboxReviewError) as raised:
        _restore_store(tampered)
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l5_3_restart_snapshot_rejects_initial_transition_order_opposite_issue_order() -> None:
    clock = _FakeClock(value=2_000_000_100)
    coordinator, store, *_ = _coordinator(clock=clock)
    first_delivery = _issue(coordinator)

    clock.value = first_delivery.challenge.issued_at - 100
    second_delivery = coordinator.create_single_use_challenge(
        _accepted_source(domain_state_version=8),
        namespace=_NAMESPACE,
        thread_id=_THREAD_ID,
        checkpoint_id="sandbox-checkpoint-002",
        interrupt_id="sandbox-interrupt-002",
    )

    live_snapshot = store.snapshot()
    assert tuple(
        challenge.challenge_ref for challenge in live_snapshot.challenges
    ) == (
        first_delivery.challenge.challenge_ref,
        second_delivery.challenge.challenge_ref,
    )
    assert (
        live_snapshot.challenges[1].issued_at
        < live_snapshot.challenges[0].issued_at
    )
    assert (
        _restore_store(live_snapshot).snapshot()
        == live_snapshot
    )

    tampered = live_snapshot.model_dump(mode="python")
    reversed_initial = tuple(reversed(tampered["transitions"]))
    for sequence, transition in enumerate(reversed_initial):
        transition["sequence"] = sequence
        transition["transition_ref"] = _derived_record_ref(
            "sandbox-transition-",
            transition,
            ref_field="transition_ref",
        )
    tampered["transitions"] = reversed_initial

    with pytest.raises(SandboxReviewError) as raised:
        _restore_store(tampered)
    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l5_3_missing_checkpoint_or_challenge_is_rejected_without_reconstruction() -> None:
    coordinator, store, *_ = _coordinator()
    delivery = _issue(coordinator)
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)
    snapshot = store.snapshot().model_dump(mode="python")

    without_challenge = dict(snapshot)
    without_challenge["challenges"] = ()
    with pytest.raises(SandboxReviewError) as missing_challenge:
        _restore_store(without_challenge)
    assert missing_challenge.value.__cause__ is None
    assert missing_challenge.value.__context__ is None

    staged = coordinator.stage_verified_resume_attempt(submission)
    assert staged.resume_attempt_ref is not None
    without_checkpoint = store.snapshot().model_dump(mode="python")
    without_checkpoint["checkpoints"] = ()
    with pytest.raises(SandboxReviewError) as missing_checkpoint:
        _restore_store(without_checkpoint)
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
    assert _NONCE.hex().encode() not in persisted
    assert _NONCE.hex() not in persisted_repr
    assert submission.proof.sandbox_test_signature.encode() in persisted
    assert submission.proof.sandbox_test_signature not in persisted_repr
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
