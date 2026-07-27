"""Adversarial regressions for the L5 authority boundary."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

import app.agent_runtime.sandbox_safety as safety_module
from app.agent_runtime.sandbox_recheck import (
    SandboxRecheckCoordinator,
    SandboxRecheckError,
)
from app.agent_runtime.sandbox_review import (
    SandboxImmutableRuleBundleRegistry,
    SandboxInMemoryReviewStore,
    SandboxResumeCommandV1,
    SandboxReviewAction,
    SandboxReviewCoordinator,
    SandboxReviewError,
    SandboxReviewSourceV1,
)
from app.agent_runtime.sandbox_safety import (
    MAX_CANONICAL_BYTES,
    SandboxEvaluationCaseV1,
    SandboxEvaluatorAuthorityV1,
    SandboxFormulaItemV1,
    SandboxProfileFactV1,
    SandboxRuleBundleV1,
    SandboxRuleParameterV1,
    SandboxRuleV1,
    SandboxSafetyAdapterError,
    SandboxSafetyDecision,
    SandboxSafetyEvaluationV1,
    SandboxSafetyFailureCode,
    SandboxSafetyRuleAdapter,
    SandboxSafetySubjectV1,
    SandboxSyntheticManifestV1,
    canonical_json_bytes,
)
from tests.test_l5_3_sandbox_reviewer_interrupt_resume import (
    _CHECKPOINT_ID,
    _INTERRUPT_ID,
    _NAMESPACE,
    _THREAD_ID,
    _accepted_source,
    _FakeClock,
    _FakeNonceFactory,
    _FakeSignatureVerifier,
    _submission,
)
from tests.test_l5_4_sandbox_modify_full_recheck import (
    _accepted_modify_snapshot,
    _revision_command,
    _subject_and_bundle,
)
from tests.test_l5_4_sandbox_modify_full_recheck import (
    _coordinator as _recheck_coordinator,
)


class _SelectiveRuleBundleAuthorizer:
    def __init__(
        self,
        *,
        recognized: set[str],
        authorized: set[str],
    ) -> None:
        self.recognized = recognized
        self.authorized = authorized

    def recognize(self, *, rule_bundle: SandboxRuleBundleV1) -> bool:
        return rule_bundle.rule_bundle_digest in self.recognized

    def authorize(self, *, rule_bundle: SandboxRuleBundleV1) -> bool:
        return rule_bundle.rule_bundle_digest in self.authorized


def test_immutable_rule_bundle_registry_is_a_concrete_trust_root() -> None:
    trusted = _accepted_source().safety_rule_bundle
    _, untrusted = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="immutable-registry.2",
    )
    registry = SandboxImmutableRuleBundleRegistry(
        recognized_rule_bundle_digests=(trusted.rule_bundle_digest,),
        authorized_rule_bundle_digests=(trusted.rule_bundle_digest,),
    )

    assert registry.recognize(rule_bundle=trusted)
    assert registry.authorize(rule_bundle=trusted)
    assert not registry.recognize(rule_bundle=untrusted)
    assert not registry.authorize(rule_bundle=untrusted)
    with pytest.raises(AttributeError):
        registry._authorized = frozenset({untrusted.rule_bundle_digest})


def _result_digest(
    *,
    adapter_version: str,
    decision_subject_digest: str,
    decision: SandboxSafetyDecision,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "adapter_version": adapter_version,
                "decision": decision,
                "decision_subject_digest": decision_subject_digest,
                "issues": (),
                "sandbox_schema_version": "sandbox-safety-result.v1",
            }
        )
    ).hexdigest()


def test_review_source_replays_rule_bundle_and_rejects_forged_allow() -> None:
    blocked = _accepted_source(decision=SandboxSafetyDecision.BLOCK)
    forged_allow = blocked.safety_result.model_copy(
        update={
            "decision": SandboxSafetyDecision.ALLOW,
            "issues": (),
            "result_digest": _result_digest(
                adapter_version=blocked.safety_result.adapter_version,
                decision_subject_digest=(
                    blocked.safety_result.decision_subject_digest
                ),
                decision=SandboxSafetyDecision.ALLOW,
            ),
        }
    )

    with pytest.raises(ValidationError):
        SandboxReviewSourceV1.build(
            safety_subject=blocked.safety_subject,
            safety_rule_bundle=blocked.safety_rule_bundle,
            safety_result=forged_allow,
            safety_command_id=blocked.safety_command_id,
            safety_run_id=blocked.safety_run_id,
            safety_trace_id=blocked.safety_trace_id,
            explanation_result=None,
        )


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("fixed-fictitious-profile", "synthetic +86 138 1234 5678"),
        ("fixed-fictitious-profile", "synthetic 138\u200b1234\u200b5678"),
        ("fixed-fictitious-profile", "138\u034f0013\u034f8000"),
        ("fixed-fictitious-profile", "110105 1949 1231 002X"),
        ("fixed-fictitious-profile", "010-12345678"),
        ("fixed-fictitious-profile", "测试@例子.公司"),
        ("fixed-fictitious-profile", "alice@example.com!"),
        ("fixed-fictitious-profile", "alice@example.com?"),
        ("fixed-fictitious-profile", "邮箱alice@example.com。"),
        ("fixed-fictitious-profile", "alice@example.com/path"),
        ("手机号", "fixed-fictitious-value"),
        ("patient_name", "fixed-fictitious-value"),
        ("first_name", "fixed-fictitious-value"),
        ("last_name", "fixed-fictitious-value"),
        ("given_name", "fixed-fictitious-value"),
        ("family_name", "fixed-fictitious-value"),
        ("surname", "fixed-fictitious-value"),
        ("id_card", "fixed-fictitious-value"),
        ("id number", "fixed-fictitious-value"),
        ("identity_number", "fixed-fictitious-value"),
        ("contact_details", "fixed-fictitious-value"),
        ("home-address", "fixed-fictitious-value"),
        ("mrn", "MRN-00001"),
        ("就诊号", "VISIT-00001"),
        ("姓\u200b名", "fixed-fictitious-value"),
        ("姓\u034f名", "fixed-fictitious-value"),
        ("姓\ufe0f名", "fixed-fictitious-value"),
        ("名字", "fixed-fictitious-value"),
        ("全名", "fixed-fictitious-value"),
        ("电话", "fixed-fictitious-value"),
        ("手机", "fixed-fictitious-value"),
        ("证件号", "fixed-fictitious-value"),
        ("挂号号", "fixed-fictitious-value"),
        ("联系方式", "fixed-fictitious-value"),
        ("家庭住址", "fixed-fictitious-value"),
        ("phone", "fixed-fictitious-value"),
        ("outpatient_no", "fixed-fictitious-value"),
        ("contact_phone", "fixed-fictitious-value"),
    ),
)
def test_manifest_builder_runs_configured_identifier_scan(
    name: str,
    value: str,
) -> None:
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
            fact_id="synthetic-profile-001",
            name=name,
            value=value,
        ),
    )

    with pytest.raises(ValueError, match="prohibited identifier"):
        SandboxSyntheticManifestV1.build(
            dataset_name="fixed-fictitious-fixture",
            dataset_version="1.0.0",
            formula_items=formula_items,
            profile_facts=profile_facts,
        )


@pytest.mark.parametrize(
    "dataset_name",
    (
        "dataset_name",
        "component_name",
        "id",
        "mobile_threshold",
        "patient_name_label",
    ),
)
def test_identifier_scan_does_not_treat_generic_or_suffixed_keys_as_identity(
    dataset_name: str,
) -> None:
    manifest = SandboxSyntheticManifestV1.build(
        dataset_name="fixed-fictitious-fixture",
        dataset_version="1.0.0",
        formula_items=(
            SandboxFormulaItemV1(
                item_id="synthetic-item-001",
                component="fixed-fictitious-component",
                amount_milliunits=1,
                unit="synthetic_unit",
            ),
        ),
        profile_facts=(
            SandboxProfileFactV1(
                fact_id="synthetic-profile-001",
                name=dataset_name,
                value="bounded-test-value",
            ),
        ),
    )

    assert manifest.prohibited_identifier_scan.result == (
        "passed_no_configured_identifier_pattern_matches"
    )


def test_identifier_scan_does_not_reject_generic_narrative_text() -> None:
    manifest = SandboxSyntheticManifestV1.build(
        dataset_name="fixed-fictitious-fixture",
        dataset_version="1.0.0",
        formula_items=(
            SandboxFormulaItemV1(
                item_id="synthetic-item-001",
                component="fixed-fictitious-component",
                amount_milliunits=1,
                unit="synthetic_unit",
            ),
        ),
        profile_facts=(
            SandboxProfileFactV1(
                fact_id="synthetic-profile-001",
                name="fixed-fictitious-profile",
                value="the name of this synthetic component is alpha",
            ),
        ),
    )

    assert manifest.prohibited_identifier_scan.result == (
        "passed_no_configured_identifier_pattern_matches"
    )


def test_manifest_scans_formula_text_for_identity_key_markers() -> None:
    with pytest.raises(ValueError, match="prohibited identifier"):
        SandboxSyntheticManifestV1.build(
            dataset_name="fixed-fictitious-fixture",
            dataset_version="1.0.0",
            formula_items=(
                SandboxFormulaItemV1(
                    item_id="synthetic-item-001",
                    component="patient_name",
                    amount_milliunits=1,
                    unit="synthetic_unit",
                ),
            ),
            profile_facts=(
                SandboxProfileFactV1(
                    fact_id="synthetic-profile-001",
                    name="fixed-fictitious-profile",
                    value="bounded-test-value",
                ),
            ),
        )


def test_adapter_rechecks_content_instead_of_trusting_passed_scan_literal() -> None:
    formula_items = (
        SandboxFormulaItemV1(
            item_id="synthetic-item-001",
            component="fixed-fictitious-component",
            amount_milliunits=1,
            unit="synthetic_unit",
        ),
    )
    safe_facts = (
        SandboxProfileFactV1(
            fact_id="synthetic-profile-001",
            name="fixed-fictitious-profile",
            value="bounded-test-value",
        ),
    )
    unsafe_facts = (
        safe_facts[0].model_copy(update={"value": "synthetic 13812345678"}),
    )
    self_attested_manifest = SandboxSyntheticManifestV1.build(
        dataset_name="fixed-fictitious-fixture",
        dataset_version="1.0.0",
        formula_items=formula_items,
        profile_facts=safe_facts,
    )
    evaluation_case = SandboxEvaluationCaseV1.build(
        case_id="fixed-fictitious-case-001",
        formula_items=formula_items,
        profile_facts=unsafe_facts,
        manifest=self_attested_manifest,
        evaluation=SandboxSafetyEvaluationV1(
            decision=SandboxSafetyDecision.ALLOW,
            issues=(),
        ),
    )
    authority = SandboxEvaluatorAuthorityV1.build(cases=(evaluation_case,))
    bundle = SandboxRuleBundleV1.build(
        rule_bundle_version="sandbox-rules.v1",
        rules=(),
        evaluator_authority=authority,
    )
    subject = SandboxSafetySubjectV1.build(
        test_session_id="sandbox-test-session-001",
        domain_state_version=1,
        formula_artifact_id="synthetic-formula-001",
        formula_revision=1,
        formula_items=formula_items,
        profile_artifact_id="synthetic-profile-001",
        profile_revision=1,
        profile_facts=unsafe_facts,
        graph_version="sandbox-graph.v1",
        rule_bundle_version=bundle.rule_bundle_version,
        rule_bundle_digest=bundle.rule_bundle_digest,
        evaluator_authority_digest=authority.authority_digest,
        synthetic_manifest=self_attested_manifest,
    )

    with pytest.raises(SandboxSafetyAdapterError) as raised:
        SandboxSafetyRuleAdapter().evaluate(
            subject,
            bundle,
            command_id="sandbox-command-001",
            run_id="sandbox-run-001",
            trace_id="sandbox-trace-001",
        )

    assert raised.value.code is SandboxSafetyFailureCode.PROHIBITED_IDENTIFIER


def _source_for_package(
    subject: SandboxSafetySubjectV1,
    bundle: SandboxRuleBundleV1,
) -> SandboxReviewSourceV1:
    result = SandboxSafetyRuleAdapter().evaluate(
        subject,
        bundle,
        command_id="sandbox-authority-command-001",
        run_id="sandbox-authority-run-001",
        trace_id="sandbox-authority-trace-001",
    )
    return SandboxReviewSourceV1.build(
        safety_subject=subject,
        safety_rule_bundle=bundle,
        safety_result=result,
        safety_command_id="sandbox-authority-command-001",
        safety_run_id="sandbox-authority-run-001",
        safety_trace_id="sandbox-authority-trace-001",
        explanation_result=None,
    )


def _review_with_authorizer(
    authorizer: _SelectiveRuleBundleAuthorizer,
) -> tuple[
    SandboxReviewCoordinator,
    SandboxInMemoryReviewStore,
    _FakeNonceFactory,
    _FakeSignatureVerifier,
]:
    store = SandboxInMemoryReviewStore()
    nonce_factory = _FakeNonceFactory()
    signature_verifier = _FakeSignatureVerifier()
    return (
        SandboxReviewCoordinator(
            store=store,
            clock=_FakeClock(),
            nonce_factory=nonce_factory,
            signature_verifier=signature_verifier,
            rule_bundle_authorizer=authorizer,
        ),
        store,
        nonce_factory,
        signature_verifier,
    )


def test_review_rejects_self_consistent_allow_from_untrusted_bundle() -> None:
    trusted = _accepted_source()
    subject, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="authority-substitution.2",
    )
    untrusted = _source_for_package(subject, bundle)
    authorizer = _SelectiveRuleBundleAuthorizer(
        recognized={
            trusted.safety_rule_bundle.rule_bundle_digest,
            bundle.rule_bundle_digest,
        },
        authorized={trusted.safety_rule_bundle.rule_bundle_digest},
    )
    coordinator, store, nonce_factory, _ = _review_with_authorizer(authorizer)

    with pytest.raises(SandboxReviewError) as raised:
        coordinator.create_single_use_challenge(
            untrusted,
            namespace="sandbox.authority.local",
            thread_id="sandbox-authority-thread-001",
            checkpoint_id="sandbox-authority-checkpoint-001",
            interrupt_id="sandbox-authority-interrupt-001",
        )

    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert nonce_factory.calls == 0
    assert store.snapshot().challenges == ()


@pytest.mark.parametrize("phase", ("stage", "resume", "eligibility"))
def test_revoked_bundle_blocks_every_review_operation_before_side_effect(
    phase: str,
) -> None:
    source = _accepted_source()
    digest = source.safety_rule_bundle.rule_bundle_digest
    authorizer = _SelectiveRuleBundleAuthorizer(
        recognized={digest},
        authorized={digest},
    )
    coordinator, _, _, signature_verifier = _review_with_authorizer(authorizer)
    delivery = coordinator.create_single_use_challenge(
        source,
        namespace=_NAMESPACE,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
        interrupt_id=_INTERRUPT_ID,
    )
    submission = _submission(delivery, SandboxReviewAction.CONFIRM)

    if phase == "stage":
        authorizer.authorized.clear()
        assert coordinator.stage_verified_resume_attempt(submission).status == (
            "resume_rejected"
        )
        assert signature_verifier.calls == 0
        return

    staged = coordinator.stage_verified_resume_attempt(submission)
    assert staged.resume_attempt_ref is not None
    authorizer.authorized.clear()
    if phase == "resume":
        assert coordinator.resume(
            SandboxResumeCommandV1(
                resume_attempt_ref=staged.resume_attempt_ref
            )
        ).status == "resume_rejected"
        return

    authorizer.authorized.add(digest)
    assert coordinator.resume(
        SandboxResumeCommandV1(
            resume_attempt_ref=staged.resume_attempt_ref
        )
    ).status == "applied"
    authorizer.authorized.clear()
    assert coordinator.eligibility(
        namespace=_NAMESPACE,
        test_session_id=source.safety_subject.test_session_id,
        thread_id=_THREAD_ID,
        checkpoint_id=_CHECKPOINT_ID,
    ).status == "blocked"


def test_recheck_rejects_unapproved_candidate_before_snapshot_change() -> None:
    review_snapshot, clock, nonce_factory, signature_verifier = (
        _accepted_modify_snapshot()
    )
    initial_bundle = review_snapshot.sources[0].source.safety_rule_bundle
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="unapproved-transition.2",
    )
    authorizer = _SelectiveRuleBundleAuthorizer(
        recognized={
            initial_bundle.rule_bundle_digest,
            bundle.rule_bundle_digest,
        },
        authorized={initial_bundle.rule_bundle_digest},
    )
    coordinator = SandboxRecheckCoordinator(
        review_snapshot=review_snapshot,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=signature_verifier,
        rule_bundle_authorizer=authorizer,
    )
    before = coordinator.snapshot()

    with pytest.raises(SandboxRecheckError) as raised:
        coordinator.apply_revision(
            _revision_command(
                coordinator,
                candidate=candidate,
                bundle=bundle,
                suffix="unapproved-transition",
            )
        )

    assert str(raised.value) == "SANDBOX_RECHECK_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert coordinator.snapshot() == before


@pytest.mark.parametrize("invalid_result", (1, {"decision": "allow"}))
def test_recheck_invalid_evaluator_return_never_leaks_dynamic_error(
    monkeypatch: pytest.MonkeyPatch,
    invalid_result: object,
) -> None:
    coordinator, *_ = _recheck_coordinator()
    monkeypatch.setattr(
        SandboxSafetyRuleAdapter,
        "evaluate",
        lambda *_args, **_kwargs: invalid_result,
    )

    outcome = coordinator.apply_revision(
        _revision_command(coordinator, suffix="invalid-evaluator-return")
    )
    current = coordinator.snapshot().revisions[-1]

    assert outcome.status == "recheck_failed"
    assert current.status == "recheck_failed"
    assert current.result is None


def test_recheck_forged_well_formed_result_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, *_ = _recheck_coordinator()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="forged-evaluator.2",
        decision=SandboxSafetyDecision.BLOCK,
        issue_count=1,
    )
    blocked = SandboxSafetyRuleAdapter().evaluate(
        candidate,
        bundle,
        command_id="sandbox-recheck-command-forged-evaluator",
        run_id="sandbox-recheck-run-forged-evaluator",
        trace_id="sandbox-recheck-trace-forged-evaluator",
    )
    forged = blocked.model_copy(
        update={
            "decision": SandboxSafetyDecision.ALLOW,
            "issues": (),
            "result_digest": _result_digest(
                adapter_version=blocked.adapter_version,
                decision_subject_digest=blocked.decision_subject_digest,
                decision=SandboxSafetyDecision.ALLOW,
            ),
        }
    )
    monkeypatch.setattr(
        SandboxSafetyRuleAdapter,
        "evaluate",
        lambda *_args, **_kwargs: forged,
    )

    outcome = coordinator.apply_revision(
        _revision_command(
            coordinator,
            candidate=candidate,
            bundle=bundle,
            suffix="forged-evaluator",
        )
    )
    current = coordinator.snapshot().revisions[-1]

    assert outcome.status == "recheck_failed"
    assert current.result is None
    assert current.challenge_ref is None


def test_wire_v1_manifest_and_review_source_are_explicitly_incompatible() -> None:
    manifest = _accepted_source().safety_subject.synthetic_manifest
    old_manifest = manifest.model_dump(mode="python")
    old_manifest["schema_version"] = "sandbox-synthetic-manifest.v1"

    with pytest.raises(ValidationError):
        SandboxSyntheticManifestV1.model_validate(old_manifest, strict=True)

    source = _accepted_source()
    old_source = source.model_dump(mode="python")
    for field_name in (
        "safety_rule_bundle",
        "safety_command_id",
        "safety_run_id",
        "safety_trace_id",
    ):
        old_source.pop(field_name)

    with pytest.raises(ValidationError):
        SandboxReviewSourceV1.model_validate(old_source, strict=True)


@pytest.mark.parametrize("kind", ("session", "rule_parameter"))
def test_adapter_scans_identifier_bearing_package_metadata(kind: str) -> None:
    source = _accepted_source()
    bundle = source.safety_rule_bundle
    test_session_id = source.safety_subject.test_session_id
    if kind == "session":
        test_session_id = "sandbox-13800138000"
    else:
        bundle = SandboxRuleBundleV1.build(
            rule_bundle_version=source.safety_rule_bundle.rule_bundle_version,
            rules=(
                SandboxRuleV1(
                    rule_id="sandbox.rule.metadata.v1",
                    rule_revision=1,
                    parameters=(
                        SandboxRuleParameterV1(
                            name="medical_record_number",
                            value="MRN-00001",
                        ),
                    ),
                ),
            ),
            evaluator_authority=(
                source.safety_rule_bundle.evaluator_authority
            ),
        )
    subject = SandboxSafetySubjectV1.build(
        test_session_id=test_session_id,
        domain_state_version=source.safety_subject.domain_state_version,
        formula_artifact_id=source.safety_subject.formula_artifact_id,
        formula_revision=source.safety_subject.formula_revision,
        formula_items=source.safety_subject.formula_items,
        profile_artifact_id=source.safety_subject.profile_artifact_id,
        profile_revision=source.safety_subject.profile_revision,
        profile_facts=source.safety_subject.profile_facts,
        graph_version=source.safety_subject.graph_version,
        rule_bundle_version=bundle.rule_bundle_version,
        rule_bundle_digest=bundle.rule_bundle_digest,
        evaluator_authority_digest=(
            bundle.evaluator_authority.authority_digest
        ),
        synthetic_manifest=source.safety_subject.synthetic_manifest,
    )

    with pytest.raises(SandboxSafetyAdapterError) as raised:
        SandboxSafetyRuleAdapter().evaluate(
            subject,
            bundle,
            command_id="sandbox-metadata-command-001",
            run_id="sandbox-metadata-run-001",
            trace_id="sandbox-metadata-trace-001",
        )

    assert raised.value.code is SandboxSafetyFailureCode.PROHIBITED_IDENTIFIER


def test_recheck_rejects_identifier_bearing_run_id_without_a_write() -> None:
    coordinator, *_ = _recheck_coordinator()
    before = coordinator.snapshot()
    command = _revision_command(coordinator, suffix="identifier-envelope")
    command = command.model_copy(
        update={"command_id": "sandbox-13800138000"}
    )

    with pytest.raises(SandboxRecheckError) as raised:
        coordinator.apply_revision(command)

    assert str(raised.value) == "SANDBOX_RECHECK_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert coordinator.snapshot() == before


def test_review_restore_rejects_unrecognized_historical_bundle() -> None:
    review_snapshot, clock, nonce_factory, signature_verifier = (
        _accepted_modify_snapshot()
    )
    store = SandboxInMemoryReviewStore(
        snapshot=review_snapshot,
        signature_verifier=signature_verifier,
    )
    authorizer = _SelectiveRuleBundleAuthorizer(
        recognized=set(),
        authorized=set(),
    )

    with pytest.raises(SandboxReviewError) as raised:
        SandboxReviewCoordinator(
            store=store,
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=signature_verifier,
            rule_bundle_authorizer=authorizer,
        )

    assert str(raised.value) == "SANDBOX_REVIEW_REJECTED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_recheck_snapshot_uses_accepted_historical_recognition_cache() -> None:
    review_snapshot, clock, nonce_factory, signature_verifier = (
        _accepted_modify_snapshot()
    )
    initial_bundle = review_snapshot.sources[0].source.safety_rule_bundle
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="recognized-cache.2",
    )
    authorizer = _SelectiveRuleBundleAuthorizer(
        recognized={
            initial_bundle.rule_bundle_digest,
            bundle.rule_bundle_digest,
        },
        authorized={
            initial_bundle.rule_bundle_digest,
            bundle.rule_bundle_digest,
        },
    )
    coordinator = SandboxRecheckCoordinator(
        review_snapshot=review_snapshot,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=signature_verifier,
        rule_bundle_authorizer=authorizer,
    )
    outcome = coordinator.apply_revision(
        _revision_command(
            coordinator,
            candidate=candidate,
            bundle=bundle,
            suffix="recognized-cache",
        )
    )
    assert outcome.status == "review_required"
    accepted = coordinator.snapshot()

    authorizer.recognized.clear()

    assert coordinator.snapshot() == accepted
    with pytest.raises(SandboxRecheckError):
        SandboxRecheckCoordinator(
            snapshot=accepted,
            clock=clock,
            nonce_factory=nonce_factory,
            signature_verifier=signature_verifier,
            rule_bundle_authorizer=authorizer,
        )


def test_recheck_transition_does_not_rerecognize_cached_history() -> None:
    review_snapshot, clock, nonce_factory, signature_verifier = (
        _accepted_modify_snapshot()
    )
    initial_bundle = review_snapshot.sources[0].source.safety_rule_bundle
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=2,
        dataset_version="closed-recognition-cache.2",
    )
    authorizer = _SelectiveRuleBundleAuthorizer(
        recognized={
            initial_bundle.rule_bundle_digest,
            bundle.rule_bundle_digest,
        },
        authorized={
            initial_bundle.rule_bundle_digest,
            bundle.rule_bundle_digest,
        },
    )
    coordinator = SandboxRecheckCoordinator(
        review_snapshot=review_snapshot,
        clock=clock,
        nonce_factory=nonce_factory,
        signature_verifier=signature_verifier,
        rule_bundle_authorizer=authorizer,
    )
    authorizer.recognized.remove(initial_bundle.rule_bundle_digest)

    outcome = coordinator.apply_revision(
        _revision_command(
            coordinator,
            candidate=candidate,
            bundle=bundle,
            suffix="closed-recognition-cache",
        )
    )

    assert outcome.status == "review_required"
    assert coordinator.snapshot().revisions[-1].rule_bundle == bundle


def test_review_store_exposes_no_public_state_mutator() -> None:
    store = SandboxInMemoryReviewStore()

    for method_name in (
        "recover_challenge",
        "issue",
        "stage",
        "apply",
        "eligibility",
    ):
        assert not hasattr(store, method_name)


@pytest.mark.parametrize("field_name", ("command_id", "run_id", "trace_id"))
def test_adapter_rejects_identifier_bearing_run_envelope_fields(
    field_name: str,
) -> None:
    source = _accepted_source()
    identifiers = {
        "command_id": "sandbox-command-safe-001",
        "run_id": "sandbox-run-safe-001",
        "trace_id": "sandbox-trace-safe-001",
    }
    identifiers[field_name] = f"sandbox-{field_name}-13800138000"

    with pytest.raises(SandboxSafetyAdapterError) as raised:
        SandboxSafetyRuleAdapter().evaluate(
            source.safety_subject,
            source.safety_rule_bundle,
            **identifiers,
        )

    assert raised.value.code is SandboxSafetyFailureCode.PROHIBITED_IDENTIFIER


def test_adapter_rejects_oversized_wire_before_model_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _accepted_source()
    calls = 0

    def forbidden_parse(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("oversized wire reached Pydantic parsing")

    monkeypatch.setattr(safety_module, "_parse_model", forbidden_parse)

    with pytest.raises(SandboxSafetyAdapterError) as raised:
        SandboxSafetyRuleAdapter().evaluate(
            b"x" * (MAX_CANONICAL_BYTES + 1),
            source.safety_rule_bundle,
            command_id="sandbox-wire-command-001",
            run_id="sandbox-wire-run-001",
            trace_id="sandbox-wire-trace-001",
        )

    assert raised.value.code is SandboxSafetyFailureCode.LIMIT_EXCEEDED
    assert calls == 0
