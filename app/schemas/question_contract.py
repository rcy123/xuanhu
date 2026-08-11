"""Versioned R9 question-contract and coverage-ledger DTOs.

The contract freezes *what the current question is trying to collect* as one
to four generic aspects.  It deliberately contains no per-dimension slot
schema.  Model-produced coverage is a candidate only: ``build_coverage_event``
must ground every claimed span against the current answer before the compact,
quote-free event may be persisted.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QUESTION_CONTRACT_SCHEMA_VERSION: Literal["question-contract.v1"] = "question-contract.v1"
QUESTION_CONTRACT_REF_SCHEMA_VERSION: Literal["question-contract-ref.v1"] = "question-contract-ref.v1"
CONTRACT_REPLY_CONTEXT_SCHEMA_VERSION: Literal["contract-reply-context.v1"] = "contract-reply-context.v1"
QUESTION_COVERAGE_CANDIDATE_SCHEMA_VERSION: Literal["question-coverage-candidate.v1"] = (
    "question-coverage-candidate.v1"
)
QUESTION_COVERAGE_EVENT_SCHEMA_VERSION: Literal["question-coverage-event.v1"] = "question-coverage-event.v1"
CONTRACT_COVERAGE_PROJECTION_SCHEMA_VERSION: Literal["contract-coverage-projection.v1"] = (
    "contract-coverage-projection.v1"
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DIMENSION_PATTERN = r"^[a-z][a-z0-9_.-]*$"


class _ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CoverageStatus(StrEnum):
    """Evidence-grounded status of one aspect in one answer.

    Only ``addressed`` and ``not_applicable`` satisfy an aspect.  The other
    values remain visible to the fold so the graph can ask only the residual
    aspects and eventually apply the configured cap.
    """

    ADDRESSED = "addressed"
    NOT_APPLICABLE = "not_applicable"
    UNANSWERED = "unanswered"
    UNCLEAR = "unclear"
    UNAVAILABLE = "unavailable"


class ContractCoverageDisposition(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    EXHAUSTED_PARTIAL = "exhausted_partial"
    MANUAL_REQUIRED = "manual_required"


def canonical_sha256(value: object) -> str:
    """Return a deterministic SHA-256 digest for a JSON-safe value."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def question_contract_id(question_message_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"xuanhu:question-contract:v1:{question_message_id}")


def question_aspect_id(root_contract_id: UUID, ordinal: int, criterion: str) -> UUID:
    criterion_digest = hashlib.sha256(criterion.encode("utf-8")).hexdigest()
    return uuid5(root_contract_id, f"question-aspect:v1:{ordinal}:{criterion_digest}")


def question_coverage_event_id(contract_id: UUID, answer_message_id: UUID) -> UUID:
    return uuid5(contract_id, f"question-coverage-event:v1:{answer_message_id}")


def _clean_human_text(value: str, *, field_name: str) -> str:
    if value != value.strip() or not value:
        raise ValueError(f"{field_name} must be non-blank and trimmed")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field_name} must use NFC Unicode normalization")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


class QuestionAspect(_ContractModel):
    """One generic, immutable criterion within a root question contract."""

    aspect_id: UUID
    ordinal: int = Field(ge=0, le=3)
    criterion: str = Field(min_length=1, max_length=240, repr=False)
    required: bool = True

    @field_validator("criterion")
    @classmethod
    def clean_criterion(cls, value: str) -> str:
        return _clean_human_text(value, field_name="criterion")


class QuestionContract(_ContractModel):
    """Durable immutable intent for one root question or residual follow-up."""

    schema_version: Literal["question-contract.v1"] = QUESTION_CONTRACT_SCHEMA_VERSION
    contract_id: UUID
    session_id: UUID
    question_message_id: UUID
    root_contract_id: UUID
    parent_contract_id: UUID | None = None
    revision: int = Field(ge=1, le=5)
    dimension: str = Field(min_length=1, max_length=64, pattern=_DIMENSION_PATTERN)
    selection_kind: Literal["required", "conflict"]
    safety_critical: bool = False
    max_followups: int = Field(default=2, ge=1, le=4)
    question_digest: str = Field(pattern=_SHA256_PATTERN)
    aspects: tuple[QuestionAspect, ...] = Field(min_length=1, max_length=4)
    contract_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_identity_and_digest(self) -> QuestionContract:
        if self.contract_id != question_contract_id(self.question_message_id):
            raise ValueError("contract_id does not match its stable question-message identity")
        if self.revision > self.max_followups + 1:
            raise ValueError("revision exceeds max_followups")
        if self.revision == 1:
            if self.root_contract_id != self.contract_id or self.parent_contract_id is not None:
                raise ValueError("root contract must identify itself and have no parent")
        elif self.root_contract_id == self.contract_id or self.parent_contract_id is None:
            raise ValueError("follow-up contract requires a distinct root and a parent")

        aspect_ids = [item.aspect_id for item in self.aspects]
        ordinals = [item.ordinal for item in self.aspects]
        canonical_criteria = [item.criterion.casefold() for item in self.aspects]
        if len(aspect_ids) != len(set(aspect_ids)):
            raise ValueError("contract aspect ids must be unique")
        if len(ordinals) != len(set(ordinals)) or ordinals != sorted(ordinals):
            raise ValueError("contract aspects must have unique ascending ordinals")
        if len(canonical_criteria) != len(set(canonical_criteria)):
            raise ValueError("contract aspect criteria must be unique")
        if not any(item.required for item in self.aspects):
            raise ValueError("a contract requires at least one required aspect")
        for aspect in self.aspects:
            expected_id = question_aspect_id(self.root_contract_id, aspect.ordinal, aspect.criterion)
            if aspect.aspect_id != expected_id:
                raise ValueError("aspect_id does not match the stable root criterion identity")
        if self.contract_digest != _contract_digest(self):
            raise ValueError("contract_digest does not match the canonical contract")
        return self

    def to_ref(self) -> QuestionContractRef:
        return QuestionContractRef(
            contract_id=self.contract_id,
            root_contract_id=self.root_contract_id,
            question_message_id=self.question_message_id,
            revision=self.revision,
            question_digest=self.question_digest,
            contract_digest=self.contract_digest,
        )


class QuestionContractRef(_ContractModel):
    """Public-safe structured-message binding; intentionally omits criteria."""

    schema_version: Literal["question-contract-ref.v1"] = QUESTION_CONTRACT_REF_SCHEMA_VERSION
    contract_id: UUID
    root_contract_id: UUID
    question_message_id: UUID
    revision: int = Field(ge=1, le=5)
    question_digest: str = Field(pattern=_SHA256_PATTERN)
    contract_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_stable_contract_id(self) -> QuestionContractRef:
        if self.contract_id != question_contract_id(self.question_message_id):
            raise ValueError("contract reference has an invalid stable contract_id")
        if (self.revision == 1) != (self.contract_id == self.root_contract_id):
            raise ValueError("contract reference root relation does not match revision")
        return self


class ContractReplyContext(_ContractModel):
    """Private model context reconstructed from the durable contract table."""

    schema_version: Literal["contract-reply-context.v1"] = CONTRACT_REPLY_CONTEXT_SCHEMA_VERSION
    contract: QuestionContract
    answer_message_id: UUID


class CoverageEvidenceCandidate(_ContractModel):
    """Transient quote-bearing span emitted at the model boundary."""

    source_message_id: UUID
    start_char: int = Field(ge=0, le=4_000)
    end_char: int = Field(gt=0, le=4_000)
    quote: str = Field(min_length=1, max_length=4_000, repr=False)

    @model_validator(mode="after")
    def validate_range(self) -> CoverageEvidenceCandidate:
        if self.end_char <= self.start_char:
            raise ValueError("coverage evidence must be a non-empty half-open range")
        return self


class EvidenceRef(_ContractModel):
    """Persistable evidence locator; raw answer text is never copied here."""

    source_message_id: UUID
    start_char: int = Field(ge=0, le=4_000)
    end_char: int = Field(gt=0, le=4_000)
    quote_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceRef:
        if self.end_char <= self.start_char:
            raise ValueError("evidence reference must be a non-empty half-open range")
        return self


class CoverageCandidateItem(_ContractModel):
    aspect_id: UUID
    status: CoverageStatus
    evidence: tuple[CoverageEvidenceCandidate, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def validate_evidence_relation(self) -> CoverageCandidateItem:
        if self.status is CoverageStatus.UNANSWERED:
            if self.evidence:
                raise ValueError("unanswered aspect must not carry evidence")
        elif not self.evidence:
            raise ValueError("an explicit coverage status requires grounded evidence")
        if len(self.evidence) != len(
            {(item.source_message_id, item.start_char, item.end_char) for item in self.evidence}
        ):
            raise ValueError("coverage evidence spans must be unique")
        return self


class QuestionCoverageCandidate(_ContractModel):
    """Untrusted model product for exactly one bound answer."""

    schema_version: Literal["question-coverage-candidate.v1"] = QUESTION_COVERAGE_CANDIDATE_SCHEMA_VERSION
    contract_id: UUID
    answer_message_id: UUID
    items: tuple[CoverageCandidateItem, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_aspects(self) -> QuestionCoverageCandidate:
        aspect_ids = [item.aspect_id for item in self.items]
        if len(aspect_ids) != len(set(aspect_ids)):
            raise ValueError("coverage candidate aspect ids must be unique")
        return self


class CoverageEventItem(_ContractModel):
    aspect_id: UUID
    status: CoverageStatus
    evidence: tuple[EvidenceRef, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def validate_evidence_relation(self) -> CoverageEventItem:
        if self.status is CoverageStatus.UNANSWERED:
            if self.evidence:
                raise ValueError("unanswered persisted aspect must not carry evidence")
        elif not self.evidence:
            raise ValueError("an explicit persisted coverage status requires evidence")
        if len(self.evidence) != len(
            {(item.source_message_id, item.start_char, item.end_char) for item in self.evidence}
        ):
            raise ValueError("persisted evidence references must be unique")
        return self


class QuestionCoverageEvent(_ContractModel):
    """Append-only, quote-free, deterministically identified coverage event."""

    schema_version: Literal["question-coverage-event.v1"] = QUESTION_COVERAGE_EVENT_SCHEMA_VERSION
    event_id: UUID
    session_id: UUID
    contract_id: UUID
    root_contract_id: UUID
    answer_message_id: UUID
    items: tuple[CoverageEventItem, ...] = Field(min_length=1, max_length=4)
    event_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_identity_and_digest(self) -> QuestionCoverageEvent:
        if self.event_id != question_coverage_event_id(self.contract_id, self.answer_message_id):
            raise ValueError("event_id does not match the stable contract-answer identity")
        aspect_ids = [item.aspect_id for item in self.items]
        if len(aspect_ids) != len(set(aspect_ids)):
            raise ValueError("coverage event aspect ids must be unique")
        if self.event_digest != _coverage_event_digest(self):
            raise ValueError("event_digest does not match the canonical event")
        return self


class AspectCoverageProjection(_ContractModel):
    """Joint evidence metadata folded across the first answer and follow-ups."""

    aspect_id: UUID
    latest_status: CoverageStatus
    evidence: tuple[EvidenceRef, ...] = Field(default=(), max_length=20)
    source_event_ids: tuple[UUID, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def unique_projection_metadata(self) -> AspectCoverageProjection:
        if len(self.source_event_ids) != len(set(self.source_event_ids)):
            raise ValueError("source event ids must be unique")
        if len(self.evidence) != len(
            {(item.source_message_id, item.start_char, item.end_char) for item in self.evidence}
        ):
            raise ValueError("joint evidence references must be unique")
        return self


class ContractCoverageProjection(_ContractModel):
    schema_version: Literal["contract-coverage-projection.v1"] = CONTRACT_COVERAGE_PROJECTION_SCHEMA_VERSION
    root_contract_id: UUID
    latest_contract_id: UUID
    latest_revision: int = Field(ge=1, le=5)
    disposition: ContractCoverageDisposition
    satisfied_aspect_ids: tuple[UUID, ...] = Field(default=(), max_length=4)
    unresolved_aspect_ids: tuple[UUID, ...] = Field(default=(), max_length=4)
    residual_aspects: tuple[QuestionAspect, ...] = Field(default=(), max_length=4)
    joint_evidence: tuple[AspectCoverageProjection, ...] = Field(min_length=1, max_length=4)
    followups_used: int = Field(ge=0, le=4)
    max_followups: int = Field(ge=1, le=4)
    event_count: int = Field(ge=0, le=5)

    @model_validator(mode="after")
    def validate_projection_sets(self) -> ContractCoverageProjection:
        satisfied = set(self.satisfied_aspect_ids)
        unresolved = set(self.unresolved_aspect_ids)
        evidence_ids = [item.aspect_id for item in self.joint_evidence]
        if satisfied & unresolved:
            raise ValueError("satisfied and unresolved aspect sets must be disjoint")
        if satisfied | unresolved != set(evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("projection aspect partitions must match joint evidence")
        if tuple(item.aspect_id for item in self.residual_aspects) != self.unresolved_aspect_ids:
            raise ValueError("residual aspects must exactly match unresolved aspect order")
        if self.disposition is ContractCoverageDisposition.SATISFIED:
            if self.unresolved_aspect_ids:
                raise ValueError("satisfied projection cannot carry unresolved aspects")
        elif not self.unresolved_aspect_ids:
            raise ValueError("non-satisfied projection requires unresolved aspects")
        if self.followups_used != self.latest_revision - 1:
            raise ValueError("followups_used must match latest revision")
        if self.event_count > self.latest_revision:
            raise ValueError("event count cannot exceed contract count")
        return self


def build_question_contract(
    *,
    session_id: UUID,
    question_message_id: UUID,
    question_text: str,
    dimension: str | None = None,
    selection_kind: Literal["required", "conflict"] | None = None,
    aspect_criteria: Sequence[str] | None = None,
    max_followups: int | None = None,
    safety_critical: bool | None = None,
    parent_contract: QuestionContract | None = None,
    residual_aspects: Sequence[QuestionAspect] | None = None,
) -> QuestionContract:
    """Build a root contract or a target-preserving residual follow-up.

    Follow-ups inherit every authority field and may only retain aspects from
    their direct parent.  ``evaluate_contract_coverage`` independently proves
    that the retained set is the *exact* residual of the prior event.
    """

    _clean_human_text(question_text, field_name="question_text")
    contract_id = question_contract_id(question_message_id)
    if parent_contract is None:
        if dimension is None or selection_kind is None or not aspect_criteria:
            raise ValueError("root contract requires dimension, selection_kind and aspect_criteria")
        if residual_aspects is not None:
            raise ValueError("root contract cannot carry residual aspects")
        resolved_max_followups = 2 if max_followups is None else max_followups
        resolved_safety_critical = False if safety_critical is None else safety_critical
        criteria = tuple(aspect_criteria)
        if not 1 <= len(criteria) <= 4:
            raise ValueError("root contract requires one to four aspects")
        root_contract_id = contract_id
        aspects = tuple(
            QuestionAspect(
                aspect_id=question_aspect_id(root_contract_id, ordinal, criterion),
                ordinal=ordinal,
                criterion=criterion,
            )
            for ordinal, criterion in enumerate(criteria)
        )
        parent_contract_id = None
        revision = 1
    else:
        if session_id != parent_contract.session_id:
            raise ValueError("follow-up session must match its parent contract")
        if any(value is not None for value in (dimension, selection_kind, aspect_criteria)):
            raise ValueError("follow-up authority fields are inherited, not caller supplied")
        if max_followups is not None and max_followups != parent_contract.max_followups:
            raise ValueError("follow-up cap and safety policy must match its parent")
        if safety_critical is not None and safety_critical != parent_contract.safety_critical:
            raise ValueError("follow-up cap and safety policy must match its parent")
        if not residual_aspects:
            raise ValueError("follow-up contract requires at least one residual aspect")
        parent_by_id = {item.aspect_id: item for item in parent_contract.aspects}
        aspects = tuple(residual_aspects)
        if any(parent_by_id.get(item.aspect_id) != item for item in aspects):
            raise ValueError("follow-up contract cannot add or mutate target aspects")
        if tuple(sorted(aspects, key=lambda item: item.ordinal)) != aspects:
            raise ValueError("residual aspects must retain root order")
        root_contract_id = parent_contract.root_contract_id
        parent_contract_id = parent_contract.contract_id
        revision = parent_contract.revision + 1
        dimension = parent_contract.dimension
        selection_kind = parent_contract.selection_kind
        resolved_max_followups = parent_contract.max_followups
        resolved_safety_critical = parent_contract.safety_critical

    if dimension is None or selection_kind is None:
        raise AssertionError("contract authority must be resolved before construction")
    question_digest = hashlib.sha256(question_text.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": QUESTION_CONTRACT_SCHEMA_VERSION,
        "contract_id": str(contract_id),
        "session_id": str(session_id),
        "question_message_id": str(question_message_id),
        "root_contract_id": str(root_contract_id),
        "parent_contract_id": str(parent_contract_id) if parent_contract_id else None,
        "revision": revision,
        "dimension": dimension,
        "selection_kind": selection_kind,
        "safety_critical": resolved_safety_critical,
        "max_followups": resolved_max_followups,
        "question_digest": question_digest,
        "aspects": [_aspect_payload(item) for item in aspects],
    }
    return QuestionContract(
        contract_id=contract_id,
        session_id=session_id,
        question_message_id=question_message_id,
        root_contract_id=root_contract_id,
        parent_contract_id=parent_contract_id,
        revision=revision,
        dimension=dimension,
        selection_kind=selection_kind,
        safety_critical=resolved_safety_critical,
        max_followups=resolved_max_followups,
        question_digest=question_digest,
        aspects=aspects,
        contract_digest=canonical_sha256(payload),
    )


def build_coverage_event(
    *,
    contract: QuestionContract,
    candidate: QuestionCoverageCandidate,
    message_contents: Mapping[UUID, str],
) -> QuestionCoverageEvent:
    """Ground a candidate and return the only representation safe to persist."""

    if candidate.contract_id != contract.contract_id:
        raise ValueError("coverage candidate is bound to a different contract")
    expected_ids = tuple(item.aspect_id for item in contract.aspects)
    candidate_by_id = {item.aspect_id: item for item in candidate.items}
    if set(candidate_by_id) != set(expected_ids) or len(candidate_by_id) != len(expected_ids):
        raise ValueError("coverage candidate must report every and only contracted aspect")
    answer_content = message_contents.get(candidate.answer_message_id)
    if answer_content is None:
        raise ValueError("bound answer message content is unavailable")

    event_items: list[CoverageEventItem] = []
    for aspect_id in expected_ids:
        item = candidate_by_id[aspect_id]
        refs: list[EvidenceRef] = []
        for span in item.evidence:
            if span.source_message_id != candidate.answer_message_id:
                raise ValueError("coverage evidence may reference only the current bound answer")
            raw = answer_content[span.start_char : span.end_char]
            if span.end_char > len(answer_content) or raw != span.quote:
                # Offset repair: the quote is a genuine substring of the answer
                # (the verifier already enforced this), but the model may have
                # miscounted Unicode code points.  Re-anchor deterministically
                # on the first occurrence so the persisted evidence locator is
                # always correct (真实后端复盘 2026-08)。
                position = answer_content.find(span.quote)
                if position == -1:
                    raise ValueError("coverage evidence quote is not grounded in the current answer")
                span = span.model_copy(
                    update={"start_char": position, "end_char": position + len(span.quote)}
                )
            refs.append(
                EvidenceRef(
                    source_message_id=span.source_message_id,
                    start_char=span.start_char,
                    end_char=span.end_char,
                    quote_sha256=hashlib.sha256(span.quote.encode("utf-8")).hexdigest(),
                )
            )
        event_items.append(CoverageEventItem(aspect_id=aspect_id, status=item.status, evidence=tuple(refs)))

    event_id = question_coverage_event_id(contract.contract_id, candidate.answer_message_id)
    payload = {
        "schema_version": QUESTION_COVERAGE_EVENT_SCHEMA_VERSION,
        "event_id": str(event_id),
        "session_id": str(contract.session_id),
        "contract_id": str(contract.contract_id),
        "root_contract_id": str(contract.root_contract_id),
        "answer_message_id": str(candidate.answer_message_id),
        "items": [_event_item_payload(item) for item in event_items],
    }
    return QuestionCoverageEvent(
        event_id=event_id,
        session_id=contract.session_id,
        contract_id=contract.contract_id,
        root_contract_id=contract.root_contract_id,
        answer_message_id=candidate.answer_message_id,
        items=tuple(event_items),
        event_digest=canonical_sha256(payload),
    )


def _aspect_payload(aspect: QuestionAspect) -> dict[str, object]:
    return {
        "aspect_id": str(aspect.aspect_id),
        "ordinal": aspect.ordinal,
        "criterion": aspect.criterion,
        "required": aspect.required,
    }


def _event_item_payload(item: CoverageEventItem) -> dict[str, object]:
    return {
        "aspect_id": str(item.aspect_id),
        "status": item.status.value,
        "evidence": [
            {
                "source_message_id": str(ref.source_message_id),
                "start_char": ref.start_char,
                "end_char": ref.end_char,
                "quote_sha256": ref.quote_sha256,
            }
            for ref in item.evidence
        ],
    }


def _contract_digest(contract: QuestionContract) -> str:
    return canonical_sha256(
        {
            "schema_version": contract.schema_version,
            "contract_id": str(contract.contract_id),
            "session_id": str(contract.session_id),
            "question_message_id": str(contract.question_message_id),
            "root_contract_id": str(contract.root_contract_id),
            "parent_contract_id": str(contract.parent_contract_id) if contract.parent_contract_id else None,
            "revision": contract.revision,
            "dimension": contract.dimension,
            "selection_kind": contract.selection_kind,
            "safety_critical": contract.safety_critical,
            "max_followups": contract.max_followups,
            "question_digest": contract.question_digest,
            "aspects": [_aspect_payload(item) for item in contract.aspects],
        }
    )


def _coverage_event_digest(event: QuestionCoverageEvent) -> str:
    return canonical_sha256(
        {
            "schema_version": event.schema_version,
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "contract_id": str(event.contract_id),
            "root_contract_id": str(event.root_contract_id),
            "answer_message_id": str(event.answer_message_id),
            "items": [
                {
                    "aspect_id": str(item.aspect_id),
                    "status": item.status.value,
                    "evidence": [
                        {
                            "source_message_id": str(ref.source_message_id),
                            "start_char": ref.start_char,
                            "end_char": ref.end_char,
                            "quote_sha256": ref.quote_sha256,
                        }
                        for ref in item.evidence
                    ],
                }
                for item in event.items
            ],
        }
    )


__all__ = [
    "AspectCoverageProjection",
    "ContractCoverageDisposition",
    "ContractCoverageProjection",
    "ContractReplyContext",
    "CoverageCandidateItem",
    "CoverageEvidenceCandidate",
    "CoverageEventItem",
    "CoverageStatus",
    "EvidenceRef",
    "QuestionAspect",
    "QuestionContract",
    "QuestionContractRef",
    "QuestionCoverageCandidate",
    "QuestionCoverageEvent",
    "build_coverage_event",
    "build_question_contract",
    "canonical_sha256",
    "question_aspect_id",
    "question_contract_id",
    "question_coverage_event_id",
]
