"""Offline, sandbox-only reviewer interrupt and resume state machine."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_runtime.sandbox_explanation import (
    SandboxExplanationResultV1,
    canonical_explanation_bytes,
)
from app.agent_runtime.sandbox_safety import (
    SandboxSafetyDecision,
    SandboxSafetyResultV1,
    SandboxSafetySubjectV1,
    canonical_json_bytes,
    canonical_result_bytes,
)

MAX_RESUME_SUBMISSION_BYTES = 65_536
_CHALLENGE_TTL_SECONDS = 900
_REVIEW_SCHEMA_VERSION = "sandbox-review-challenge.v1"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        ser_json_bytes="hex",
        val_json_bytes="hex",
    )


class SandboxReviewError(ValueError):
    """A fixed, payload-free, chainless review-boundary failure."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("SANDBOX_REVIEW_REJECTED")


class SandboxReviewAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    MODIFY_FIXTURE = "modify_fixture"


_ALLOWED_ACTIONS = (
    SandboxReviewAction.CONFIRM,
    SandboxReviewAction.REJECT,
    SandboxReviewAction.MODIFY_FIXTURE,
)


def canonical_review_bytes(value: object) -> bytes:
    """Encode a review value as complete, stable canonical JSON."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_review_bytes(value)).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SandboxReviewSourceV1(_StrictFrozenModel):
    """Accepted L5-1/L5-2 authority and its canonical bindings."""

    safety_subject: SandboxSafetySubjectV1
    safety_result: SandboxSafetyResultV1
    explanation_result: SandboxExplanationResultV1 | None
    input_digest: str = Field(pattern=_DIGEST_PATTERN)
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    explanation_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    review_render_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def authority_is_bound(self) -> SandboxReviewSourceV1:
        input_digest = _bytes_sha256(canonical_json_bytes(self.safety_subject))
        result_canonical_digest = _bytes_sha256(
            canonical_result_bytes(self.safety_result)
        )
        explanation_digest = (
            None
            if self.explanation_result is None
            else _bytes_sha256(
                canonical_explanation_bytes(self.explanation_result)
            )
        )
        expected_render_digest = _review_render_digest(
            input_digest=input_digest,
            result_digest=self.safety_result.result_digest,
            result_canonical_digest=result_canonical_digest,
            explanation_digest=explanation_digest,
        )
        if (
            input_digest != self.input_digest
            or input_digest != self.safety_result.decision_subject_digest
            or self.safety_subject.adapter_version
            != self.safety_result.adapter_version
            or self.result_digest != self.safety_result.result_digest
            or self.explanation_digest != explanation_digest
            or self.review_render_digest != expected_render_digest
        ):
            raise ValueError("review source binding mismatch")
        if (
            self.explanation_result is not None
            and self.explanation_result.source_result_digest
            != self.safety_result.result_digest
        ):
            raise ValueError("review explanation binding mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        safety_subject: SandboxSafetySubjectV1,
        safety_result: SandboxSafetyResultV1,
        explanation_result: SandboxExplanationResultV1 | None = None,
    ) -> SandboxReviewSourceV1:
        subject = _deep_model(SandboxSafetySubjectV1, safety_subject)
        result = _deep_model(SandboxSafetyResultV1, safety_result)
        explanation = (
            None
            if explanation_result is None
            else _deep_model(SandboxExplanationResultV1, explanation_result)
        )
        input_digest = _bytes_sha256(canonical_json_bytes(subject))
        result_canonical_digest = _bytes_sha256(canonical_result_bytes(result))
        explanation_digest = (
            None
            if explanation is None
            else _bytes_sha256(canonical_explanation_bytes(explanation))
        )
        return cls(
            safety_subject=subject,
            safety_result=result,
            explanation_result=explanation,
            input_digest=input_digest,
            result_digest=result.result_digest,
            explanation_digest=explanation_digest,
            review_render_digest=_review_render_digest(
                input_digest=input_digest,
                result_digest=result.result_digest,
                result_canonical_digest=result_canonical_digest,
                explanation_digest=explanation_digest,
            ),
        )


def _review_render_digest(
    *,
    input_digest: str,
    result_digest: str,
    result_canonical_digest: str,
    explanation_digest: str | None,
) -> str:
    return _sha256(
        {
            "explanation_digest": explanation_digest,
            "input_digest": input_digest,
            "result_canonical_digest": result_canonical_digest,
            "result_digest": result_digest,
            "summary": "synthetic_safety_review_pending",
        }
    )


class SandboxReviewChallengeV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=64)
    graph_version: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    namespace: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    test_session_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    thread_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    checkpoint_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    interrupt_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    domain_state_version: int = Field(ge=1)
    formula_revision: int = Field(ge=1)
    input_digest: str = Field(pattern=_DIGEST_PATTERN)
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    rule_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    synthetic_dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    review_render_digest: str = Field(pattern=_DIGEST_PATTERN)
    allowed_actions: tuple[SandboxReviewAction, ...]
    synthetic_technical_summary: Literal["synthetic_safety_review_pending"]
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    nonce_digest: str = Field(pattern=_DIGEST_PATTERN)
    challenge_ref: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    state: Literal["issued", "expired", "claimed", "applied"]

    @model_validator(mode="after")
    def challenge_is_canonical(self) -> SandboxReviewChallengeV1:
        if self.allowed_actions != _ALLOWED_ACTIONS:
            raise ValueError("allowed actions mismatch")
        if self.expires_at != self.issued_at + _CHALLENGE_TTL_SECONDS:
            raise ValueError("challenge expiry mismatch")
        if self.challenge_ref != _challenge_ref(self):
            raise ValueError("challenge reference mismatch")
        return self


def _challenge_authority(challenge: SandboxReviewChallengeV1) -> dict[str, object]:
    return {
        "adapter_version": challenge.adapter_version,
        "allowed_actions": challenge.allowed_actions,
        "checkpoint_id": challenge.checkpoint_id,
        "domain_state_version": challenge.domain_state_version,
        "expires_at": challenge.expires_at,
        "formula_revision": challenge.formula_revision,
        "graph_version": challenge.graph_version,
        "input_digest": challenge.input_digest,
        "interrupt_id": challenge.interrupt_id,
        "issued_at": challenge.issued_at,
        "namespace": challenge.namespace,
        "nonce_digest": challenge.nonce_digest,
        "result_digest": challenge.result_digest,
        "review_render_digest": challenge.review_render_digest,
        "rule_bundle_digest": challenge.rule_bundle_digest,
        "sandbox_schema_version": challenge.sandbox_schema_version,
        "synthetic_dataset_digest": challenge.synthetic_dataset_digest,
        "synthetic_technical_summary": challenge.synthetic_technical_summary,
        "test_session_id": challenge.test_session_id,
        "thread_id": challenge.thread_id,
    }


def _challenge_ref(challenge: SandboxReviewChallengeV1) -> str:
    return f"sandbox-challenge-{_sha256(_challenge_authority(challenge))}"


class SandboxChallengeDeliveryV1(_StrictFrozenModel):
    challenge: SandboxReviewChallengeV1
    plaintext_nonce: bytes | None = Field(default=None, repr=False)


class SandboxTestReviewProofV1(_StrictFrozenModel):
    sandbox_test_reviewer_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    sandbox_test_role: Literal["sandbox_reviewer_test_role"]
    sandbox_test_organization_label: Literal["local_synthetic_sandbox"]
    sandbox_test_qualification_label: Literal["not_a_medical_credential"]
    sandbox_test_signature_scheme: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    sandbox_test_key_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    sandbox_test_signed_payload_digest: str = Field(pattern=_DIGEST_PATTERN)
    sandbox_test_signature: str = Field(min_length=1, repr=False)


class SandboxResumeSubmissionV1(_StrictFrozenModel):
    namespace: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    test_session_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )
    challenge: SandboxReviewChallengeV1
    action: SandboxReviewAction
    plaintext_nonce: bytes = Field(repr=False)
    proof: SandboxTestReviewProofV1 = Field(repr=False)


class SandboxResumeCommandV1(_StrictFrozenModel):
    resume_attempt_ref: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )


def review_signed_payload_digest(
    *,
    challenge: SandboxReviewChallengeV1,
    action: SandboxReviewAction,
    plaintext_nonce: bytes,
    sandbox_test_reviewer_id: str,
    sandbox_test_role: str,
    sandbox_test_organization_label: str,
    sandbox_test_qualification_label: str,
    sandbox_test_signature_scheme: str,
    sandbox_test_key_id: str,
) -> str:
    """Bind the signature to every challenge, action, identity, and nonce field."""

    return _sha256(
        {
            "action": action,
            "challenge": challenge,
            "plaintext_nonce": plaintext_nonce.hex(),
            "sandbox_test_key_id": sandbox_test_key_id,
            "sandbox_test_organization_label": sandbox_test_organization_label,
            "sandbox_test_qualification_label": sandbox_test_qualification_label,
            "sandbox_test_reviewer_id": sandbox_test_reviewer_id,
            "sandbox_test_role": sandbox_test_role,
            "sandbox_test_signature_scheme": sandbox_test_signature_scheme,
        }
    )


class _StoredSourceV1(_StrictFrozenModel):
    source_ref: str = Field(pattern=r"^sandbox-source-[0-9a-f]{64}$")
    issue_sequence: int = Field(ge=0)
    namespace: str
    test_session_id: str
    thread_id: str
    checkpoint_id: str
    interrupt_id: str
    source: SandboxReviewSourceV1

    @model_validator(mode="after")
    def source_ref_is_derived(self) -> _StoredSourceV1:
        if self.source_ref != _stored_source_ref(self):
            raise ValueError("stored source reference mismatch")
        return self


class _CheckpointV1(_StrictFrozenModel):
    issue_sequence: int = Field(ge=0)
    namespace: str
    test_session_id: str
    thread_id: str
    checkpoint_id: str
    interrupt_id: str
    challenge_ref: str
    source_ref: str
    state: Literal["review_pending", "review_applied"]


class _SealedAttemptV1(_StrictFrozenModel):
    resume_attempt_ref: str
    challenge_ref: str
    source_ref: str
    namespace: str
    test_session_id: str
    action: SandboxReviewAction
    sandbox_test_reviewer_id: str
    sandbox_test_role: Literal["sandbox_reviewer_test_role"]
    sandbox_test_organization_label: Literal["local_synthetic_sandbox"]
    sandbox_test_qualification_label: Literal["not_a_medical_credential"]
    sandbox_test_signature_scheme: str
    sandbox_test_key_id: str
    sandbox_test_signed_payload_digest: str = Field(pattern=_DIGEST_PATTERN)
    state: Literal["sealed", "applied"]

    @model_validator(mode="after")
    def attempt_ref_is_derived(self) -> _SealedAttemptV1:
        if self.resume_attempt_ref != _attempt_ref(self):
            raise ValueError("resume attempt reference mismatch")
        return self


class SandboxTestReviewEventV1(_StrictFrozenModel):
    event_ref: str
    sequence: int = Field(ge=0)
    resume_attempt_ref: str
    challenge_ref: str
    sandbox_schema_version: str
    adapter_version: str
    graph_version: str
    namespace: str
    test_session_id: str
    thread_id: str
    checkpoint_id: str
    interrupt_id: str
    domain_state_version: int
    formula_revision: int
    input_digest: str = Field(pattern=_DIGEST_PATTERN)
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    rule_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    synthetic_dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    review_render_digest: str = Field(pattern=_DIGEST_PATTERN)
    action: SandboxReviewAction
    sandbox_test_reviewer_id: str
    sandbox_test_role: Literal["sandbox_reviewer_test_role"]
    sandbox_test_organization_label: Literal["local_synthetic_sandbox"]
    sandbox_test_qualification_label: Literal["not_a_medical_credential"]
    sandbox_test_signature_scheme: str
    sandbox_test_key_id: str
    sandbox_test_signed_payload_digest: str = Field(pattern=_DIGEST_PATTERN)
    applied_at: int

    @model_validator(mode="after")
    def event_ref_is_derived(self) -> SandboxTestReviewEventV1:
        if self.event_ref != _event_ref(self):
            raise ValueError("review event reference mismatch")
        return self


class _TransitionV1(_StrictFrozenModel):
    transition_ref: str
    sequence: int = Field(ge=0)
    challenge_ref: str
    resume_attempt_ref: str | None
    from_state: str
    to_state: str
    observed_at: int

    @model_validator(mode="after")
    def transition_ref_is_derived(self) -> _TransitionV1:
        if self.transition_ref != _transition_ref(self):
            raise ValueError("transition reference mismatch")
        return self


class _CurrentAuthorityV1(_StrictFrozenModel):
    issue_sequence: int = Field(ge=0)
    namespace: str
    test_session_id: str
    thread_id: str
    checkpoint_id: str
    challenge_ref: str


class SandboxReviewStoreSnapshotV1(_StrictFrozenModel):
    sources: tuple[_StoredSourceV1, ...] = ()
    challenges: tuple[SandboxReviewChallengeV1, ...] = ()
    checkpoints: tuple[_CheckpointV1, ...] = ()
    attempts: tuple[_SealedAttemptV1, ...] = ()
    events: tuple[SandboxTestReviewEventV1, ...] = ()
    transitions: tuple[_TransitionV1, ...] = ()
    current_authorities: tuple[_CurrentAuthorityV1, ...] = ()

    @model_validator(mode="after")
    def records_are_complete_and_consistent(self) -> SandboxReviewStoreSnapshotV1:
        if not _snapshot_is_integral(self):
            raise ValueError("review store snapshot integrity mismatch")
        return self


class _StageResultV1(_StrictFrozenModel):
    status: Literal["staged", "resume_rejected"]
    resume_attempt_ref: str | None = None


class _ResumeResultV1(_StrictFrozenModel):
    status: Literal["applied", "replayed_or_conflict", "resume_rejected"]
    command: SandboxResumeCommandV1 | None = None


class _EligibilityV1(_StrictFrozenModel):
    status: Literal["eligible", "blocked"]


class SandboxSignatureVerifier(Protocol):
    def verify(
        self,
        *,
        signed_payload_digest: str,
        sandbox_test_signature_scheme: str,
        sandbox_test_key_id: str,
        sandbox_test_signature: str,
    ) -> bool: ...


def _deep_model[ModelT: BaseModel](
    model_type: type[ModelT], value: object
) -> ModelT:
    return model_type.model_validate_json(canonical_review_bytes(value), strict=True)


def _fixed_stage_rejection() -> _StageResultV1:
    return _StageResultV1(status="resume_rejected")


def _fixed_resume_rejection() -> _ResumeResultV1:
    return _ResumeResultV1(status="resume_rejected")


def _stored_source_ref(source: _StoredSourceV1) -> str:
    body = source.model_dump(mode="python", exclude={"source_ref"})
    return f"sandbox-source-{_sha256(body)}"


def _attempt_ref(attempt: _SealedAttemptV1) -> str:
    body = attempt.model_dump(
        mode="python",
        exclude={"resume_attempt_ref", "state"},
    )
    return f"sandbox-attempt-{_sha256(body)}"


def _event_ref(event: SandboxTestReviewEventV1) -> str:
    body = event.model_dump(mode="python", exclude={"event_ref"})
    return f"sandbox-review-event-{_sha256(body)}"


def _transition_ref(transition: _TransitionV1) -> str:
    body = transition.model_dump(mode="python", exclude={"transition_ref"})
    return f"sandbox-transition-{_sha256(body)}"


def _values_are_unique(values: tuple[str, ...]) -> bool:
    return len(values) == len(set(values))


def _scope_key(
    value: _CheckpointV1 | _CurrentAuthorityV1,
) -> tuple[str, str, str]:
    return (value.namespace, value.test_session_id, value.thread_id)


def _event_matches_authority(
    event: SandboxTestReviewEventV1,
    challenge: SandboxReviewChallengeV1,
    attempt: _SealedAttemptV1,
) -> bool:
    return (
        event.resume_attempt_ref == attempt.resume_attempt_ref
        and event.challenge_ref == challenge.challenge_ref
        and event.sandbox_schema_version == challenge.sandbox_schema_version
        and event.adapter_version == challenge.adapter_version
        and event.graph_version == challenge.graph_version
        and event.namespace == challenge.namespace
        and event.test_session_id == challenge.test_session_id
        and event.thread_id == challenge.thread_id
        and event.checkpoint_id == challenge.checkpoint_id
        and event.interrupt_id == challenge.interrupt_id
        and event.domain_state_version == challenge.domain_state_version
        and event.formula_revision == challenge.formula_revision
        and event.input_digest == challenge.input_digest
        and event.result_digest == challenge.result_digest
        and event.rule_bundle_digest == challenge.rule_bundle_digest
        and event.synthetic_dataset_digest == challenge.synthetic_dataset_digest
        and event.review_render_digest == challenge.review_render_digest
        and event.action is attempt.action
        and event.sandbox_test_reviewer_id == attempt.sandbox_test_reviewer_id
        and event.sandbox_test_role == attempt.sandbox_test_role
        and event.sandbox_test_organization_label
        == attempt.sandbox_test_organization_label
        and event.sandbox_test_qualification_label
        == attempt.sandbox_test_qualification_label
        and event.sandbox_test_signature_scheme
        == attempt.sandbox_test_signature_scheme
        and event.sandbox_test_key_id == attempt.sandbox_test_key_id
        and event.sandbox_test_signed_payload_digest
        == attempt.sandbox_test_signed_payload_digest
    )


def _causal_observation_is_reachable(
    challenge: SandboxReviewChallengeV1,
    *,
    predecessor_at: int,
    observed_at: int,
) -> bool:
    """Apply the same per-attempt causal time window live and on restore."""

    return (
        challenge.issued_at
        <= predecessor_at
        <= observed_at
        < challenge.expires_at
    )


def _snapshot_is_integral(snapshot: SandboxReviewStoreSnapshotV1) -> bool:
    sources = snapshot.sources
    challenges = snapshot.challenges
    checkpoints = snapshot.checkpoints
    attempts = snapshot.attempts
    events = snapshot.events
    transitions = snapshot.transitions
    current = snapshot.current_authorities

    if not (
        len(sources) == len(challenges) == len(checkpoints)
        and tuple(source.issue_sequence for source in sources)
        == tuple(range(len(sources)))
        and tuple(checkpoint.issue_sequence for checkpoint in checkpoints)
        == tuple(range(len(checkpoints)))
        and tuple(event.sequence for event in events) == tuple(range(len(events)))
        and tuple(transition.sequence for transition in transitions)
        == tuple(range(len(transitions)))
        and _values_are_unique(tuple(source.source_ref for source in sources))
        and _values_are_unique(
            tuple(challenge.challenge_ref for challenge in challenges)
        )
        and _values_are_unique(
            tuple(attempt.resume_attempt_ref for attempt in attempts)
        )
        and _values_are_unique(tuple(event.event_ref for event in events))
        and _values_are_unique(
            tuple(transition.transition_ref for transition in transitions)
        )
    ):
        return False

    checkpoint_keys = tuple(
        (
            checkpoint.namespace,
            checkpoint.test_session_id,
            checkpoint.thread_id,
            checkpoint.checkpoint_id,
            checkpoint.interrupt_id,
        )
        for checkpoint in checkpoints
    )
    if len(checkpoint_keys) != len(set(checkpoint_keys)):
        return False

    for sequence, (source, challenge, checkpoint) in enumerate(
        zip(sources, challenges, checkpoints, strict=True)
    ):
        if (
            source.issue_sequence != sequence
            or checkpoint.issue_sequence != sequence
            or checkpoint.challenge_ref != challenge.challenge_ref
            or checkpoint.source_ref != source.source_ref
            or not (
                source.namespace == challenge.namespace == checkpoint.namespace
            )
            or not (
                source.test_session_id
                == challenge.test_session_id
                == checkpoint.test_session_id
            )
            or not (
                source.thread_id == challenge.thread_id == checkpoint.thread_id
            )
            or not (
                source.checkpoint_id
                == challenge.checkpoint_id
                == checkpoint.checkpoint_id
            )
            or not (
                source.interrupt_id
                == challenge.interrupt_id
                == checkpoint.interrupt_id
            )
            or not _challenge_matches_source(challenge, source.source)
            or (challenge.state == "applied")
            != (checkpoint.state == "review_applied")
            or challenge.state == "claimed"
        ):
            return False

    source_by_ref = {source.source_ref: source for source in sources}
    challenge_by_ref = {
        challenge.challenge_ref: challenge for challenge in challenges
    }
    checkpoint_by_challenge = {
        checkpoint.challenge_ref: checkpoint for checkpoint in checkpoints
    }
    attempt_by_ref = {
        attempt.resume_attempt_ref: attempt for attempt in attempts
    }

    for attempt in attempts:
        attempt_challenge = challenge_by_ref.get(attempt.challenge_ref)
        attempt_source = source_by_ref.get(attempt.source_ref)
        attempt_checkpoint = checkpoint_by_challenge.get(attempt.challenge_ref)
        if (
            attempt_challenge is None
            or attempt_source is None
            or attempt_checkpoint is None
            or attempt_checkpoint.source_ref != attempt.source_ref
            or attempt.namespace != attempt_challenge.namespace
            or attempt.test_session_id != attempt_challenge.test_session_id
        ):
            return False

    events_by_attempt: dict[str, list[SandboxTestReviewEventV1]] = {}
    for event in events:
        event_attempt = attempt_by_ref.get(event.resume_attempt_ref)
        event_challenge = challenge_by_ref.get(event.challenge_ref)
        if (
            event_attempt is None
            or event_challenge is None
            or event_attempt.challenge_ref != event.challenge_ref
            or event_attempt.state != "applied"
            or event_challenge.state != "applied"
            or not _event_matches_authority(
                event, event_challenge, event_attempt
            )
        ):
            return False
        events_by_attempt.setdefault(event.resume_attempt_ref, []).append(event)
    if any(len(bound_events) != 1 for bound_events in events_by_attempt.values()):
        return False
    for attempt in attempts:
        event_count = len(events_by_attempt.get(attempt.resume_attempt_ref, ()))
        if (attempt.state == "applied" and event_count != 1) or (
            attempt.state == "sealed" and event_count != 0
        ):
            return False

    transitions_by_challenge: dict[str, list[_TransitionV1]] = {}
    for transition in transitions:
        if transition.challenge_ref not in challenge_by_ref:
            return False
        if (
            transition.resume_attempt_ref is not None
            and transition.resume_attempt_ref not in attempt_by_ref
        ):
            return False
        transitions_by_challenge.setdefault(transition.challenge_ref, []).append(
            transition
        )

    for challenge in challenges:
        bound = transitions_by_challenge.get(challenge.challenge_ref, [])
        challenge_attempts = tuple(
            attempt
            for attempt in attempts
            if attempt.challenge_ref == challenge.challenge_ref
        )
        applied_attempts = tuple(
            attempt for attempt in challenge_attempts if attempt.state == "applied"
        )
        challenge_events = tuple(
            event
            for event in events
            if event.challenge_ref == challenge.challenge_ref
        )
        if len(applied_attempts) > 1:
            return False
        if challenge.state == "applied":
            if len(applied_attempts) != 1 or len(challenge_events) != 1:
                return False
        elif applied_attempts or challenge_events:
            return False
        initial = tuple(
            transition
            for transition in bound
            if transition.resume_attempt_ref is None
            and transition.from_state == "decided"
            and transition.to_state == "review_pending"
            and transition.observed_at == challenge.issued_at
        )
        if len(initial) != 1:
            return False
        for attempt in challenge_attempts:
            staged = tuple(
                transition
                for transition in bound
                if transition.resume_attempt_ref == attempt.resume_attempt_ref
                and transition.from_state == "attempt_unstaged"
                and transition.to_state == "attempt_staged"
            )
            if (
                len(staged) != 1
                or staged[0].sequence <= initial[0].sequence
                or not _causal_observation_is_reachable(
                    challenge,
                    predecessor_at=challenge.issued_at,
                    observed_at=staged[0].observed_at,
                )
            ):
                return False
            applied_transitions = tuple(
                transition
                for transition in bound
                if transition.resume_attempt_ref == attempt.resume_attempt_ref
                and (transition.from_state, transition.to_state)
                in {
                    ("issued", "claimed"),
                    ("claimed", "applied"),
                    ("review_pending", "review_applied"),
                }
            )
            if attempt.state == "applied":
                expected_states = (
                    ("issued", "claimed"),
                    ("claimed", "applied"),
                    ("review_pending", "review_applied"),
                )
                if tuple(
                    (transition.from_state, transition.to_state)
                    for transition in applied_transitions
                ) != expected_states or applied_transitions[0].sequence <= staged[0].sequence:
                    return False
                first_applied_sequence = applied_transitions[0].sequence
                if tuple(
                    transition.sequence for transition in applied_transitions
                ) != tuple(
                    range(first_applied_sequence, first_applied_sequence + 3)
                ) or any(
                    transition.to_state == "attempt_staged"
                    and transition.sequence >= first_applied_sequence
                    for transition in bound
                ):
                    return False
                event = events_by_attempt[attempt.resume_attempt_ref][0]
                if any(
                    transition.observed_at != event.applied_at
                    for transition in applied_transitions
                ) or not _causal_observation_is_reachable(
                    challenge,
                    predecessor_at=staged[0].observed_at,
                    observed_at=event.applied_at,
                ):
                    return False
            elif applied_transitions:
                return False
        allowed_count = 1 + sum(
            1 + (3 if attempt.state == "applied" else 0)
            for attempt in challenge_attempts
        )
        if len(bound) != allowed_count:
            return False

    if tuple(event.resume_attempt_ref for event in events) != tuple(
        transition.resume_attempt_ref
        for transition in transitions
        if transition.to_state == "review_applied"
    ):
        return False

    applied_challenge_refs = {
        attempt.challenge_ref for attempt in attempts if attempt.state == "applied"
    }
    if applied_challenge_refs != {
        challenge.challenge_ref
        for challenge in challenges
        if challenge.state == "applied"
    }:
        return False

    current_scopes = tuple(_scope_key(marker) for marker in current)
    checkpoint_scopes = {_scope_key(checkpoint) for checkpoint in checkpoints}
    if (
        len(current_scopes) != len(set(current_scopes))
        or set(current_scopes) != checkpoint_scopes
    ):
        return False
    for marker in current:
        scoped_checkpoints = tuple(
            checkpoint
            for checkpoint in checkpoints
            if _scope_key(checkpoint) == _scope_key(marker)
        )
        latest = max(scoped_checkpoints, key=lambda checkpoint: checkpoint.issue_sequence)
        if (
            marker.issue_sequence != latest.issue_sequence
            or marker.checkpoint_id != latest.checkpoint_id
            or marker.challenge_ref != latest.challenge_ref
        ):
            return False
    return True


class SandboxInMemoryReviewStore:
    """Thread-safe, sandbox-only in-memory reference domain store."""

    def __init__(self, *, snapshot: object | None = None) -> None:
        self._lock = threading.RLock()
        self._operation_count = 0
        initial: SandboxReviewStoreSnapshotV1 | None = None
        with suppress(Exception):
            initial = (
                SandboxReviewStoreSnapshotV1()
                if snapshot is None
                else _deep_model(SandboxReviewStoreSnapshotV1, snapshot)
            )
        if initial is None:
            raise SandboxReviewError()
        self._sources = list(initial.sources)
        self._challenges = list(initial.challenges)
        self._checkpoints = list(initial.checkpoints)
        self._attempts = list(initial.attempts)
        self._events = list(initial.events)
        self._transitions = list(initial.transitions)
        self._current_authorities = list(initial.current_authorities)

    @property
    def operation_count(self) -> int:
        with self._lock:
            return self._operation_count

    def snapshot(self) -> SandboxReviewStoreSnapshotV1:
        with self._lock:
            value = SandboxReviewStoreSnapshotV1(
                sources=tuple(self._sources),
                challenges=tuple(self._challenges),
                checkpoints=tuple(self._checkpoints),
                attempts=tuple(self._attempts),
                events=tuple(self._events),
                transitions=tuple(self._transitions),
                current_authorities=tuple(self._current_authorities),
            )
            return _deep_model(SandboxReviewStoreSnapshotV1, value)

    def recover_challenge(
        self,
        *,
        namespace: str,
        test_session_id: str,
        thread_id: str,
        checkpoint_id: str,
        interrupt_id: str,
    ) -> tuple[SandboxReviewChallengeV1, SandboxReviewSourceV1] | None:
        with self._lock:
            checkpoint = self._checkpoint(
                namespace, test_session_id, thread_id, checkpoint_id, interrupt_id
            )
            if checkpoint is None:
                return None
            challenge = self._challenge(checkpoint.challenge_ref)
            source = self._source(checkpoint.source_ref)
            if challenge is None or source is None:
                return None
            return (
                _deep_model(SandboxReviewChallengeV1, challenge),
                _deep_model(SandboxReviewSourceV1, source.source),
            )

    def issue(
        self,
        *,
        source: SandboxReviewSourceV1,
        challenge: SandboxReviewChallengeV1,
    ) -> bool:
        with self._lock:
            existing = self._checkpoint(
                challenge.namespace,
                challenge.test_session_id,
                challenge.thread_id,
                challenge.checkpoint_id,
                challenge.interrupt_id,
            )
            if existing is not None:
                return False
            source_copy = _deep_model(SandboxReviewSourceV1, source)
            challenge_copy = _deep_model(SandboxReviewChallengeV1, challenge)
            issue_sequence = len(self._checkpoints)
            provisional_source = _StoredSourceV1.model_construct(
                source_ref="sandbox-source-" + "0" * 64,
                issue_sequence=issue_sequence,
                namespace=challenge.namespace,
                test_session_id=challenge.test_session_id,
                thread_id=challenge.thread_id,
                checkpoint_id=challenge.checkpoint_id,
                interrupt_id=challenge.interrupt_id,
                source=source_copy,
            )
            stored_source = _StoredSourceV1(
                source_ref=_stored_source_ref(provisional_source),
                issue_sequence=issue_sequence,
                namespace=challenge.namespace,
                test_session_id=challenge.test_session_id,
                thread_id=challenge.thread_id,
                checkpoint_id=challenge.checkpoint_id,
                interrupt_id=challenge.interrupt_id,
                source=source_copy,
            )
            source_ref = stored_source.source_ref
            self._sources.append(stored_source)
            self._challenges.append(challenge_copy)
            self._checkpoints.append(
                _CheckpointV1(
                    issue_sequence=issue_sequence,
                    namespace=challenge.namespace,
                    test_session_id=challenge.test_session_id,
                    thread_id=challenge.thread_id,
                    checkpoint_id=challenge.checkpoint_id,
                    interrupt_id=challenge.interrupt_id,
                    challenge_ref=challenge.challenge_ref,
                    source_ref=source_ref,
                    state="review_pending",
                )
            )
            self._transitions.append(
                _transition(
                    challenge_ref=challenge.challenge_ref,
                    resume_attempt_ref=None,
                    from_state="decided",
                    to_state="review_pending",
                    observed_at=challenge.issued_at,
                    sequence=len(self._transitions),
                )
            )
            current = _CurrentAuthorityV1(
                issue_sequence=issue_sequence,
                namespace=challenge.namespace,
                test_session_id=challenge.test_session_id,
                thread_id=challenge.thread_id,
                checkpoint_id=challenge.checkpoint_id,
                challenge_ref=challenge.challenge_ref,
            )
            existing_current = next(
                (
                    marker
                    for marker in self._current_authorities
                    if _scope_key(marker) == _scope_key(current)
                ),
                None,
            )
            if existing_current is None:
                self._current_authorities.append(current)
            else:
                current_index = self._current_authorities.index(existing_current)
                self._current_authorities[current_index] = current
            self._operation_count += 1
            return True

    def stage(
        self,
        *,
        submission: SandboxResumeSubmissionV1,
        attempt: _SealedAttemptV1,
        now: int,
    ) -> bool:
        with self._lock:
            challenge = self._challenge(submission.challenge.challenge_ref)
            if challenge is None:
                return False
            checkpoint = self._checkpoint(
                challenge.namespace,
                challenge.test_session_id,
                challenge.thread_id,
                challenge.checkpoint_id,
                challenge.interrupt_id,
            )
            source = None if checkpoint is None else self._source(checkpoint.source_ref)
            if checkpoint is None or source is None:
                return False
            if challenge.state == "expired":
                return False
            if now >= challenge.expires_at:
                self._replace_challenge(challenge, state="expired")
                self._operation_count += 1
                return False
            if not _causal_observation_is_reachable(
                challenge,
                predecessor_at=challenge.issued_at,
                observed_at=now,
            ):
                return False
            if (
                challenge.state != "issued"
                or checkpoint.state != "review_pending"
                or submission.challenge != challenge
                or not _challenge_matches_source(challenge, source.source)
                or attempt.source_ref != checkpoint.source_ref
            ):
                return False
            existing = self._attempt(attempt.resume_attempt_ref)
            if existing is None:
                attempt_copy = _deep_model(_SealedAttemptV1, attempt)
                self._attempts.append(attempt_copy)
                self._transitions.append(
                    _transition(
                        challenge_ref=challenge.challenge_ref,
                        resume_attempt_ref=attempt.resume_attempt_ref,
                        from_state="attempt_unstaged",
                        to_state="attempt_staged",
                        observed_at=now,
                        sequence=len(self._transitions),
                    )
                )
                self._operation_count += 1
            return True

    def apply(
        self, command: SandboxResumeCommandV1, *, now: int
    ) -> Literal["applied", "replayed_or_conflict", "resume_rejected"]:
        with self._lock:
            attempt = self._attempt(command.resume_attempt_ref)
            if attempt is None:
                return "resume_rejected"
            challenge = self._challenge(attempt.challenge_ref)
            source = self._source(attempt.source_ref)
            if challenge is None or source is None:
                return "resume_rejected"
            checkpoint = self._checkpoint(
                challenge.namespace,
                challenge.test_session_id,
                challenge.thread_id,
                challenge.checkpoint_id,
                challenge.interrupt_id,
            )
            if checkpoint is None:
                return "resume_rejected"
            if (
                attempt.state == "applied"
                or challenge.state in {"claimed", "applied"}
                or checkpoint.state == "review_applied"
            ):
                return "replayed_or_conflict"
            if challenge.state == "expired":
                return "resume_rejected"
            if now >= challenge.expires_at:
                self._replace_challenge(challenge, state="expired")
                self._operation_count += 1
                return "resume_rejected"
            staged = tuple(
                transition
                for transition in self._transitions
                if transition.challenge_ref == challenge.challenge_ref
                and transition.resume_attempt_ref == attempt.resume_attempt_ref
                and transition.from_state == "attempt_unstaged"
                and transition.to_state == "attempt_staged"
            )
            if len(staged) != 1 or not _causal_observation_is_reachable(
                challenge,
                predecessor_at=(
                    staged[0].observed_at
                    if staged
                    else challenge.issued_at
                ),
                observed_at=now,
            ):
                return "resume_rejected"
            if (
                challenge.state != "issued"
                or checkpoint.state != "review_pending"
                or attempt.namespace != challenge.namespace
                or attempt.test_session_id != challenge.test_session_id
                or not _challenge_matches_source(challenge, source.source)
            ):
                return "resume_rejected"

            claimed = self._replace_challenge(challenge, state="claimed")
            self._transitions.append(
                _transition(
                    challenge_ref=challenge.challenge_ref,
                    resume_attempt_ref=attempt.resume_attempt_ref,
                    from_state="issued",
                    to_state="claimed",
                    observed_at=now,
                    sequence=len(self._transitions),
                )
            )
            applied = self._replace_challenge(claimed, state="applied")
            self._replace_checkpoint(checkpoint, state="review_applied")
            self._replace_attempt(attempt, state="applied")
            self._transitions.extend(
                (
                    _transition(
                        challenge_ref=applied.challenge_ref,
                        resume_attempt_ref=attempt.resume_attempt_ref,
                        from_state="claimed",
                        to_state="applied",
                        observed_at=now,
                        sequence=len(self._transitions),
                    ),
                    _transition(
                        challenge_ref=applied.challenge_ref,
                        resume_attempt_ref=attempt.resume_attempt_ref,
                        from_state="review_pending",
                        to_state="review_applied",
                        observed_at=now,
                        sequence=len(self._transitions) + 1,
                    ),
                )
            )
            self._events.append(
                _review_event(
                    applied,
                    attempt,
                    now=now,
                    sequence=len(self._events),
                )
            )
            self._operation_count += 1
            return "applied"

    def eligibility(
        self,
        *,
        namespace: str,
        test_session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> bool:
        with self._lock:
            current = tuple(
                marker
                for marker in self._current_authorities
                if marker.namespace == namespace
                and marker.test_session_id == test_session_id
                and marker.thread_id == thread_id
            )
            if (
                len(current) != 1
                or current[0].checkpoint_id != checkpoint_id
            ):
                return False
            marker = current[0]
            checkpoints = tuple(
                checkpoint
                for checkpoint in self._checkpoints
                if checkpoint.issue_sequence == marker.issue_sequence
                and checkpoint.namespace == namespace
                and checkpoint.test_session_id == test_session_id
                and checkpoint.thread_id == thread_id
                and checkpoint.checkpoint_id == marker.checkpoint_id
                and checkpoint.challenge_ref == marker.challenge_ref
            )
            if len(checkpoints) != 1 or checkpoints[0].state != "review_applied":
                return False
            checkpoint = checkpoints[0]
            challenge = self._challenge(checkpoint.challenge_ref)
            source = self._source(checkpoint.source_ref)
            if (
                challenge is None
                or source is None
                or challenge.state != "applied"
                or not _challenge_matches_source(challenge, source.source)
            ):
                return False
            events = tuple(
                event
                for event in self._events
                if event.challenge_ref == challenge.challenge_ref
            )
            return (
                len(events) == 1
                and events[0].action is SandboxReviewAction.CONFIRM
                and events[0].review_render_digest == source.source.review_render_digest
            )

    def _challenge(self, challenge_ref: str) -> SandboxReviewChallengeV1 | None:
        return next(
            (
                challenge
                for challenge in self._challenges
                if challenge.challenge_ref == challenge_ref
            ),
            None,
        )

    def _source(self, source_ref: str) -> _StoredSourceV1 | None:
        return next(
            (source for source in self._sources if source.source_ref == source_ref),
            None,
        )

    def _attempt(self, resume_attempt_ref: str) -> _SealedAttemptV1 | None:
        return next(
            (
                attempt
                for attempt in self._attempts
                if attempt.resume_attempt_ref == resume_attempt_ref
            ),
            None,
        )

    def _checkpoint(
        self,
        namespace: str,
        test_session_id: str,
        thread_id: str,
        checkpoint_id: str,
        interrupt_id: str,
    ) -> _CheckpointV1 | None:
        return next(
            (
                checkpoint
                for checkpoint in self._checkpoints
                if checkpoint.namespace == namespace
                and checkpoint.test_session_id == test_session_id
                and checkpoint.thread_id == thread_id
                and checkpoint.checkpoint_id == checkpoint_id
                and checkpoint.interrupt_id == interrupt_id
            ),
            None,
        )

    def _replace_challenge(
        self,
        challenge: SandboxReviewChallengeV1,
        *,
        state: Literal["issued", "expired", "claimed", "applied"],
    ) -> SandboxReviewChallengeV1:
        replacement = challenge.model_copy(update={"state": state})
        index = self._challenges.index(challenge)
        self._challenges[index] = replacement
        return replacement

    def _replace_checkpoint(
        self,
        checkpoint: _CheckpointV1,
        *,
        state: Literal["review_pending", "review_applied"],
    ) -> None:
        index = self._checkpoints.index(checkpoint)
        self._checkpoints[index] = checkpoint.model_copy(update={"state": state})

    def _replace_attempt(
        self,
        attempt: _SealedAttemptV1,
        *,
        state: Literal["sealed", "applied"],
    ) -> None:
        index = self._attempts.index(attempt)
        self._attempts[index] = attempt.model_copy(update={"state": state})


def _transition(
    *,
    challenge_ref: str,
    resume_attempt_ref: str | None,
    from_state: str,
    to_state: str,
    observed_at: int,
    sequence: int,
) -> _TransitionV1:
    body = {
        "challenge_ref": challenge_ref,
        "from_state": from_state,
        "observed_at": observed_at,
        "resume_attempt_ref": resume_attempt_ref,
        "sequence": sequence,
        "to_state": to_state,
    }
    return _TransitionV1(
        transition_ref=f"sandbox-transition-{_sha256(body)}",
        sequence=sequence,
        challenge_ref=challenge_ref,
        resume_attempt_ref=resume_attempt_ref,
        from_state=from_state,
        to_state=to_state,
        observed_at=observed_at,
    )


def _review_event(
    challenge: SandboxReviewChallengeV1,
    attempt: _SealedAttemptV1,
    *,
    now: int,
    sequence: int,
) -> SandboxTestReviewEventV1:
    body: dict[str, object] = {
        "action": attempt.action,
        "adapter_version": challenge.adapter_version,
        "applied_at": now,
        "challenge_ref": challenge.challenge_ref,
        "checkpoint_id": challenge.checkpoint_id,
        "domain_state_version": challenge.domain_state_version,
        "formula_revision": challenge.formula_revision,
        "graph_version": challenge.graph_version,
        "input_digest": challenge.input_digest,
        "interrupt_id": challenge.interrupt_id,
        "namespace": challenge.namespace,
        "result_digest": challenge.result_digest,
        "resume_attempt_ref": attempt.resume_attempt_ref,
        "review_render_digest": challenge.review_render_digest,
        "rule_bundle_digest": challenge.rule_bundle_digest,
        "sandbox_schema_version": challenge.sandbox_schema_version,
        "sequence": sequence,
        "sandbox_test_key_id": attempt.sandbox_test_key_id,
        "sandbox_test_organization_label": attempt.sandbox_test_organization_label,
        "sandbox_test_qualification_label": attempt.sandbox_test_qualification_label,
        "sandbox_test_reviewer_id": attempt.sandbox_test_reviewer_id,
        "sandbox_test_role": attempt.sandbox_test_role,
        "sandbox_test_signature_scheme": attempt.sandbox_test_signature_scheme,
        "sandbox_test_signed_payload_digest": (
            attempt.sandbox_test_signed_payload_digest
        ),
        "synthetic_dataset_digest": challenge.synthetic_dataset_digest,
        "test_session_id": challenge.test_session_id,
        "thread_id": challenge.thread_id,
    }
    return SandboxTestReviewEventV1(
        event_ref=f"sandbox-review-event-{_sha256(body)}",
        sequence=sequence,
        resume_attempt_ref=attempt.resume_attempt_ref,
        challenge_ref=challenge.challenge_ref,
        sandbox_schema_version=challenge.sandbox_schema_version,
        adapter_version=challenge.adapter_version,
        graph_version=challenge.graph_version,
        namespace=challenge.namespace,
        test_session_id=challenge.test_session_id,
        thread_id=challenge.thread_id,
        checkpoint_id=challenge.checkpoint_id,
        interrupt_id=challenge.interrupt_id,
        domain_state_version=challenge.domain_state_version,
        formula_revision=challenge.formula_revision,
        input_digest=challenge.input_digest,
        result_digest=challenge.result_digest,
        rule_bundle_digest=challenge.rule_bundle_digest,
        synthetic_dataset_digest=challenge.synthetic_dataset_digest,
        review_render_digest=challenge.review_render_digest,
        action=attempt.action,
        sandbox_test_reviewer_id=attempt.sandbox_test_reviewer_id,
        sandbox_test_role=attempt.sandbox_test_role,
        sandbox_test_organization_label=attempt.sandbox_test_organization_label,
        sandbox_test_qualification_label=attempt.sandbox_test_qualification_label,
        sandbox_test_signature_scheme=attempt.sandbox_test_signature_scheme,
        sandbox_test_key_id=attempt.sandbox_test_key_id,
        sandbox_test_signed_payload_digest=(
            attempt.sandbox_test_signed_payload_digest
        ),
        applied_at=now,
    )


def _challenge_matches_source(
    challenge: SandboxReviewChallengeV1,
    source: SandboxReviewSourceV1,
) -> bool:
    subject = source.safety_subject
    return (
        challenge.adapter_version == subject.adapter_version
        and challenge.graph_version == subject.graph_version
        and challenge.test_session_id == subject.test_session_id
        and challenge.domain_state_version == subject.domain_state_version
        and challenge.formula_revision == subject.formula_revision
        and challenge.input_digest == source.input_digest
        and challenge.result_digest == source.result_digest
        and challenge.rule_bundle_digest == subject.rule_bundle_digest
        and challenge.synthetic_dataset_digest == subject.synthetic_dataset_digest
        and challenge.review_render_digest == source.review_render_digest
    )


class SandboxReviewCoordinator:
    """Offline coordinator whose only resume authority is the domain store."""

    def __init__(
        self,
        *,
        store: SandboxInMemoryReviewStore,
        clock: Callable[[], int],
        nonce_factory: Callable[[], bytes],
        signature_verifier: SandboxSignatureVerifier,
    ) -> None:
        self._store = store
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._signature_verifier = signature_verifier

    def create_single_use_challenge(
        self,
        source: SandboxReviewSourceV1,
        *,
        namespace: str,
        thread_id: str,
        checkpoint_id: str,
        interrupt_id: str,
    ) -> SandboxChallengeDeliveryV1:
        try:
            accepted = _deep_model(SandboxReviewSourceV1, source)
            if accepted.safety_result.decision is not SandboxSafetyDecision.ALLOW:
                raise SandboxReviewError()
            recovered = self._store.recover_challenge(
                namespace=namespace,
                test_session_id=accepted.safety_subject.test_session_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                interrupt_id=interrupt_id,
            )
            if recovered is not None:
                challenge, stored_source = recovered
                if stored_source != accepted:
                    raise SandboxReviewError()
                return SandboxChallengeDeliveryV1(challenge=challenge)

            plaintext_nonce = self._nonce_factory()
            if type(plaintext_nonce) is not bytes or len(plaintext_nonce) != 32:
                raise SandboxReviewError()
            now = self._now()
            subject = accepted.safety_subject
            provisional = SandboxReviewChallengeV1.model_construct(
                challenge_ref="sandbox-challenge-" + "0" * 64,
                sandbox_schema_version=_REVIEW_SCHEMA_VERSION,
                adapter_version=subject.adapter_version,
                graph_version=subject.graph_version,
                namespace=namespace,
                test_session_id=subject.test_session_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                interrupt_id=interrupt_id,
                domain_state_version=subject.domain_state_version,
                formula_revision=subject.formula_revision,
                input_digest=accepted.input_digest,
                result_digest=accepted.result_digest,
                rule_bundle_digest=subject.rule_bundle_digest,
                synthetic_dataset_digest=subject.synthetic_dataset_digest,
                review_render_digest=accepted.review_render_digest,
                allowed_actions=_ALLOWED_ACTIONS,
                synthetic_technical_summary="synthetic_safety_review_pending",
                issued_at=now,
                expires_at=now + _CHALLENGE_TTL_SECONDS,
                nonce_digest=_bytes_sha256(plaintext_nonce),
                state="issued",
            )
            challenge = SandboxReviewChallengeV1(
                challenge_ref=_challenge_ref(provisional),
                sandbox_schema_version=_REVIEW_SCHEMA_VERSION,
                adapter_version=subject.adapter_version,
                graph_version=subject.graph_version,
                namespace=namespace,
                test_session_id=subject.test_session_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                interrupt_id=interrupt_id,
                domain_state_version=subject.domain_state_version,
                formula_revision=subject.formula_revision,
                input_digest=accepted.input_digest,
                result_digest=accepted.result_digest,
                rule_bundle_digest=subject.rule_bundle_digest,
                synthetic_dataset_digest=subject.synthetic_dataset_digest,
                review_render_digest=accepted.review_render_digest,
                allowed_actions=_ALLOWED_ACTIONS,
                synthetic_technical_summary="synthetic_safety_review_pending",
                issued_at=now,
                expires_at=now + _CHALLENGE_TTL_SECONDS,
                nonce_digest=_bytes_sha256(plaintext_nonce),
                state="issued",
            )
            created = self._store.issue(source=accepted, challenge=challenge)
            if not created:
                recovered = self._store.recover_challenge(
                    namespace=namespace,
                    test_session_id=subject.test_session_id,
                    thread_id=thread_id,
                    checkpoint_id=checkpoint_id,
                    interrupt_id=interrupt_id,
                )
                if recovered is None or recovered[1] != accepted:
                    raise SandboxReviewError()
                return SandboxChallengeDeliveryV1(challenge=recovered[0])
            return SandboxChallengeDeliveryV1(
                challenge=challenge,
                plaintext_nonce=plaintext_nonce,
            )
        except Exception:
            pass
        raise SandboxReviewError()

    def stage_verified_resume_attempt(
        self, submission_input: SandboxResumeSubmissionV1 | bytes
    ) -> _StageResultV1:
        try:
            encoded = (
                bytes(submission_input)
                if isinstance(submission_input, bytes)
                else canonical_review_bytes(submission_input)
            )
            if len(encoded) > MAX_RESUME_SUBMISSION_BYTES:
                return _fixed_stage_rejection()
            submission = SandboxResumeSubmissionV1.model_validate_json(
                encoded, strict=True
            )
            challenge = submission.challenge
            if (
                submission.namespace != challenge.namespace
                or submission.test_session_id != challenge.test_session_id
                or submission.action not in challenge.allowed_actions
                or len(submission.plaintext_nonce) != 32
                or _bytes_sha256(submission.plaintext_nonce)
                != challenge.nonce_digest
            ):
                return _fixed_stage_rejection()
            proof = submission.proof
            signed_payload_digest = review_signed_payload_digest(
                challenge=challenge,
                action=submission.action,
                plaintext_nonce=submission.plaintext_nonce,
                sandbox_test_reviewer_id=proof.sandbox_test_reviewer_id,
                sandbox_test_role=proof.sandbox_test_role,
                sandbox_test_organization_label=(
                    proof.sandbox_test_organization_label
                ),
                sandbox_test_qualification_label=(
                    proof.sandbox_test_qualification_label
                ),
                sandbox_test_signature_scheme=proof.sandbox_test_signature_scheme,
                sandbox_test_key_id=proof.sandbox_test_key_id,
            )
            if (
                proof.sandbox_test_signed_payload_digest != signed_payload_digest
                or not self._signature_verifier.verify(
                    signed_payload_digest=signed_payload_digest,
                    sandbox_test_signature_scheme=(
                        proof.sandbox_test_signature_scheme
                    ),
                    sandbox_test_key_id=proof.sandbox_test_key_id,
                    sandbox_test_signature=proof.sandbox_test_signature,
                )
            ):
                return _fixed_stage_rejection()
            snapshot = self._store.snapshot()
            matching = tuple(
                source
                for source in snapshot.sources
                if source.namespace == challenge.namespace
                and source.test_session_id == challenge.test_session_id
                and source.thread_id == challenge.thread_id
                and source.checkpoint_id == challenge.checkpoint_id
                and source.interrupt_id == challenge.interrupt_id
            )
            if len(matching) != 1:
                return _fixed_stage_rejection()
            source_ref = matching[0].source_ref
            provisional_attempt = _SealedAttemptV1.model_construct(
                resume_attempt_ref="sandbox-attempt-" + "0" * 64,
                challenge_ref=challenge.challenge_ref,
                source_ref=source_ref,
                namespace=submission.namespace,
                test_session_id=submission.test_session_id,
                action=submission.action,
                sandbox_test_reviewer_id=proof.sandbox_test_reviewer_id,
                sandbox_test_role=proof.sandbox_test_role,
                sandbox_test_organization_label=(
                    proof.sandbox_test_organization_label
                ),
                sandbox_test_qualification_label=(
                    proof.sandbox_test_qualification_label
                ),
                sandbox_test_signature_scheme=proof.sandbox_test_signature_scheme,
                sandbox_test_key_id=proof.sandbox_test_key_id,
                sandbox_test_signed_payload_digest=signed_payload_digest,
                state="sealed",
            )
            attempt = _SealedAttemptV1(
                resume_attempt_ref=_attempt_ref(provisional_attempt),
                challenge_ref=challenge.challenge_ref,
                source_ref=source_ref,
                namespace=submission.namespace,
                test_session_id=submission.test_session_id,
                action=submission.action,
                sandbox_test_reviewer_id=proof.sandbox_test_reviewer_id,
                sandbox_test_role=proof.sandbox_test_role,
                sandbox_test_organization_label=(
                    proof.sandbox_test_organization_label
                ),
                sandbox_test_qualification_label=(
                    proof.sandbox_test_qualification_label
                ),
                sandbox_test_signature_scheme=proof.sandbox_test_signature_scheme,
                sandbox_test_key_id=proof.sandbox_test_key_id,
                sandbox_test_signed_payload_digest=signed_payload_digest,
                state="sealed",
            )
            if not self._store.stage(
                submission=submission,
                attempt=attempt,
                now=self._now(),
            ):
                return _fixed_stage_rejection()
            return _StageResultV1(
                status="staged", resume_attempt_ref=attempt.resume_attempt_ref
            )
        except (ValidationError, UnicodeError, ValueError, TypeError):
            return _fixed_stage_rejection()
        except Exception:
            return _fixed_stage_rejection()

    def resume(self, command_input: SandboxResumeCommandV1) -> _ResumeResultV1:
        try:
            command = _deep_model(SandboxResumeCommandV1, command_input)
            status = self._store.apply(command, now=self._now())
            if status == "applied":
                return _ResumeResultV1(status="applied", command=command)
            if status == "replayed_or_conflict":
                return _ResumeResultV1(status="replayed_or_conflict")
            return _fixed_resume_rejection()
        except Exception:
            return _fixed_resume_rejection()

    def eligibility(
        self,
        *,
        namespace: str,
        test_session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> _EligibilityV1:
        try:
            eligible = self._store.eligibility(
                namespace=namespace,
                test_session_id=test_session_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
            )
            return _EligibilityV1(status="eligible" if eligible else "blocked")
        except Exception:
            return _EligibilityV1(status="blocked")

    def _now(self) -> int:
        value = self._clock()
        if type(value) is not int or value < 0:
            raise SandboxReviewError()
        return value
