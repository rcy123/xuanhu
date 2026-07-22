"""Offline L5-4 full recheck and review invalidation reference coordinator."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_runtime.sandbox_review import (
    SandboxChallengeDeliveryV1,
    SandboxInMemoryReviewStore,
    SandboxResumeCommandV1,
    SandboxResumeSubmissionV1,
    SandboxReviewAction,
    SandboxReviewChallengeV1,
    SandboxReviewCoordinator,
    SandboxReviewSourceV1,
    SandboxReviewStoreSnapshotV1,
    SandboxSignatureVerifier,
)
from app.agent_runtime.sandbox_safety import (
    SandboxRuleBundleV1,
    SandboxSafetyAdapterError,
    SandboxSafetyDecision,
    SandboxSafetyFailureCode,
    SandboxSafetyResultV1,
    SandboxSafetyRuleAdapter,
    SandboxSafetySubjectV1,
    canonical_json_bytes,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_REF_PATTERN = r"^sandbox-recheck-[a-z-]+[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class _ReviewSourceRecord(Protocol):
    namespace: str
    test_session_id: str
    thread_id: str
    checkpoint_id: str
    interrupt_id: str
    source: SandboxReviewSourceV1


class SandboxRecheckError(ValueError):
    """Fixed, payload-free L5-4 failure boundary."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("SANDBOX_RECHECK_REJECTED")


class SandboxRevisionCommandV1(_StrictFrozenModel):
    expected_current_revision_ref: str = Field(pattern=_REF_PATTERN)
    command_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    run_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    trace_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    candidate_subject: SandboxSafetySubjectV1
    rule_bundle: SandboxRuleBundleV1
    checkpoint_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    interrupt_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)


class SandboxRecheckStageResultV1(_StrictFrozenModel):
    status: Literal["staged", "resume_rejected"]
    resume_attempt_ref: str | None = None


class SandboxRecheckResumeResultV1(_StrictFrozenModel):
    status: Literal["applied", "replayed_or_conflict", "resume_rejected"]
    command: SandboxResumeCommandV1 | None = None


class SandboxCompletionEligibilityV1(_StrictFrozenModel):
    status: Literal["eligible", "blocked"]


class SandboxModificationResultV1(_StrictFrozenModel):
    status: Literal[
        "review_required",
        "blocked",
        "recheck_failed",
        "review_setup_failed",
        "replayed_or_conflict",
    ]
    current_revision_ref: str = Field(pattern=_REF_PATTERN)
    delivery: SandboxChallengeDeliveryV1 | None = None


class SandboxRevisionRecordV1(_StrictFrozenModel):
    sequence: int = Field(ge=0)
    revision_ref: str = Field(pattern=_REF_PATTERN)
    parent_revision_ref: str | None = Field(default=None, pattern=_REF_PATTERN)
    accepted_command_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    namespace: str
    test_session_id: str
    thread_id: str
    checkpoint_id: str
    interrupt_id: str
    subject: SandboxSafetySubjectV1
    rule_bundle: SandboxRuleBundleV1 | None
    result: SandboxSafetyResultV1 | None
    explanation_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    review_render_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    review_schema_version: str = Field(
        min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN
    )
    challenge_ref: str | None = None
    status: Literal[
        "modify_applied",
        "review_required",
        "blocked",
        "recheck_failed",
        "review_setup_failed",
    ]

    @model_validator(mode="after")
    def ref_is_derived(self) -> SandboxRevisionRecordV1:
        if self.revision_ref != _derived_ref(
            "sandbox-recheck-revision-", self, "revision_ref"
        ):
            raise ValueError("revision reference mismatch")
        return self


class SandboxRecheckRunV1(_StrictFrozenModel):
    sequence: int = Field(ge=0)
    run_ref: str = Field(pattern=_REF_PATTERN)
    command_digest: str = Field(pattern=_DIGEST_PATTERN)
    command_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    run_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    trace_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    old_revision_ref: str = Field(pattern=_REF_PATTERN)
    new_revision_ref: str = Field(pattern=_REF_PATTERN)
    status: Literal[
        "review_required", "blocked", "recheck_failed", "review_setup_failed"
    ]
    result_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    challenge_ref: str | None = None

    @model_validator(mode="after")
    def ref_is_derived(self) -> SandboxRecheckRunV1:
        if self.run_ref != _derived_ref("sandbox-recheck-run-", self, "run_ref"):
            raise ValueError("run reference mismatch")
        return self


class SandboxInvalidationV1(_StrictFrozenModel):
    sequence: int = Field(ge=0)
    invalidation_ref: str = Field(pattern=_REF_PATTERN)
    old_revision_ref: str = Field(pattern=_REF_PATTERN)
    new_revision_ref: str = Field(pattern=_REF_PATTERN)
    old_subject_digest: str = Field(pattern=_DIGEST_PATTERN)
    old_result_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    old_explanation_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    old_review_render_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    old_challenge_refs: tuple[str, ...]
    old_event_refs: tuple[str, ...]

    @model_validator(mode="after")
    def ref_is_derived(self) -> SandboxInvalidationV1:
        if self.invalidation_ref != _derived_ref(
            "sandbox-recheck-invalidation-", self, "invalidation_ref"
        ):
            raise ValueError("invalidation reference mismatch")
        return self


class _CommandReceiptV1(_StrictFrozenModel):
    sequence: int = Field(ge=0)
    receipt_ref: str = Field(pattern=_REF_PATTERN)
    command_digest: str = Field(pattern=_DIGEST_PATTERN)
    command_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    run_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    trace_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=128)
    old_revision_ref: str = Field(pattern=_REF_PATTERN)
    new_revision_ref: str = Field(pattern=_REF_PATTERN)
    status: Literal[
        "review_required", "blocked", "recheck_failed", "review_setup_failed"
    ]

    @model_validator(mode="after")
    def ref_is_derived(self) -> _CommandReceiptV1:
        if self.receipt_ref != _derived_ref(
            "sandbox-recheck-receipt-", self, "receipt_ref"
        ):
            raise ValueError("receipt reference mismatch")
        return self


class SandboxRecheckSnapshotV1(_StrictFrozenModel):
    revisions: tuple[SandboxRevisionRecordV1, ...]
    runs: tuple[SandboxRecheckRunV1, ...] = ()
    invalidations: tuple[SandboxInvalidationV1, ...] = ()
    receipts: tuple[_CommandReceiptV1, ...] = ()
    current_revision_ref: str = Field(pattern=_REF_PATTERN)
    review_snapshot: SandboxReviewStoreSnapshotV1

    @model_validator(mode="after")
    def snapshot_is_integral(self) -> SandboxRecheckSnapshotV1:
        if not _snapshot_is_integral(self):
            raise ValueError("recheck snapshot integrity mismatch")
        return self


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _authority_changed(
    candidate: SandboxSafetySubjectV1, current: SandboxSafetySubjectV1
) -> bool:
    return (
        candidate.formula_content_digest != current.formula_content_digest
        or candidate.profile_revision != current.profile_revision
        or candidate.profile_content_digest != current.profile_content_digest
        or candidate.graph_version != current.graph_version
        or candidate.adapter_version != current.adapter_version
        or candidate.rule_bundle_version != current.rule_bundle_version
        or candidate.rule_bundle_digest != current.rule_bundle_digest
        or candidate.evaluator_authority_digest != current.evaluator_authority_digest
        or candidate.synthetic_dataset_name != current.synthetic_dataset_name
        or candidate.synthetic_dataset_version != current.synthetic_dataset_version
        or candidate.synthetic_dataset_digest != current.synthetic_dataset_digest
        or candidate.synthetic_manifest_digest != current.synthetic_manifest_digest
    )


def _command_is_prevalidated(
    command: SandboxRevisionCommandV1,
    current: SandboxRevisionRecordV1,
) -> bool:
    candidate = command.candidate_subject
    bundle = command.rule_bundle
    return (
        candidate.test_session_id == current.test_session_id
        and candidate.formula_artifact_id == current.subject.formula_artifact_id
        and candidate.profile_artifact_id == current.subject.profile_artifact_id
        and candidate.domain_state_version == current.subject.domain_state_version + 1
        and candidate.formula_revision == current.subject.formula_revision + 1
        and _authority_changed(candidate, current.subject)
        and candidate.rule_bundle_version == bundle.rule_bundle_version
        and candidate.rule_bundle_digest == bundle.rule_bundle_digest
        and candidate.evaluator_authority_digest
        == bundle.evaluator_authority.authority_digest
        and candidate.adapter_version == bundle.adapter_version
        and command.checkpoint_id != current.checkpoint_id
        and command.interrupt_id != current.interrupt_id
    )


def _derived_ref(prefix: str, value: BaseModel, ref_field: str) -> str:
    return prefix + _digest(value.model_dump(mode="python", exclude={ref_field}))


def _deep_model[ModelT: BaseModel](model_type: type[ModelT], value: object) -> ModelT:
    return model_type.model_validate_json(canonical_json_bytes(value), strict=True)


def _build_derived[ModelT: BaseModel](
    model_type: type[ModelT],
    prefix: str,
    ref_field: str,
    values: dict[str, object],
) -> ModelT:
    return model_type.model_validate(
        {ref_field: prefix + _digest(values), **values}, strict=True
    )


def _revision_record(**values: object) -> SandboxRevisionRecordV1:
    return _build_derived(
        SandboxRevisionRecordV1,
        "sandbox-recheck-revision-",
        "revision_ref",
        values,
    )


def _run_record(**values: object) -> SandboxRecheckRunV1:
    return _build_derived(
        SandboxRecheckRunV1,
        "sandbox-recheck-run-",
        "run_ref",
        values,
    )


def _invalidation_record(**values: object) -> SandboxInvalidationV1:
    return _build_derived(
        SandboxInvalidationV1,
        "sandbox-recheck-invalidation-",
        "invalidation_ref",
        values,
    )


def _receipt_record(**values: object) -> _CommandReceiptV1:
    return _build_derived(
        _CommandReceiptV1,
        "sandbox-recheck-receipt-",
        "receipt_ref",
        values,
    )


def _historical_invalidation_authority_refs(
    snapshot: SandboxReviewStoreSnapshotV1,
    revision: SandboxRevisionRecordV1,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    subject = revision.subject
    source_refs = {
        source.source_ref
        for source in snapshot.sources
        if source.source.safety_subject == revision.subject
        and source.source.safety_result == revision.result
    }
    challenge_refs = tuple(
        challenge.challenge_ref
        for challenge in snapshot.challenges
        if challenge.test_session_id == subject.test_session_id
        and challenge.domain_state_version == subject.domain_state_version
        and challenge.formula_revision == subject.formula_revision
        and any(
            checkpoint.challenge_ref == challenge.challenge_ref
            and checkpoint.source_ref in source_refs
            for checkpoint in snapshot.checkpoints
        )
    )
    challenge_set = set(challenge_refs)
    event_refs = tuple(
        event.event_ref
        for event in snapshot.events
        if event.challenge_ref in challenge_set
    )
    return challenge_refs, event_refs


def _source_matches_same_revision(
    source: _ReviewSourceRecord,
    revision: SandboxRevisionRecordV1,
) -> bool:
    return (
        source.namespace == revision.namespace
        and source.test_session_id == revision.test_session_id
        and source.thread_id == revision.thread_id
        and source.checkpoint_id == revision.checkpoint_id
        and source.interrupt_id == revision.interrupt_id
        and source.source.safety_subject == revision.subject
    )


def _source_matches_revision_exactly(
    source: _ReviewSourceRecord,
    revision: SandboxRevisionRecordV1,
) -> bool:
    return (
        _source_matches_same_revision(source, revision)
        and source.source.safety_result == revision.result
        and source.source.explanation_result is None
    )


def _same_revision_authority_refs(
    snapshot: SandboxReviewStoreSnapshotV1,
    revision: SandboxRevisionRecordV1,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    source_refs = tuple(
        source.source_ref
        for source in snapshot.sources
        if _source_matches_same_revision(source, revision)
    )
    source_set = set(source_refs)
    challenge_refs = tuple(
        challenge.challenge_ref
        for challenge in snapshot.challenges
        if any(
            checkpoint.challenge_ref == challenge.challenge_ref
            and checkpoint.source_ref in source_set
            for checkpoint in snapshot.checkpoints
        )
    )
    challenge_set = set(challenge_refs)
    event_refs = tuple(
        event.event_ref
        for event in snapshot.events
        if event.challenge_ref in challenge_set
    )
    return source_refs, challenge_refs, event_refs


def _challenge_matches_revision(
    challenge: SandboxReviewChallengeV1,
    revision: SandboxRevisionRecordV1,
) -> bool:
    result = revision.result
    return (
        result is not None
        and revision.review_render_digest is not None
        and challenge.challenge_ref == revision.challenge_ref
        and challenge.sandbox_schema_version == revision.review_schema_version
        and challenge.namespace == revision.namespace
        and challenge.test_session_id == revision.test_session_id
        and challenge.thread_id == revision.thread_id
        and challenge.checkpoint_id == revision.checkpoint_id
        and challenge.interrupt_id == revision.interrupt_id
        and challenge.domain_state_version == revision.subject.domain_state_version
        and challenge.formula_revision == revision.subject.formula_revision
        and challenge.adapter_version == revision.subject.adapter_version
        and challenge.graph_version == revision.subject.graph_version
        and challenge.input_digest == _digest(revision.subject)
        and challenge.result_digest == result.result_digest
        and challenge.rule_bundle_digest == revision.subject.rule_bundle_digest
        and challenge.synthetic_dataset_digest
        == revision.subject.synthetic_dataset_digest
        and challenge.review_render_digest == revision.review_render_digest
    )


def _issue_projection_and_current_are_integral(
    snapshot: SandboxReviewStoreSnapshotV1,
    revisions: tuple[SandboxRevisionRecordV1, ...],
) -> bool:
    first = revisions[0]
    scope = (first.namespace, first.test_session_id, first.thread_id)

    def in_scope(record: object) -> bool:
        return (
            getattr(record, "namespace", None),
            getattr(record, "test_session_id", None),
            getattr(record, "thread_id", None),
        ) == scope

    initial_checkpoints = tuple(
        checkpoint
        for checkpoint in snapshot.checkpoints
        if in_scope(checkpoint)
        and checkpoint.challenge_ref == first.challenge_ref
        and checkpoint.checkpoint_id == first.checkpoint_id
        and checkpoint.interrupt_id == first.interrupt_id
    )
    if len(initial_checkpoints) != 1:
        return False
    initial_issue_sequence = initial_checkpoints[0].issue_sequence
    projected_challenge_refs = tuple(
        checkpoint.challenge_ref
        for checkpoint in sorted(
            (
                checkpoint
                for checkpoint in snapshot.checkpoints
                if in_scope(checkpoint)
                and checkpoint.issue_sequence >= initial_issue_sequence
            ),
            key=lambda checkpoint: checkpoint.issue_sequence,
        )
    )
    outer_challenge_refs = tuple(
        revision.challenge_ref
        for revision in revisions
        if revision.challenge_ref is not None
    )
    if projected_challenge_refs != outer_challenge_refs:
        return False

    current = revisions[-1]
    if current.status == "review_required":
        expected = current
    elif current.status in {"blocked", "recheck_failed", "review_setup_failed"}:
        if len(revisions) < 2:
            return False
        expected = revisions[-2]
    elif current.status == "modify_applied":
        expected = current
    else:
        return False
    if expected.challenge_ref is None:
        return False

    expected_checkpoints = tuple(
        checkpoint
        for checkpoint in snapshot.checkpoints
        if in_scope(checkpoint)
        and checkpoint.challenge_ref == expected.challenge_ref
        and checkpoint.checkpoint_id == expected.checkpoint_id
    )
    markers = tuple(
        marker for marker in snapshot.current_authorities if in_scope(marker)
    )
    return (
        len(expected_checkpoints) == 1
        and len(markers) == 1
        and markers[0].issue_sequence == expected_checkpoints[0].issue_sequence
        and markers[0].checkpoint_id == expected.checkpoint_id
        and markers[0].challenge_ref == expected.challenge_ref
    )


def _snapshot_is_integral(snapshot: SandboxRecheckSnapshotV1) -> bool:
    revisions = snapshot.revisions
    runs = snapshot.runs
    invalidations = snapshot.invalidations
    receipts = snapshot.receipts
    if (
        not revisions
        or snapshot.current_revision_ref != revisions[-1].revision_ref
        or tuple(revision.sequence for revision in revisions)
        != tuple(range(len(revisions)))
        or tuple(run.sequence for run in runs) != tuple(range(len(runs)))
        or tuple(item.sequence for item in invalidations)
        != tuple(range(len(invalidations)))
        or tuple(receipt.sequence for receipt in receipts)
        != tuple(range(len(receipts)))
        or len(runs) != len(revisions) - 1
        or len(invalidations) != len(runs)
        or len(receipts) != len(runs)
        or len({revision.revision_ref for revision in revisions}) != len(revisions)
        or len({run.run_ref for run in runs}) != len(runs)
        or len({item.invalidation_ref for item in invalidations})
        != len(invalidations)
        or len({receipt.receipt_ref for receipt in receipts}) != len(receipts)
        or len({receipt.command_digest for receipt in receipts}) != len(receipts)
        or len({receipt.command_id for receipt in receipts}) != len(receipts)
        or len({receipt.run_id for receipt in receipts}) != len(receipts)
        or len({receipt.trace_id for receipt in receipts}) != len(receipts)
    ):
        return False
    first = revisions[0]
    first_exact_challenges = tuple(
        challenge
        for challenge in snapshot.review_snapshot.challenges
        if challenge.challenge_ref == first.challenge_ref
    )
    first_exact_events = tuple(
        event
        for event in snapshot.review_snapshot.events
        if event.challenge_ref == first.challenge_ref
    )
    if (
        first.parent_revision_ref is not None
        or first.status != "modify_applied"
        or first.rule_bundle is not None
        or first.result is None
        or first.result.decision is not SandboxSafetyDecision.ALLOW
        or first.test_session_id != first.subject.test_session_id
        or len(first_exact_challenges) != 1
        or not _challenge_matches_revision(first_exact_challenges[0], first)
        or first_exact_challenges[0].state != "applied"
        or len(first_exact_events) != 1
        or first_exact_events[0].action is not SandboxReviewAction.MODIFY_FIXTURE
    ):
        return False
    for index, revision in enumerate(revisions[1:], start=1):
        prior = revisions[index - 1]
        run = runs[index - 1]
        invalidation = invalidations[index - 1]
        receipt = receipts[index - 1]
        if revision.rule_bundle is None:
            return False
        reconstructed_command = SandboxRevisionCommandV1(
            expected_current_revision_ref=prior.revision_ref,
            command_id=run.command_id,
            run_id=run.run_id,
            trace_id=run.trace_id,
            candidate_subject=revision.subject,
            rule_bundle=revision.rule_bundle,
            checkpoint_id=revision.checkpoint_id,
            interrupt_id=revision.interrupt_id,
        )
        if (
            revision.parent_revision_ref != prior.revision_ref
            or revision.accepted_command_digest != run.command_digest
            or run.old_revision_ref != prior.revision_ref
            or run.new_revision_ref != revision.revision_ref
            or run.status != revision.status
            or run.result_digest
            != (None if revision.result is None else revision.result.result_digest)
            or run.challenge_ref != revision.challenge_ref
            or invalidation.old_revision_ref != prior.revision_ref
            or invalidation.new_revision_ref != revision.revision_ref
            or invalidation.old_subject_digest != _digest(prior.subject)
            or invalidation.old_result_digest
            != (None if prior.result is None else prior.result.result_digest)
            or invalidation.old_explanation_digest != prior.explanation_digest
            or invalidation.old_review_render_digest != prior.review_render_digest
            or receipt.command_digest != run.command_digest
            or receipt.command_id != run.command_id
            or receipt.run_id != run.run_id
            or receipt.trace_id != run.trace_id
            or receipt.old_revision_ref != prior.revision_ref
            or receipt.new_revision_ref != revision.revision_ref
            or receipt.status != revision.status
            or run.command_digest != _digest(reconstructed_command)
            or not _command_is_prevalidated(reconstructed_command, prior)
        ):
            return False
        if (
            revision.test_session_id != revision.subject.test_session_id
            or revision.namespace != prior.namespace
            or revision.test_session_id != prior.test_session_id
            or revision.thread_id != prior.thread_id
        ):
            return False
        if revision.result is not None and (
            revision.result.decision_subject_digest != _digest(revision.subject)
            or revision.result.adapter_version != revision.subject.adapter_version
        ):
            return False
        exact_challenges = tuple(
            challenge
            for challenge in snapshot.review_snapshot.challenges
            if challenge.challenge_ref == revision.challenge_ref
        )
        exact_events = tuple(
            event
            for event in snapshot.review_snapshot.events
            if event.challenge_ref == revision.challenge_ref
        )
        if revision.status == "review_required" and (
            revision.result is None
            or revision.result.decision is not SandboxSafetyDecision.ALLOW
            or revision.challenge_ref is None
            or len(exact_challenges) != 1
            or not _challenge_matches_revision(exact_challenges[0], revision)
        ):
            return False
        if (
            revision.status == "review_required"
            and index < len(revisions) - 1
            and (
                len(exact_events) != 1
                or exact_challenges[0].state != "applied"
                or exact_events[0].action is not SandboxReviewAction.MODIFY_FIXTURE
            )
        ):
            return False
        if revision.status == "blocked" and (
            revision.result is None
            or revision.result.decision is not SandboxSafetyDecision.BLOCK
            or revision.challenge_ref is not None
        ):
            return False
        if revision.status == "recheck_failed" and (
            revision.result is not None or revision.challenge_ref is not None
        ):
            return False
        if revision.status == "review_setup_failed" and (
            revision.result is None
            or revision.result.decision is not SandboxSafetyDecision.ALLOW
            or revision.challenge_ref is not None
        ):
            return False
        if revision.status != "review_required":
            same_sources, same_challenges, same_events = (
                _same_revision_authority_refs(snapshot.review_snapshot, revision)
            )
            if (
                revision.review_schema_version != prior.review_schema_version
                or bool(same_sources)
                or bool(same_challenges)
                or bool(same_events)
            ):
                return False
        old_challenges, old_events = _historical_invalidation_authority_refs(
            snapshot.review_snapshot, prior
        )
        if (
            invalidation.old_challenge_refs != old_challenges
            or invalidation.old_event_refs != old_events
        ):
            return False
    if not _issue_projection_and_current_are_integral(
        snapshot.review_snapshot, revisions
    ):
        return False
    current = revisions[-1]
    if current.result is not None and (
        current.result.decision_subject_digest != _digest(current.subject)
        or current.result.adapter_version != current.subject.adapter_version
    ):
        return False
    if current.status == "review_required":
        if (
            current.result is None
            or current.result.decision is not SandboxSafetyDecision.ALLOW
            or current.challenge_ref is None
        ):
            return False
        sources = tuple(
            source
            for source in snapshot.review_snapshot.sources
            if _source_matches_revision_exactly(source, current)
        )
        challenges = tuple(
            challenge
            for challenge in snapshot.review_snapshot.challenges
            if challenge.challenge_ref == current.challenge_ref
        )
        if len(sources) != 1 or len(challenges) != 1:
            return False
        markers = tuple(
            marker
            for marker in snapshot.review_snapshot.current_authorities
            if marker.namespace == current.namespace
            and marker.test_session_id == current.test_session_id
            and marker.thread_id == current.thread_id
            and marker.checkpoint_id == current.checkpoint_id
            and marker.challenge_ref == current.challenge_ref
        )
        if (
            len(markers) != 1
            or challenges[0].sandbox_schema_version != current.review_schema_version
        ):
            return False
    elif current.status == "blocked":
        if (
            current.result is None
            or current.result.decision is not SandboxSafetyDecision.BLOCK
            or current.challenge_ref is not None
        ):
            return False
    elif current.status in {"recheck_failed", "review_setup_failed"}:
        if current.challenge_ref is not None:
            return False
    return True


def _snapshot_results_are_reproducible(snapshot: SandboxRecheckSnapshotV1) -> bool:
    for index, revision in enumerate(snapshot.revisions[1:]):
        if revision.result is None:
            continue
        bundle = revision.rule_bundle
        if bundle is None:
            return False
        run = snapshot.runs[index]
        try:
            reproduced = SandboxSafetyRuleAdapter().evaluate(
                revision.subject,
                bundle,
                command_id=run.command_id,
                run_id=run.run_id,
                trace_id=run.trace_id,
            )
        except Exception:
            return False
        if reproduced != revision.result:
            return False
    return True


def _initial_authority(
    snapshot: SandboxReviewStoreSnapshotV1,
) -> tuple[SandboxRevisionRecordV1, str]:
    if len(snapshot.current_authorities) != 1:
        raise SandboxRecheckError()
    marker = snapshot.current_authorities[0]
    checkpoints = tuple(
        checkpoint
        for checkpoint in snapshot.checkpoints
        if checkpoint.issue_sequence == marker.issue_sequence
        and checkpoint.challenge_ref == marker.challenge_ref
        and checkpoint.namespace == marker.namespace
        and checkpoint.test_session_id == marker.test_session_id
        and checkpoint.thread_id == marker.thread_id
        and checkpoint.checkpoint_id == marker.checkpoint_id
    )
    challenges = tuple(
        challenge
        for challenge in snapshot.challenges
        if challenge.challenge_ref == marker.challenge_ref
    )
    if len(checkpoints) != 1 or len(challenges) != 1:
        raise SandboxRecheckError()
    checkpoint = checkpoints[0]
    challenge = challenges[0]
    sources = tuple(
        source for source in snapshot.sources if source.source_ref == checkpoint.source_ref
    )
    events = tuple(
        event for event in snapshot.events if event.challenge_ref == challenge.challenge_ref
    )
    if (
        len(sources) != 1
        or len(events) != 1
        or challenge.state != "applied"
        or checkpoint.state != "review_applied"
        or events[0].action is not SandboxReviewAction.MODIFY_FIXTURE
    ):
        raise SandboxRecheckError()
    source = sources[0].source
    record = _revision_record(
        sequence=0,
        parent_revision_ref=None,
        accepted_command_digest=None,
        namespace=checkpoint.namespace,
        test_session_id=checkpoint.test_session_id,
        thread_id=checkpoint.thread_id,
        checkpoint_id=checkpoint.checkpoint_id,
        interrupt_id=checkpoint.interrupt_id,
        subject=source.safety_subject,
        rule_bundle=None,
        result=source.safety_result,
        explanation_digest=source.explanation_digest,
        review_render_digest=source.review_render_digest,
        review_schema_version=challenge.sandbox_schema_version,
        challenge_ref=challenge.challenge_ref,
        status="modify_applied",
    )
    return record, checkpoint.source_ref


class SandboxRecheckCoordinator:
    """Outer-lock coordinator for offline modification and complete re-evaluation."""

    def __init__(
        self,
        *,
        clock: Callable[[], int],
        nonce_factory: Callable[[], bytes],
        signature_verifier: SandboxSignatureVerifier,
        review_snapshot: object | None = None,
        snapshot: object | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._signature_verifier = signature_verifier
        restored: SandboxRecheckSnapshotV1 | None = None
        review_store: SandboxInMemoryReviewStore | None = None
        with self._lock:
            with suppress(Exception):
                if (review_snapshot is None) == (snapshot is None):
                    raise SandboxRecheckError()
                if snapshot is not None:
                    restored = _deep_model(SandboxRecheckSnapshotV1, snapshot)
                else:
                    review = _deep_model(SandboxReviewStoreSnapshotV1, review_snapshot)
                    initial, _ = _initial_authority(review)
                    restored = SandboxRecheckSnapshotV1(
                        revisions=(initial,),
                        current_revision_ref=initial.revision_ref,
                        review_snapshot=review,
                    )
                if not _snapshot_results_are_reproducible(restored):
                    raise SandboxRecheckError()
                review_store = SandboxInMemoryReviewStore(
                    snapshot=restored.review_snapshot
                )
            if restored is None or review_store is None:
                raise SandboxRecheckError()
            self._revisions = list(restored.revisions)
            self._runs = list(restored.runs)
            self._invalidations = list(restored.invalidations)
            self._receipts = list(restored.receipts)
            self._current_revision_ref = restored.current_revision_ref
            self._review_store = review_store
            self._review = self._new_review_coordinator(review_store)

    @property
    def current_revision_ref(self) -> str:
        with self._lock:
            return self._current_revision_ref

    def snapshot(self) -> SandboxRecheckSnapshotV1:
        with self._lock:
            value = SandboxRecheckSnapshotV1(
                revisions=tuple(self._revisions),
                runs=tuple(self._runs),
                invalidations=tuple(self._invalidations),
                receipts=tuple(self._receipts),
                current_revision_ref=self._current_revision_ref,
                review_snapshot=self._review_store.snapshot(),
            )
            return _deep_model(SandboxRecheckSnapshotV1, value)

    def apply_revision(
        self, command_input: SandboxRevisionCommandV1
    ) -> SandboxModificationResultV1:
        command: SandboxRevisionCommandV1 | None = None
        with suppress(Exception):
            command = _deep_model(SandboxRevisionCommandV1, command_input)
        if command is None:
            raise SandboxRecheckError()
        with self._lock:
            command_digest = _digest(command)
            receipt = next(
                (
                    item
                    for item in self._receipts
                    if item.command_digest == command_digest
                ),
                None,
            )
            if receipt is not None:
                return SandboxModificationResultV1(
                    status="replayed_or_conflict",
                    current_revision_ref=self._current_revision_ref,
                )
            if any(
                item.command_id == command.command_id
                or item.run_id == command.run_id
                or item.trace_id == command.trace_id
                for item in self._receipts
            ):
                return SandboxModificationResultV1(
                    status="replayed_or_conflict",
                    current_revision_ref=self._current_revision_ref,
                )
            current = self._revisions[-1]
            if command.expected_current_revision_ref != current.revision_ref:
                return SandboxModificationResultV1(
                    status="replayed_or_conflict",
                    current_revision_ref=self._current_revision_ref,
                )
            if not self._current_modify_is_applied(current) or not _command_is_prevalidated(
                command, current
            ):
                raise SandboxRecheckError()

            result: SandboxSafetyResultV1 | None = None
            status: Literal[
                "review_required", "blocked", "recheck_failed", "review_setup_failed"
            ]
            delivery: SandboxChallengeDeliveryV1 | None = None
            candidate_store: SandboxInMemoryReviewStore | None = None
            review_render_digest: str | None = None
            evaluation_failed = False
            prewrite_rejection = False
            try:
                result = SandboxSafetyRuleAdapter().evaluate(
                    command.candidate_subject,
                    command.rule_bundle,
                    command_id=command.command_id,
                    run_id=command.run_id,
                    trace_id=command.trace_id,
                )
            except SandboxSafetyAdapterError as error:
                if error.code in {
                    SandboxSafetyFailureCode.SCHEMA_INVALID,
                    SandboxSafetyFailureCode.DIGEST_MISMATCH,
                    SandboxSafetyFailureCode.LIMIT_EXCEEDED,
                    SandboxSafetyFailureCode.EVALUATOR_RESULT_INVALID,
                }:
                    prewrite_rejection = True
                else:
                    evaluation_failed = True
            except Exception:
                evaluation_failed = True

            if prewrite_rejection:
                raise SandboxRecheckError()

            if evaluation_failed:
                status = "recheck_failed"
            elif result is not None and result.decision is SandboxSafetyDecision.BLOCK:
                status = "blocked"
            elif result is not None:
                try:
                    source = SandboxReviewSourceV1.build(
                        safety_subject=command.candidate_subject,
                        safety_result=result,
                        explanation_result=None,
                    )
                    review_render_digest = source.review_render_digest
                    candidate_store = SandboxInMemoryReviewStore(
                        snapshot=self._review_store.snapshot()
                    )
                    candidate_review = self._new_review_coordinator(candidate_store)
                    delivery = candidate_review.create_single_use_challenge(
                        source,
                        namespace=current.namespace,
                        thread_id=current.thread_id,
                        checkpoint_id=command.checkpoint_id,
                        interrupt_id=command.interrupt_id,
                    )
                    if delivery.plaintext_nonce is None:
                        raise SandboxRecheckError()
                    status = "review_required"
                except Exception:
                    candidate_store = None
                    delivery = None
                    status = "review_setup_failed"
            else:
                status = "recheck_failed"

            published_review = (
                self._review_store.snapshot()
                if candidate_store is None
                else candidate_store.snapshot()
            )
            challenge_ref = None if delivery is None else delivery.challenge.challenge_ref
            revision = _revision_record(
                sequence=len(self._revisions),
                parent_revision_ref=current.revision_ref,
                accepted_command_digest=command_digest,
                namespace=current.namespace,
                test_session_id=current.test_session_id,
                thread_id=current.thread_id,
                checkpoint_id=command.checkpoint_id,
                interrupt_id=command.interrupt_id,
                subject=command.candidate_subject,
                rule_bundle=command.rule_bundle,
                result=result,
                explanation_digest=None,
                review_render_digest=review_render_digest,
                review_schema_version=(
                    current.review_schema_version
                    if delivery is None
                    else delivery.challenge.sandbox_schema_version
                ),
                challenge_ref=challenge_ref,
                status=status,
            )
            old_challenges, old_events = _historical_invalidation_authority_refs(
                published_review, current
            )
            invalidation = _invalidation_record(
                sequence=len(self._invalidations),
                old_revision_ref=current.revision_ref,
                new_revision_ref=revision.revision_ref,
                old_subject_digest=_digest(current.subject),
                old_result_digest=(
                    None if current.result is None else current.result.result_digest
                ),
                old_explanation_digest=current.explanation_digest,
                old_review_render_digest=current.review_render_digest,
                old_challenge_refs=old_challenges,
                old_event_refs=old_events,
            )
            run = _run_record(
                sequence=len(self._runs),
                command_digest=command_digest,
                command_id=command.command_id,
                run_id=command.run_id,
                trace_id=command.trace_id,
                old_revision_ref=current.revision_ref,
                new_revision_ref=revision.revision_ref,
                status=status,
                result_digest=None if result is None else result.result_digest,
                challenge_ref=challenge_ref,
            )
            receipt = _receipt_record(
                sequence=len(self._receipts),
                command_digest=command_digest,
                command_id=command.command_id,
                run_id=command.run_id,
                trace_id=command.trace_id,
                old_revision_ref=current.revision_ref,
                new_revision_ref=revision.revision_ref,
                status=status,
            )
            candidate_snapshot = SandboxRecheckSnapshotV1(
                revisions=tuple((*self._revisions, revision)),
                runs=tuple((*self._runs, run)),
                invalidations=tuple((*self._invalidations, invalidation)),
                receipts=tuple((*self._receipts, receipt)),
                current_revision_ref=revision.revision_ref,
                review_snapshot=published_review,
            )
            if candidate_store is None:
                candidate_store = SandboxInMemoryReviewStore(
                    snapshot=candidate_snapshot.review_snapshot
                )
            self._revisions.append(revision)
            self._runs.append(run)
            self._invalidations.append(invalidation)
            self._receipts.append(receipt)
            self._current_revision_ref = revision.revision_ref
            self._review_store = candidate_store
            self._review = self._new_review_coordinator(candidate_store)
            return SandboxModificationResultV1(
                status=status,
                current_revision_ref=revision.revision_ref,
                delivery=delivery,
            )

    def stage_current_review(
        self, submission_input: SandboxResumeSubmissionV1 | bytes
    ) -> SandboxRecheckStageResultV1:
        with self._lock:
            current = self._revisions[-1]
            submission: SandboxResumeSubmissionV1 | None = None
            with suppress(Exception):
                submission = (
                    SandboxResumeSubmissionV1.model_validate_json(
                        bytes(submission_input), strict=True
                    )
                    if isinstance(submission_input, bytes)
                    else _deep_model(SandboxResumeSubmissionV1, submission_input)
                )
            if (
                current.status != "review_required"
                or submission is None
                or submission.challenge.challenge_ref != current.challenge_ref
            ):
                return SandboxRecheckStageResultV1(status="resume_rejected")
            result = self._review.stage_verified_resume_attempt(submission)
            return SandboxRecheckStageResultV1(
                status=result.status,
                resume_attempt_ref=result.resume_attempt_ref,
            )

    def resume_current_review(
        self, command_input: SandboxResumeCommandV1
    ) -> SandboxRecheckResumeResultV1:
        with self._lock:
            command: SandboxResumeCommandV1 | None = None
            with suppress(Exception):
                command = _deep_model(SandboxResumeCommandV1, command_input)
            current = self._revisions[-1]
            if command is None or current.status != "review_required":
                return SandboxRecheckResumeResultV1(status="resume_rejected")
            attempts = tuple(
                attempt
                for attempt in self._review_store.snapshot().attempts
                if attempt.resume_attempt_ref == command.resume_attempt_ref
                and attempt.challenge_ref == current.challenge_ref
            )
            if len(attempts) != 1:
                return SandboxRecheckResumeResultV1(status="resume_rejected")
            result = self._review.resume(command)
            return SandboxRecheckResumeResultV1(
                status=result.status,
                command=result.command,
            )

    def completion_eligibility(
        self,
        *,
        namespace: str,
        test_session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> SandboxCompletionEligibilityV1:
        with self._lock:
            try:
                current = self._revisions[-1]
                if (
                    current.status != "review_required"
                    or current.result is None
                    or current.result.decision is not SandboxSafetyDecision.ALLOW
                    or current.challenge_ref is None
                    or (namespace, test_session_id, thread_id, checkpoint_id)
                    != (
                        current.namespace,
                        current.test_session_id,
                        current.thread_id,
                        current.checkpoint_id,
                    )
                ):
                    return SandboxCompletionEligibilityV1(status="blocked")
                snapshot = self._review_store.snapshot()
                markers = tuple(
                    marker
                    for marker in snapshot.current_authorities
                    if (
                        marker.namespace,
                        marker.test_session_id,
                        marker.thread_id,
                        marker.checkpoint_id,
                        marker.challenge_ref,
                    )
                    == (
                        namespace,
                        test_session_id,
                        thread_id,
                        checkpoint_id,
                        current.challenge_ref,
                    )
                )
                checkpoints = tuple(
                    checkpoint
                    for checkpoint in snapshot.checkpoints
                    if checkpoint.challenge_ref == current.challenge_ref
                    and checkpoint.checkpoint_id == checkpoint_id
                    and checkpoint.state == "review_applied"
                )
                challenges = tuple(
                    challenge
                    for challenge in snapshot.challenges
                    if challenge.challenge_ref == current.challenge_ref
                    and challenge.state == "applied"
                )
                sources = tuple(
                    source
                    for source in snapshot.sources
                    if _source_matches_revision_exactly(source, current)
                )
                events = tuple(
                    event
                    for event in snapshot.events
                    if event.challenge_ref == current.challenge_ref
                    and event.action is SandboxReviewAction.CONFIRM
                )
                if not (
                    len(markers)
                    == len(checkpoints)
                    == len(challenges)
                    == len(sources)
                    == len(events)
                    == 1
                ):
                    return SandboxCompletionEligibilityV1(status="blocked")
                status = self._review.eligibility(
                    namespace=namespace,
                    test_session_id=test_session_id,
                    thread_id=thread_id,
                    checkpoint_id=checkpoint_id,
                ).status
                return SandboxCompletionEligibilityV1(status=status)
            except Exception:
                return SandboxCompletionEligibilityV1(status="blocked")

    def _current_modify_is_applied(
        self, current: SandboxRevisionRecordV1
    ) -> bool:
        snapshot = self._review_store.snapshot()
        markers = tuple(
            marker
            for marker in snapshot.current_authorities
            if marker.namespace == current.namespace
            and marker.test_session_id == current.test_session_id
            and marker.thread_id == current.thread_id
            and marker.checkpoint_id == current.checkpoint_id
            and marker.challenge_ref == current.challenge_ref
        )
        challenges = tuple(
            challenge
            for challenge in snapshot.challenges
            if challenge.challenge_ref == current.challenge_ref
            and challenge.state == "applied"
            and challenge.sandbox_schema_version == current.review_schema_version
        )
        events = tuple(
            event
            for event in snapshot.events
            if event.challenge_ref == current.challenge_ref
            and event.action is SandboxReviewAction.MODIFY_FIXTURE
        )
        return len(markers) == len(challenges) == len(events) == 1

    def _new_review_coordinator(
        self, store: SandboxInMemoryReviewStore
    ) -> SandboxReviewCoordinator:
        return SandboxReviewCoordinator(
            store=store,
            clock=self._clock,
            nonce_factory=self._nonce_factory,
            signature_verifier=self._signature_verifier,
        )
