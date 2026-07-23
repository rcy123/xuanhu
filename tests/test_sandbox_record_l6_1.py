"""L6-1 sandbox medical record DTO and deterministic assembler tests."""

from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_runtime.sandbox_recheck import (
    SandboxRecheckCoordinator,
    SandboxRevisionCommandV1,
)
from app.agent_runtime.sandbox_record import (
    RECORD_SCHEMA_VERSION,
    SANDBOX_RECORD_DISCLAIMER,
    SandboxMedicalRecordData,
    SandboxRecordAssembler,
    SandboxRecordError,
    canonical_review_bytes,
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
)

_NAMESPACE = "sandbox.record.local"
_SESSION = "sandbox-record-session-001"
_THREAD = "sandbox-record-thread-001"
_OLD_CHECKPOINT = "sandbox-record-checkpoint-007"
_OLD_INTERRUPT = "sandbox-record-interrupt-007"
_NEW_CHECKPOINT = "sandbox-record-checkpoint-008"
_NEW_INTERRUPT = "sandbox-record-interrupt-008"
_NONCE = bytes(range(32))
_SCHEME = "sandbox-test-sha256.v1"
_KEY_ID = "sandbox-test-key-001"


class _FakeClock:
    def __init__(self, value: int = 2_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _NonceFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.value = _NONCE

    def __call__(self) -> bytes:
        self.calls += 1
        return self.value


class _Signer:
    @staticmethod
    def signature(payload_digest: str) -> str:
        return hashlib.sha256(f"sandbox-signature:{payload_digest}".encode()).hexdigest()


class _SignatureVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(
        self,
        *,
        signed_payload_digest: str,
        sandbox_test_signature_scheme: str,
        sandbox_test_key_id: str,
        sandbox_test_signature: str,
    ) -> bool:
        self.calls += 1
        expected_signature = hashlib.sha256(
            f"sandbox-signature:{signed_payload_digest}".encode()
        ).hexdigest()
        return (
            sandbox_test_signature_scheme == _SCHEME
            and sandbox_test_key_id == _KEY_ID
            and sandbox_test_signature == expected_signature
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
    graph_version: str = "sandbox-record-graph.v1",
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
        dataset_name="fixed-fictitious-record-fixture",
        dataset_version=dataset_version,
        formula_items=formula_items,
        profile_facts=profile_facts,
    )
    evaluation = SandboxSafetyEvaluationV1(
        decision=decision,
        issues=tuple(
            SandboxSafetyIssueV1(
                issue_id=f"sandbox.record.issue.{index:03d}",
                rule_id="sandbox.record.rule.001",
                severity=SandboxSafetySeverity.HIGH,
                execution_order=index,
            )
            for index in range(issue_count)
        ),
    )
    case = SandboxEvaluationCaseV1.build(
        case_id="fixed-fictitious-record-case-001",
        formula_items=formula_items,
        profile_facts=profile_facts,
        manifest=manifest,
        evaluation=evaluation,
    )
    authority = SandboxEvaluatorAuthorityV1.build(cases=(case,))
    bundle = SandboxRuleBundleV1.build(
        rule_bundle_version=(
            f"sandbox-record-rules.{formula_revision}"
            if bundle_version is None
            else bundle_version
        ),
        rules=(
            SandboxRuleV1(
                rule_id="sandbox.record.rule.001",
                rule_revision=(formula_revision if rule_revision is None else rule_revision),
                parameters=(),
            ),
        ),
        evaluator_authority=authority,
    )
    subject = SandboxSafetySubjectV1.build(
        test_session_id=_SESSION,
        domain_state_version=domain_state_version,
        formula_artifact_id="synthetic-record-formula-001",
        formula_revision=formula_revision,
        formula_items=formula_items,
        profile_artifact_id="synthetic-record-profile-001",
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
            sandbox_test_signature=_Signer.signature(payload_digest),
        ),
    )


def _accepted_modify_snapshot():
    subject, bundle = _subject_and_bundle(
        domain_state_version=7,
        formula_revision=3,
        amount_milliunits=1,
        dataset_version="1.0.0",
    )
    result = SandboxSafetyRuleAdapter().evaluate(
        subject,
        bundle,
        command_id="sandbox-record-old-command-001",
        run_id="sandbox-record-old-run-001",
        trace_id="sandbox-record-old-trace-001",
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
    staged = review.stage_verified_resume_attempt(
        _submission(delivery, SandboxReviewAction.MODIFY_FIXTURE)
    )
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
        command_id=f"sandbox-record-command-{suffix}",
        run_id=f"sandbox-record-run-{suffix}",
        trace_id=f"sandbox-record-trace-{suffix}",
        candidate_subject=candidate,
        rule_bundle=bundle,
        checkpoint_id=f"sandbox-record-checkpoint-{suffix}",
        interrupt_id=f"sandbox-record-interrupt-{suffix}",
    )


def _confirm_review_required_state():
    coordinator, *_ = _coordinator()
    outcome = coordinator.apply_revision(_revision_command(coordinator))
    assert outcome.delivery is not None
    staged = coordinator.stage_current_review(
        _submission(outcome.delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    assert coordinator.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    ).status == "applied"
    return coordinator


def _derive_mapping_ref(prefix: str, value: dict[str, object], field: str) -> str:
    body = dict(value)
    body.pop(field, None)
    return prefix + hashlib.sha256(canonical_review_bytes(body)).hexdigest()


def _refresh_mapping_ref(
    value: dict[str, object], *, prefix: str, field: str
) -> str:
    refreshed = _derive_mapping_ref(prefix, value, field)
    value[field] = refreshed
    return refreshed


class TestRecordRed:
    """RED tests that prove gaps before the production module exists."""

    def test_l6_1_red_imports_confirm_module_exists(self) -> None:
        """Verify the production module is importable."""
        assert SandboxMedicalRecordData is not None
        assert SandboxRecordAssembler is not None
        assert SandboxRecordError is not None

    def test_l6_1_red_confirm_review_state_produces_complete_record(self) -> None:
        """A confirmed review state must produce a complete medical record JSON."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()

        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        assert record.session_id == _SESSION
        assert len(record.revision_id) == 64
        assert len(record.reviewed_formula) >= 1
        assert record.safety_result["decision"] == "allow"
        assert record.review_confirm_ref.startswith("sandbox-attempt-")
        assert record.assembled_at == 2_000_000_100
        assert record.record_version == RECORD_SCHEMA_VERSION
        assert record.disclaimer == SANDBOX_RECORD_DISCLAIMER
        assert record.record_id.startswith("sandbox-record-")
        assert len(record.record_id) == len("sandbox-record-") + 64

    def test_l6_1_red_missing_revisions_raise_record_error(self) -> None:
        """An empty recheck snapshot must raise SandboxRecordError."""
        assembler = SandboxRecordAssembler()

        with pytest.raises(SandboxRecordError) as raised:
            assembler.assemble(
                {"revisions": (), "runs": (), "invalidations": (), "receipts": (),
                 "current_revision_ref": "", "review_snapshot": {}},
                namespace=_NAMESPACE,
                session_id=_SESSION,
                thread_id=_THREAD,
                checkpoint_id=_NEW_CHECKPOINT,
                now=2_000_000_100,
            )

        assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    def test_l6_1_red_wrong_checkpoint_raises_record_error(self) -> None:
        """A mismatched checkpoint must raise SandboxRecordError."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()

        with pytest.raises(SandboxRecordError) as raised:
            assembler.assemble(
                snapshot,
                namespace=_NAMESPACE,
                session_id=_SESSION,
                thread_id=_THREAD,
                checkpoint_id="sandbox-record-checkpoint-wrong",
                now=2_000_000_100,
            )

        assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    def test_l6_1_red_reject_action_does_not_produce_record(self) -> None:
        """A REJECT action must not produce a record."""
        coordinator, *_ = _coordinator()
        outcome = coordinator.apply_revision(_revision_command(coordinator))
        assert outcome.delivery is not None
        staged = coordinator.stage_current_review(
            _submission(outcome.delivery, SandboxReviewAction.REJECT)
        )
        assert staged.resume_attempt_ref is not None
        assert coordinator.resume_current_review(
            SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
        ).status == "applied"
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()

        with pytest.raises(SandboxRecordError) as raised:
            assembler.assemble(
                snapshot,
                namespace=_NAMESPACE,
                session_id=_SESSION,
                thread_id=_THREAD,
                checkpoint_id=_NEW_CHECKPOINT,
                now=2_000_000_100,
            )

        assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"

    def test_l6_1_red_tampered_review_confirm_ref_raises_record_error(self) -> None:
        """A tampered review confirm attempt ref must be rejected."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()

        bad = copy.deepcopy(snapshot.model_dump(mode="python"))
        current = bad["revisions"][-1]
        review_snapshot = bad["review_snapshot"]
        challenge_ref = current["challenge_ref"]

        exact_events = [
            event for event in review_snapshot["events"]
            if event["challenge_ref"] == challenge_ref
        ]
        assert len(exact_events) == 1
        event = exact_events[0]
        event["resume_attempt_ref"] = "sandbox-attempt-" + "f" * 64
        event["event_ref"] = _derive_mapping_ref(
            "sandbox-review-event-", event, "event_ref"
        )

        with pytest.raises(SandboxRecordError) as raised:
            assembler.assemble(
                bad,
                namespace=_NAMESPACE,
                session_id=_SESSION,
                thread_id=_THREAD,
                checkpoint_id=_NEW_CHECKPOINT,
                now=2_000_000_100,
            )

        assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"

    def test_l6_1_red_deterministic_same_input_same_output(self) -> None:
        """Same confirmed state must produce byte-identical records."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()

        first = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        second = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        assert first == second
        assert first.record_id == second.record_id
        assert canonical_review_bytes(first) == canonical_review_bytes(second)

    def test_l6_1_red_record_fields_are_immutable(self) -> None:
        """Record fields must be immutable."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        with pytest.raises(ValidationError):
            record.disclaimer = "changed"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            record.assembled_at = 0  # type: ignore[misc]
        with pytest.raises(AttributeError):
            record.reviewed_formula.append({"bad": True})  # type: ignore[attr-defined]

    def test_l6_1_red_record_id_is_collision_resistant(self) -> None:
        """Different confirmed states must produce different record IDs."""
        coord1 = _confirm_review_required_state()
        snap1 = coord1.snapshot()
        assembler = SandboxRecordAssembler()
        record1 = assembler.assemble(
            snap1,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        record2 = assembler.assemble(
            snap1,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_200,
        )

        assert record1.record_id != record2.record_id

    def test_l6_1_red_no_settings_env_network_imports(self) -> None:
        """Production module must only import approved pure-local dependencies."""
        module_path = Path("app/agent_runtime/sandbox_record.py")
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", maxsplit=1)[0])

        assert imported_roots <= {
            "__future__",
            "collections",
            "enum",
            "hashlib",
            "json",
            "pydantic",
            "typing",
            "app",
        }
        assert "os.environ" not in source
        assert "getenv(" not in source
        assert "data/" not in source
        assert ".env" not in source
        for token in ("http://", "https://", "socket", "subprocess", "requests"):
            assert token not in source

    def test_l6_1_red_assembler_has_no_model_or_network_calls(self) -> None:
        """Assembler must not call any model or have network capabilities."""
        source = Path("app/agent_runtime/sandbox_record.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        forbidden = {"open", "print", "breakpoint", "exec", "eval", "compile"}
        assert called_names.isdisjoint(forbidden)

        assembler = SandboxRecordAssembler()
        assert assembler.__slots__ == ()

    def test_l6_1_red_errors_are_chainless_and_payload_free(self) -> None:
        """All errors must be fixed, chainless, and payload-free."""
        assembler = SandboxRecordAssembler()

        try:
            raise SandboxRecordError()
        except SandboxRecordError as error:
            assert error.__cause__ is None
            assert error.__context__ is None
            assert str(error) == "SANDBOX_RECORD_UNAVAILABLE"
            assert error.__slots__ == ()

        with pytest.raises(SandboxRecordError) as raised:
            assembler.assemble(
                None,
                namespace=_NAMESPACE,
                session_id=_SESSION,
                thread_id=_THREAD,
                checkpoint_id=_NEW_CHECKPOINT,
                now=2_000_000_100,
            )
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
