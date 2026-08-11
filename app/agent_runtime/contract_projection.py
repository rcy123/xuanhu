"""Deterministic R9 contract-ledger projection onto inquiry dimensions.

The completeness policy consumes only the three derived dimension tuples; the
composer consumes the per-root coverage projections to force a residual
follow-up.  No model decision enters here: the projection is a pure fold of the
persisted contract and coverage-event ledger.

Safety-critical roots are never projected.  Their collection status is owned by
``SafetyProfile`` and the completeness safety branch, not by the question
contract ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.agent_runtime.question_contract import (
    QuestionContractIntegrityError,
    evaluate_contract_coverage,
)
from app.schemas.completeness import InquiryDimension
from app.schemas.question_contract import (
    ContractCoverageDisposition,
    ContractCoverageProjection,
    QuestionContract,
    QuestionCoverageEvent,
)

CONTRACT_PROJECTION_SCHEMA_VERSION: str = "contract-dimension-projection.v1"


class ContractRootProjection(BaseModel):
    """One root contract chain and its deterministic fold result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_contract_id: UUID
    dimension: InquiryDimension
    coverage: ContractCoverageProjection


class ContractDimensionProjection(BaseModel):
    """Dimension-level view consumed by the completeness policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CONTRACT_PROJECTION_SCHEMA_VERSION
    roots: tuple[ContractRootProjection, ...] = ()
    # open: a required contract still has residual aspects and therefore holds
    # the dimension even when a coarse Observation already exists;
    # resolved: the contract reached a terminal outcome and must not be asked
    # again merely because the legacy fact-key projection is sparse;
    # partial: a resolved non-safety contract ended through unavailable/cap.
    open_dimensions: tuple[InquiryDimension, ...] = ()
    resolved_dimensions: tuple[InquiryDimension, ...] = ()
    partial_dimensions: tuple[InquiryDimension, ...] = ()

    @classmethod
    def _from_roots(cls, roots: tuple[ContractRootProjection, ...]) -> ContractDimensionProjection:
        open_set: set[InquiryDimension] = set()
        resolved_set: set[InquiryDimension] = set()
        partial_set: set[InquiryDimension] = set()
        for root in roots:
            if root.coverage.disposition is ContractCoverageDisposition.OPEN:
                open_set.add(root.dimension)
            else:
                resolved_set.add(root.dimension)
                if root.coverage.disposition is ContractCoverageDisposition.EXHAUSTED_PARTIAL:
                    partial_set.add(root.dimension)
        return cls(
            roots=roots,
            open_dimensions=tuple(sorted(open_set, key=lambda item: item.value)),
            resolved_dimensions=tuple(sorted(resolved_set, key=lambda item: item.value)),
            partial_dimensions=tuple(sorted(partial_set, key=lambda item: item.value)),
        )


def project_contract_dimensions(
    contracts: Sequence[QuestionContract],
    events: Sequence[QuestionCoverageEvent],
) -> ContractDimensionProjection:
    """Fold every root chain independently and group by inquiry dimension.

    A malformed chain fails closed on write (the reducer rejects the commit),
    so a damaged ledger is treated here as ``no projection`` rather than raising
    inside the completeness path: the legacy fact-key projection still applies
    and intake remains safe.
    """

    grouped: dict[UUID, tuple[list[QuestionContract], list[QuestionCoverageEvent]]] = {}
    for contract in contracts:
        grouped.setdefault(contract.root_contract_id, ([], []))[0].append(contract)
    for event in events:
        grouped.setdefault(event.root_contract_id, ([], []))[1].append(event)

    roots: list[ContractRootProjection] = []
    for root_contracts, root_events in grouped.values():
        root = min(root_contracts, key=lambda item: item.revision)
        if root.safety_critical:
            continue
        try:
            coverage = evaluate_contract_coverage(root_contracts, root_events)
        except QuestionContractIntegrityError:
            continue
        try:
            dimension = InquiryDimension(root.dimension)
        except ValueError:
            continue
        roots.append(
            ContractRootProjection(
                root_contract_id=root.contract_id,
                dimension=dimension,
                coverage=coverage,
            )
        )
    return ContractDimensionProjection._from_roots(tuple(roots))


__all__ = [
    "CONTRACT_PROJECTION_SCHEMA_VERSION",
    "ContractDimensionProjection",
    "ContractRootProjection",
    "project_contract_dimensions",
]
