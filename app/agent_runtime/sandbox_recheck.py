"""Offline L5-4 full recheck and review invalidation reference coordinator."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from contextlib import suppress
from enum import Enum
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
    SandboxRuleBundleAuthorizer,
    SandboxSignatureVerifier,
    SandboxTestReviewEventV1,
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
    reproduce_sandbox_safety_result,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_REF_PATTERN = r"^sandbox-recheck-[a-z-]+[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_RLOCK_TYPE = type(threading.RLock())


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


class SandboxAuthorizedRecordProjectionV1(_StrictFrozenModel):
    """Narrow current-authority projection for one L6 record operation."""

    namespace: str
    test_session_id: str
    thread_id: str
    checkpoint_id: str
    revision_ref: str = Field(pattern=_REF_PATTERN)
    subject: SandboxSafetySubjectV1
    safety_result: SandboxSafetyResultV1
    review_confirm_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    )
    review_confirmed_at: int = Field(ge=0)

    @model_validator(mode="after")
    def result_is_recordable(self) -> SandboxAuthorizedRecordProjectionV1:
        if self.safety_result.decision is not SandboxSafetyDecision.ALLOW:
            raise ValueError("record projection is not allowed")
        return self


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


def _model_graph_is_exact(value: object) -> bool:
    """Reject hidden Pydantic state before canonical rebuilding can erase it."""

    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        return (
            set(value.__dict__) == set(fields)
            and value.__pydantic_extra__ is None
            and value.__pydantic_private__ is None
            and all(
                _model_graph_is_exact(getattr(value, field_name))
                for field_name in fields
            )
        )
    if isinstance(value, tuple):
        return type(value) is tuple and all(
            _model_graph_is_exact(item) for item in value
        )
    if isinstance(value, dict):
        return type(value) is dict and all(
            _model_graph_is_exact(key) and _model_graph_is_exact(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return type(value) is list and all(
            _model_graph_is_exact(item) for item in value
        )
    if isinstance(value, (set, frozenset)):
        return type(value) in {set, frozenset} and all(
            _model_graph_is_exact(item) for item in value
        )
    return isinstance(value, Enum) or type(value) in {
        bool,
        bytes,
        float,
        int,
        str,
        type(None),
    }


def _model_graphs_match(left: object, right: object) -> bool:
    """Compare exact types and values after a strict canonical round trip."""

    if isinstance(left, BaseModel) or isinstance(right, BaseModel):
        if type(left) is not type(right) or not isinstance(left, BaseModel):
            return False
        if not isinstance(right, BaseModel):
            return False
        fields = type(left).model_fields
        return (
            _model_graph_is_exact(left)
            and _model_graph_is_exact(right)
            and all(
                _model_graphs_match(
                    getattr(left, field_name),
                    getattr(right, field_name),
                )
                for field_name in fields
            )
        )
    if isinstance(left, (tuple, list)) or isinstance(right, (tuple, list)):
        return (
            type(left) is type(right)
            and isinstance(left, (tuple, list))
            and isinstance(right, (tuple, list))
            and len(left) == len(right)
            and all(
                _model_graphs_match(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            type(left) is dict
            and type(right) is dict
            and len(left) == len(right)
            and all(
                _model_graphs_match(left_key, right_key)
                and _model_graphs_match(left_value, right_value)
                for (left_key, left_value), (right_key, right_value) in zip(
                    left.items(), right.items(), strict=True
                )
            )
        )
    return type(left) is type(right) and left == right


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
        if revision.review_schema_version != prior.review_schema_version:
            return False
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
                bool(same_sources)
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
            reproduced = reproduce_sandbox_safety_result(
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
        rule_bundle_authorizer: SandboxRuleBundleAuthorizer,
        review_snapshot: object | None = None,
        snapshot: object | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._signature_verifier = signature_verifier
        self._rule_bundle_authorizer = rule_bundle_authorizer
        restored: SandboxRecheckSnapshotV1 | None = None
        review_store: SandboxInMemoryReviewStore | None = None
        recognized_bundle_digests: frozenset[str] | None = None
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
                recognized_bundle_digests = (
                    self._externally_recognized_bundle_digests(restored)
                )
                if recognized_bundle_digests is None:
                    raise SandboxRecheckError()
                review_store = SandboxInMemoryReviewStore(
                    snapshot=restored.review_snapshot,
                    signature_verifier=self._signature_verifier,
                )
            if (
                restored is None
                or review_store is None
                or recognized_bundle_digests is None
            ):
                raise SandboxRecheckError()
            self._revisions = list(restored.revisions)
            self._runs = list(restored.runs)
            self._invalidations = list(restored.invalidations)
            self._receipts = list(restored.receipts)
            self._current_revision_ref = restored.current_revision_ref
            self._review_store = review_store
            self._recognized_rule_bundle_digests = recognized_bundle_digests
            self._review = self._new_review_coordinator(review_store)

    @property
    def current_revision_ref(self) -> str:
        with self._lock:
            return self._current_revision_ref

    def snapshot(self) -> SandboxRecheckSnapshotV1:
        with self._lock:
            validated: SandboxRecheckSnapshotV1 | None = None
            with suppress(Exception):
                value = SandboxRecheckSnapshotV1(
                    revisions=tuple(self._revisions),
                    runs=tuple(self._runs),
                    invalidations=tuple(self._invalidations),
                    receipts=tuple(self._receipts),
                    current_revision_ref=self._current_revision_ref,
                    review_snapshot=self._review_store.snapshot(),
                )
                if not _model_graph_is_exact(value):
                    raise SandboxRecheckError()
                candidate = _deep_model(SandboxRecheckSnapshotV1, value)
                if (
                    not _model_graphs_match(value, candidate)
                    or not _snapshot_results_are_reproducible(candidate)
                ):
                    raise SandboxRecheckError()
                if not self._all_snapshot_bundles_are_recognized(candidate):
                    raise SandboxRecheckError()
                SandboxInMemoryReviewStore(
                    snapshot=candidate.review_snapshot,
                    signature_verifier=self._signature_verifier,
                )
                validated = candidate
            if validated is None:
                raise SandboxRecheckError()
            return validated

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
            if not self._rule_bundle_is_authorized(command.rule_bundle):
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
                evaluated = SandboxSafetyRuleAdapter().evaluate(
                    command.candidate_subject,
                    command.rule_bundle,
                    command_id=command.command_id,
                    run_id=command.run_id,
                    trace_id=command.trace_id,
                )
                result = _deep_model(SandboxSafetyResultV1, evaluated)
                reproduced = reproduce_sandbox_safety_result(
                    command.candidate_subject,
                    command.rule_bundle,
                    command_id=command.command_id,
                    run_id=command.run_id,
                    trace_id=command.trace_id,
                )
                if result != reproduced:
                    result = None
                    evaluation_failed = True
            except SandboxSafetyAdapterError as error:
                if error.code in {
                    SandboxSafetyFailureCode.SCHEMA_INVALID,
                    SandboxSafetyFailureCode.DIGEST_MISMATCH,
                    SandboxSafetyFailureCode.LIMIT_EXCEEDED,
                    SandboxSafetyFailureCode.EVALUATOR_RESULT_INVALID,
                    SandboxSafetyFailureCode.PROHIBITED_IDENTIFIER,
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
                        safety_rule_bundle=command.rule_bundle,
                        safety_result=result,
                        safety_command_id=command.command_id,
                        safety_run_id=command.run_id,
                        safety_trace_id=command.trace_id,
                        explanation_result=None,
                    )
                    review_render_digest = source.review_render_digest
                    candidate_store = SandboxInMemoryReviewStore(
                        snapshot=self._review_store.snapshot(),
                        signature_verifier=self._signature_verifier,
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
            candidate_recognized_digests = frozenset(
                (
                    *self._recognized_rule_bundle_digests,
                    command.rule_bundle.rule_bundle_digest,
                )
            )
            if (
                not _snapshot_results_are_reproducible(candidate_snapshot)
                or not self._snapshot_bundle_digests(candidate_snapshot)
                <= candidate_recognized_digests
            ):
                raise SandboxRecheckError()
            candidate_store = SandboxInMemoryReviewStore(
                snapshot=candidate_snapshot.review_snapshot,
                signature_verifier=self._signature_verifier,
            )
            candidate_review = self._new_review_coordinator(
                candidate_store,
                recognized_rule_bundle_digests=candidate_recognized_digests,
            )
            self._revisions.append(revision)
            self._runs.append(run)
            self._invalidations.append(invalidation)
            self._receipts.append(receipt)
            self._current_revision_ref = revision.revision_ref
            self._review_store = candidate_store
            self._recognized_rule_bundle_digests = candidate_recognized_digests
            self._review = candidate_review
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

    def _completion_authority(
        self,
        *,
        expected_scope: tuple[str, str, str, str] | None,
    ) -> tuple[SandboxRevisionRecordV1, SandboxTestReviewEventV1] | None:
        before = SandboxRecheckCoordinator._validated_authority_snapshot(self)
        if before is None:
            return None
        current = before.revisions[-1]
        current_scope = (
            current.namespace,
            current.test_session_id,
            current.thread_id,
            current.checkpoint_id,
        )
        if expected_scope is not None and (
            type(expected_scope) is not tuple
            or len(expected_scope) != 4
            or any(type(value) is not str for value in expected_scope)
            or expected_scope != current_scope
        ):
            return None
        namespace, test_session_id, thread_id, checkpoint_id = current_scope
        if (
            current.status != "review_required"
            or current.result is None
            or current.result.decision is not SandboxSafetyDecision.ALLOW
            or current.challenge_ref is None
        ):
            return None
        snapshot = before.review_snapshot
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
            return None
        source_bundle = sources[0].source.safety_rule_bundle
        if current.rule_bundle is None or source_bundle != current.rule_bundle:
            return None
        bindings = SandboxRecheckCoordinator._authority_bindings(self)
        if bindings is None:
            return None
        authorizer = self._rule_bundle_authorizer
        try:
            authorized = authorizer.authorize(
                rule_bundle=_deep_model(SandboxRuleBundleV1, source_bundle)
            )
        except Exception:
            return None
        if authorized is not True:
            return None
        after_authorize_bindings = SandboxRecheckCoordinator._authority_bindings(
            self
        )
        if not SandboxRecheckCoordinator._same_authority_bindings(
            bindings,
            after_authorize_bindings,
        ):
            return None
        after = SandboxRecheckCoordinator._validated_authority_snapshot(self)
        if after is None or after != before:
            return None
        return current, events[0]

    def _authority_bindings(self) -> tuple[object, ...] | None:
        """Retain every mutable container and collaborator by object identity."""

        try:
            if (
                type(self) is not SandboxRecheckCoordinator
                or type(self._review_store) is not SandboxInMemoryReviewStore
                or type(self._review) is not SandboxReviewCoordinator
            ):
                return None
            store = self._review_store
            review = self._review
            return (
                self._lock,
                self._clock,
                self._nonce_factory,
                self._signature_verifier,
                self._rule_bundle_authorizer,
                self._revisions,
                self._runs,
                self._invalidations,
                self._receipts,
                self._recognized_rule_bundle_digests,
                store,
                store._lock,
                store._sources,
                store._challenges,
                store._checkpoints,
                store._attempts,
                store._events,
                store._transitions,
                store._current_authorities,
                review,
                review._store,
                review._clock,
                review._nonce_factory,
                review._signature_verifier,
                review._rule_bundle_authorizer,
                review._recognition_lock,
                review._recognized_rule_bundle_digests,
            )
        except Exception:
            return None

    @staticmethod
    def _same_authority_bindings(
        before: tuple[object, ...] | None,
        after: tuple[object, ...] | None,
    ) -> bool:
        return (
            before is not None
            and after is not None
            and len(before) == len(after)
            and all(left is right for left, right in zip(before, after, strict=True))
        )

    def _validated_authority_snapshot(self) -> SandboxRecheckSnapshotV1 | None:
        """Verify callbacks, then finish on a callback-free state capture."""

        try:
            if type(self._recognized_rule_bundle_digests) is not frozenset:
                return None
            recognized = self._recognized_rule_bundle_digests
            signature_verifier = self._signature_verifier
            bindings = SandboxRecheckCoordinator._authority_bindings(self)
            if bindings is None:
                return None
            before = SandboxRecheckCoordinator._capture_authority_snapshot(
                self,
                recognized=recognized,
            )
            if before is None:
                return None
            SandboxInMemoryReviewStore(
                snapshot=before.review_snapshot,
                signature_verifier=signature_verifier,
            )
            after_verify_bindings = SandboxRecheckCoordinator._authority_bindings(
                self
            )
            if not SandboxRecheckCoordinator._same_authority_bindings(
                bindings,
                after_verify_bindings,
            ):
                return None
            after = SandboxRecheckCoordinator._capture_authority_snapshot(
                self,
                recognized=recognized,
            )
            if after is None or after != before:
                return None
            final_bindings = SandboxRecheckCoordinator._authority_bindings(self)
            if not SandboxRecheckCoordinator._same_authority_bindings(
                after_verify_bindings,
                final_bindings,
            ):
                return None
            return after
        except Exception:
            return None

    def _capture_authority_snapshot(
        self,
        *,
        recognized: frozenset[str],
    ) -> SandboxRecheckSnapshotV1 | None:
        """Capture and validate state without calling external collaborators."""

        try:
            if (
                type(self) is not SandboxRecheckCoordinator
                or type(self._lock) is not _RLOCK_TYPE
                or type(self._review_store) is not SandboxInMemoryReviewStore
                or type(self._review) is not SandboxReviewCoordinator
                or type(self._revisions) is not list
                or type(self._runs) is not list
                or type(self._invalidations) is not list
                or type(self._receipts) is not list
                or any(
                    type(item) is not SandboxRevisionRecordV1
                    for item in self._revisions
                )
                or any(
                    type(item) is not SandboxRecheckRunV1
                    for item in self._runs
                )
                or any(
                    type(item) is not SandboxInvalidationV1
                    for item in self._invalidations
                )
                or any(
                    type(item) is not _CommandReceiptV1
                    for item in self._receipts
                )
                or self._review._store is not self._review_store
                or self._review._rule_bundle_authorizer
                is not self._rule_bundle_authorizer
                or self._review._signature_verifier
                is not self._signature_verifier
                or type(self._review._recognition_lock) is not _RLOCK_TYPE
                or type(self._review._recognized_rule_bundle_digests)
                is not frozenset
                or self._review._recognized_rule_bundle_digests != recognized
            ):
                return None
            review_snapshot = SandboxInMemoryReviewStore._sealed_snapshot(
                self._review_store
            )
            value = SandboxRecheckSnapshotV1(
                revisions=tuple(self._revisions),
                runs=tuple(self._runs),
                invalidations=tuple(self._invalidations),
                receipts=tuple(self._receipts),
                current_revision_ref=self._current_revision_ref,
                review_snapshot=review_snapshot,
            )
            if not _model_graph_is_exact(value):
                return None
            candidate = _deep_model(SandboxRecheckSnapshotV1, value)
            if (
                not _model_graphs_match(value, candidate)
                or not _snapshot_results_are_reproducible(candidate)
                or not SandboxRecheckCoordinator._snapshot_bundle_digests(
                    candidate
                )
                <= recognized
            ):
                return None
            return candidate
        except Exception:
            return None

    def completion_eligibility(
        self,
        *,
        namespace: str,
        test_session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> SandboxCompletionEligibilityV1:
        if (
            type(self) is not SandboxRecheckCoordinator
            or type(self._lock) is not _RLOCK_TYPE
            or any(
                type(value) is not str
                for value in (
                    namespace,
                    test_session_id,
                    thread_id,
                    checkpoint_id,
                )
            )
        ):
            return SandboxCompletionEligibilityV1(status="blocked")
        with self._lock:
            try:
                authority = SandboxRecheckCoordinator._completion_authority(
                    self,
                    expected_scope=(
                        namespace,
                        test_session_id,
                        thread_id,
                        checkpoint_id,
                    ),
                )
                return SandboxCompletionEligibilityV1(
                    status="eligible" if authority is not None else "blocked"
                )
            except Exception:
                return SandboxCompletionEligibilityV1(status="blocked")

    def authorized_record_projection(self) -> SandboxAuthorizedRecordProjectionV1:
        """Linearize one L6 operation at the live bundle authorization read."""

        if (
            type(self) is not SandboxRecheckCoordinator
            or type(self._lock) is not _RLOCK_TYPE
        ):
            raise SandboxRecheckError()
        with self._lock:
            try:
                authority = SandboxRecheckCoordinator._completion_authority(
                    self,
                    expected_scope=None,
                )
                if authority is None:
                    raise SandboxRecheckError()
                revision, event = authority
                if revision.result is None:
                    raise SandboxRecheckError()
                return _deep_model(
                    SandboxAuthorizedRecordProjectionV1,
                    SandboxAuthorizedRecordProjectionV1(
                        namespace=revision.namespace,
                        test_session_id=revision.test_session_id,
                        thread_id=revision.thread_id,
                        checkpoint_id=revision.checkpoint_id,
                        revision_ref=revision.revision_ref,
                        subject=revision.subject,
                        safety_result=revision.result,
                        review_confirm_ref=event.resume_attempt_ref,
                        review_confirmed_at=event.applied_at,
                    ),
                )
            except Exception:
                raise SandboxRecheckError() from None

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

    def _rule_bundle_is_authorized(
        self,
        rule_bundle: SandboxRuleBundleV1,
    ) -> bool:
        try:
            candidate = _deep_model(SandboxRuleBundleV1, rule_bundle)
            return (
                self._rule_bundle_authorizer.recognize(
                    rule_bundle=candidate
                )
                is True
                and self._rule_bundle_authorizer.authorize(
                    rule_bundle=candidate
                )
                is True
            )
        except Exception:
            return False

    @staticmethod
    def _snapshot_bundles(
        snapshot: SandboxRecheckSnapshotV1,
    ) -> tuple[SandboxRuleBundleV1, ...]:
        return (
            *(
                source.source.safety_rule_bundle
                for source in snapshot.review_snapshot.sources
            ),
            *(
                revision.rule_bundle
                for revision in snapshot.revisions
                if revision.rule_bundle is not None
            ),
        )

    @classmethod
    def _snapshot_bundle_digests(
        cls,
        snapshot: SandboxRecheckSnapshotV1,
    ) -> frozenset[str]:
        bundles = cls._snapshot_bundles(snapshot)
        if not bundles:
            raise SandboxRecheckError()
        return frozenset(
            _deep_model(SandboxRuleBundleV1, bundle).rule_bundle_digest
            for bundle in bundles
        )

    def _externally_recognized_bundle_digests(
        self,
        snapshot: SandboxRecheckSnapshotV1,
    ) -> frozenset[str] | None:
        try:
            bundles = self._snapshot_bundles(snapshot)
            if not bundles:
                return None
            recognized: set[str] = set()
            for bundle in bundles:
                candidate = _deep_model(SandboxRuleBundleV1, bundle)
                if (
                    self._rule_bundle_authorizer.recognize(
                        rule_bundle=candidate
                    )
                    is not True
                ):
                    return None
                recognized.add(candidate.rule_bundle_digest)
            return frozenset(recognized)
        except Exception:
            return None

    def _all_snapshot_bundles_are_recognized(
        self,
        snapshot: SandboxRecheckSnapshotV1,
    ) -> bool:
        try:
            return (
                self._snapshot_bundle_digests(snapshot)
                <= self._recognized_rule_bundle_digests
            )
        except Exception:
            return False

    def _new_review_coordinator(
        self,
        store: SandboxInMemoryReviewStore,
        *,
        recognized_rule_bundle_digests: frozenset[str] | None = None,
    ) -> SandboxReviewCoordinator:
        recognized = (
            self._recognized_rule_bundle_digests
            if recognized_rule_bundle_digests is None
            else recognized_rule_bundle_digests
        )
        return SandboxReviewCoordinator(
            store=store,
            clock=self._clock,
            nonce_factory=self._nonce_factory,
            signature_verifier=self._signature_verifier,
            rule_bundle_authorizer=self._rule_bundle_authorizer,
            _recognized_rule_bundle_digests=recognized,
        )
