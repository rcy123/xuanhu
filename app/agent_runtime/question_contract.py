"""Pure R9 question-contract coverage fold.

No inquiry dimension is special-cased here.  Safety behavior is an immutable
property of the root contract supplied by the deterministic caller: an
unresolved safety-critical contract becomes ``manual_required`` at its cap,
while a non-safety contract becomes ``exhausted_partial``.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.schemas.question_contract import (
    AspectCoverageProjection,
    ContractCoverageDisposition,
    ContractCoverageProjection,
    CoverageStatus,
    EvidenceRef,
    QuestionContract,
    QuestionCoverageEvent,
)

_SATISFYING_STATUSES = frozenset({CoverageStatus.ADDRESSED, CoverageStatus.NOT_APPLICABLE})


class QuestionContractIntegrityError(ValueError):
    """Fail-closed signal for a malformed chain or coverage ledger."""


def evaluate_contract_coverage(
    contracts: Sequence[QuestionContract],
    events: Sequence[QuestionCoverageEvent],
) -> ContractCoverageProjection:
    """Fold an unordered contract chain and append-only events deterministically.

    The fold proves that every follow-up contains exactly the residual aspects
    from its predecessor.  Consequently a writer/model can neither silently
    drop an unanswered target nor expand the scope during a follow-up.
    """

    if not contracts:
        raise QuestionContractIntegrityError("at least one question contract is required")
    ordered_contracts = sorted(contracts, key=lambda item: item.revision)
    root = ordered_contracts[0]
    if root.revision != 1 or root.contract_id != root.root_contract_id:
        raise QuestionContractIntegrityError("contract chain must start with its root revision")

    _validate_contract_chain_identity(ordered_contracts, root)
    event_by_contract = _validate_events(events, ordered_contracts, root)

    root_aspects = root.aspects
    root_by_id = {aspect.aspect_id: aspect for aspect in root_aspects}
    unresolved: set[UUID] = set(root_by_id)
    satisfied: set[UUID] = set()
    latest_status: dict[UUID, CoverageStatus] = {
        aspect.aspect_id: CoverageStatus.UNANSWERED for aspect in root_aspects
    }
    evidence_by_aspect: dict[UUID, list[EvidenceRef]] = {aspect.aspect_id: [] for aspect in root_aspects}
    source_events_by_aspect: dict[UUID, list[UUID]] = {aspect.aspect_id: [] for aspect in root_aspects}

    previous_contract: QuestionContract | None = None
    previous_had_event = False
    for index, contract in enumerate(ordered_contracts):
        contract_aspect_ids = tuple(item.aspect_id for item in contract.aspects)
        expected_residual = tuple(item.aspect_id for item in root_aspects if item.aspect_id in unresolved)
        if contract.revision == 1:
            if contract_aspect_ids != tuple(root_by_id):
                raise QuestionContractIntegrityError("root contract aspect order is inconsistent")
        else:
            if previous_contract is None:
                raise QuestionContractIntegrityError("a follow-up requires a coverage event for its parent")
            if not previous_had_event:
                # A follow-up may appear without an event on its parent only
                # when it repeats the parent's *full* aspect set: nothing was
                # answered, so the residual is definitionally the parent's own
                # aspects.  This lets the graph re-ask a still-awaiting
                # question (e.g. after a social/abstained reply) without
                # abandoning the contract or fabricating coverage, and still
                # forbids silently dropping or expanding an unanswered target.
                if contract_aspect_ids != tuple(item.aspect_id for item in previous_contract.aspects):
                    raise QuestionContractIntegrityError(
                        "a follow-up after an unanswered parent must repeat the parent's full aspect set"
                    )
            elif contract_aspect_ids != expected_residual:
                raise QuestionContractIntegrityError("follow-up targets must exactly equal the prior residual")
            for aspect in contract.aspects:
                if root_by_id.get(aspect.aspect_id) != aspect:
                    raise QuestionContractIntegrityError("follow-up mutated a frozen root aspect")

        event = event_by_contract.get(contract.contract_id)
        previous_contract = contract
        previous_had_event = event is not None
        if event is None:
            # Only the latest contract may await an answer — except that a
            # contract may be superseded *without* an answer by a full-repeat
            # follow-up: the next revision repeats its entire aspect set, so
            # nothing unanswered is silently dropped (see the sibling check in
            # the follow-up branch above).  This lets the graph re-ask a
            # still-awaiting question after a social/abstained reply.
            next_contract = (
                ordered_contracts[index + 1] if index + 1 < len(ordered_contracts) else None
            )
            if next_contract is not None and contract_aspect_ids != tuple(
                item.aspect_id for item in next_contract.aspects
            ):
                raise QuestionContractIntegrityError("only the latest contract may await an answer")
            continue

        event_ids = tuple(item.aspect_id for item in event.items)
        if event_ids != contract_aspect_ids:
            raise QuestionContractIntegrityError("coverage event must report contracted aspects in contract order")
        for item in event.items:
            if any(ref.source_message_id != event.answer_message_id for ref in item.evidence):
                raise QuestionContractIntegrityError("persisted evidence must reference the bound answer")
            latest_status[item.aspect_id] = item.status
            source_events_by_aspect[item.aspect_id].append(event.event_id)
            for ref in item.evidence:
                if ref not in evidence_by_aspect[item.aspect_id]:
                    evidence_by_aspect[item.aspect_id].append(ref)
            if item.status in _SATISFYING_STATUSES:
                unresolved.discard(item.aspect_id)
                satisfied.add(item.aspect_id)

    latest = ordered_contracts[-1]
    unresolved_ids = tuple(item.aspect_id for item in root_aspects if item.aspect_id in unresolved)
    satisfied_ids = tuple(item.aspect_id for item in root_aspects if item.aspect_id in satisfied)
    residual = tuple(root_by_id[aspect_id] for aspect_id in unresolved_ids)
    if not unresolved_ids:
        disposition = ContractCoverageDisposition.SATISFIED
    elif latest.revision - 1 < root.max_followups:
        disposition = ContractCoverageDisposition.OPEN
    elif root.safety_critical:
        disposition = ContractCoverageDisposition.MANUAL_REQUIRED
    else:
        disposition = ContractCoverageDisposition.EXHAUSTED_PARTIAL

    joint_evidence = tuple(
        AspectCoverageProjection(
            aspect_id=aspect.aspect_id,
            latest_status=latest_status[aspect.aspect_id],
            evidence=tuple(evidence_by_aspect[aspect.aspect_id]),
            source_event_ids=tuple(source_events_by_aspect[aspect.aspect_id]),
        )
        for aspect in root_aspects
    )
    return ContractCoverageProjection(
        root_contract_id=root.contract_id,
        latest_contract_id=latest.contract_id,
        latest_revision=latest.revision,
        disposition=disposition,
        satisfied_aspect_ids=satisfied_ids,
        unresolved_aspect_ids=unresolved_ids,
        residual_aspects=residual,
        joint_evidence=joint_evidence,
        followups_used=latest.revision - 1,
        max_followups=root.max_followups,
        event_count=len(events),
    )


def _validate_contract_chain_identity(
    contracts: Sequence[QuestionContract],
    root: QuestionContract,
) -> None:
    if len(contracts) > root.max_followups + 1:
        raise QuestionContractIntegrityError("contract chain exceeds its configured cap")
    revisions = [item.revision for item in contracts]
    if revisions != list(range(1, len(contracts) + 1)):
        raise QuestionContractIntegrityError("contract revisions must be unique and contiguous")
    contract_ids = [item.contract_id for item in contracts]
    if len(contract_ids) != len(set(contract_ids)):
        raise QuestionContractIntegrityError("contract ids must be unique")

    previous: QuestionContract | None = None
    for contract in contracts:
        if (
            contract.session_id != root.session_id
            or contract.root_contract_id != root.contract_id
            or contract.dimension != root.dimension
            or contract.selection_kind != root.selection_kind
            or contract.safety_critical != root.safety_critical
            or contract.max_followups != root.max_followups
        ):
            raise QuestionContractIntegrityError("contract chain changed immutable root authority")
        if previous is not None and contract.parent_contract_id != previous.contract_id:
            raise QuestionContractIntegrityError("follow-up parent does not match the prior revision")
        previous = contract


def _validate_events(
    events: Sequence[QuestionCoverageEvent],
    contracts: Sequence[QuestionContract],
    root: QuestionContract,
) -> dict[UUID, QuestionCoverageEvent]:
    contract_ids = {item.contract_id for item in contracts}
    event_ids = [item.event_id for item in events]
    answer_ids = [item.answer_message_id for item in events]
    event_contract_ids = [item.contract_id for item in events]
    if len(event_ids) != len(set(event_ids)):
        raise QuestionContractIntegrityError("coverage event ids must be unique")
    if len(answer_ids) != len(set(answer_ids)):
        raise QuestionContractIntegrityError("one answer cannot cover multiple contracts")
    if len(event_contract_ids) != len(set(event_contract_ids)):
        raise QuestionContractIntegrityError("a contract accepts only one coverage event")

    event_by_contract: dict[UUID, QuestionCoverageEvent] = {}
    for event in events:
        if event.contract_id not in contract_ids:
            raise QuestionContractIntegrityError("coverage event references an unknown contract")
        if event.session_id != root.session_id or event.root_contract_id != root.contract_id:
            raise QuestionContractIntegrityError("coverage event crossed a session or root boundary")
        event_by_contract[event.contract_id] = event
    return event_by_contract


def contract_projection_metadata(projection: ContractCoverageProjection) -> dict[str, object]:
    """Return bounded public-safe counters; criteria and evidence stay private."""

    return {
        "schema_version": projection.schema_version,
        "root_contract_id": str(projection.root_contract_id),
        "latest_contract_id": str(projection.latest_contract_id),
        "latest_revision": projection.latest_revision,
        "disposition": projection.disposition.value,
        "satisfied_count": len(projection.satisfied_aspect_ids),
        "remaining_count": len(projection.unresolved_aspect_ids),
        "followups_used": projection.followups_used,
        "max_followups": projection.max_followups,
        "event_count": projection.event_count,
    }


__all__ = [
    "QuestionContractIntegrityError",
    "contract_projection_metadata",
    "evaluate_contract_coverage",
]
