"""Authoritative Session Read Model for durable L3/L4 results.

The adapter deliberately ignores ``ConsultSession.state_snapshot``.  Clinical
results are exposed only when a current ArtifactRevision, its payload, the
completed producing GraphRun, and the persisted verifier GateResult agree.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.repository import artifact_payload_digest
from app.models.consult import ConsultSession
from app.models.domain import ArtifactRevision, ArtifactRevisionPayload, GateResult, GraphRun, SafetyFactAssertion
from app.schemas.formula import FormulaComposition, FormulaDraft
from app.schemas.session_read_model import (
    SessionArtifactReadModelV1,
    SessionGateReadModelV1,
    SessionGraphReadModelV1,
    SessionReadModelV1,
    SessionReadProjection,
    SessionUnresolvedReadModelV1,
)
from app.schemas.syndrome import SyndromeDraft

_SYNDROME_ARTIFACT_TYPE = "syndrome_draft"
_FORMULA_ARTIFACT_TYPE = "formula_draft"
_ARTIFACT_TYPES = (_SYNDROME_ARTIFACT_TYPE, _FORMULA_ARTIFACT_TYPE)

_SYNDROME_PAYLOAD_SCHEMA_VERSION = "syndrome-artifact-payload.v1"
_FORMULA_PAYLOAD_SCHEMA_VERSION = "formula-artifact-payload.v1"

_TRIAGE_POLICY_VERSION = "triage-red-flag.v1"
_COMPLETENESS_POLICY_VERSION = "completeness-policy.v1"
_SYNDROME_GATE_NAME = "syndrome_verifier"
# RAG 启用时 verifier gate 使用 rag.v1 policy（与 syndrome_draft 的 policy 一致），
# 未启用时是 no-rag.v1 —— read model 必须两者都接受，否则 RAG 会话的
# syndrome/formula artifact 无法通过信任校验 → 前端方子为空。
_SYNDROME_GATE_POLICIES = ("syndrome-draft-policy.no-rag.v1", "syndrome-draft-policy.rag.v1")
_FORMULA_GATE_NAME = "formula_consistency"
_FORMULA_GATE_POLICY = "formula-consistency-policy.v1"
_CANONICAL_GATE_NAME = "canonical_verifier_chain"
_CANONICAL_GATE_POLICY = "l2-4-v1"

_SAFETY_FIELD_TO_COMPLETENESS_DIMENSION = {
    "allergy": "safety.allergy_status",
    "medications": "safety.medication_status",
    "major_conditions": "safety.major_condition_status",
    "pregnancy": "safety.pregnancy_status",
    "lactation": "safety.lactation_status",
}
_TRIAGE_OWNED_SAFETY_FIELDS = frozenset({"red_flag"})


@dataclass(frozen=True, slots=True)
class GateAuthorityRow:
    gate: GateResult
    graph_run: GraphRun | None


@dataclass(frozen=True, slots=True)
class ArtifactAuthorityRow:
    artifact: ArtifactRevision
    payload: ArtifactRevisionPayload | None
    graph_run: GraphRun | None


@dataclass(frozen=True, slots=True)
class _TrustedArtifact:
    projection: SessionArtifactReadModelV1
    typed_output: SyndromeDraft | FormulaDraft


def _expected_artifact_id(session_id: uuid.UUID, artifact_type: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{artifact_type}:{session_id}")


def _gate_projection(row: GateResult) -> SessionGateReadModelV1:
    return SessionGateReadModelV1(
        gate_id=row.id,
        graph_run_id=row.graph_run_id,
        gate_name=row.gate_name,
        policy_version=row.policy_version,
        input_state_version=row.input_state_version,
        decision=cast(Literal["passed", "failed", "blocked"], row.decision),
        details=dict(row.details) if isinstance(row.details, dict) else None,
    )


def _valid_gate_authority(row: GateAuthorityRow, session_id: uuid.UUID) -> bool:
    gate = row.gate
    if gate.session_id != session_id:
        return False
    if gate.graph_run_id is None:
        return row.graph_run is None and gate.gate_name == "triage"
    run = row.graph_run
    return bool(
        run is not None and run.id == gate.graph_run_id and run.session_id == session_id and run.status == "completed"
    )


def _authoritative_intake_gates(
    session_id: uuid.UUID,
    rows: tuple[GateAuthorityRow, ...],
) -> tuple[GateResult, ...]:
    """Select one unambiguous latest L3 gate bundle.

    Normal intake commits triage and completeness in the same completed run.
    Initial deterministic red-flag seeding may persist a standalone triage gate
    without a GraphRun, which remains a valid authority source.
    """

    eligible: list[GateResult] = []
    for row in rows:
        gate = row.gate
        if gate.gate_name not in {"triage", "completeness"}:
            continue
        if not _valid_gate_authority(row, session_id):
            continue
        details = gate.details
        if not isinstance(details, dict) or not isinstance(details.get("disposition"), str):
            continue
        if gate.gate_name == "triage":
            if gate.policy_version != _TRIAGE_POLICY_VERSION:
                continue
            disposition = details["disposition"]
            if disposition not in {"continue", "emergency_referral", "manual_review"}:
                continue
            candidate_count = details.get("candidate_count")
            if (
                not isinstance(candidate_count, int)
                or isinstance(candidate_count, bool)
                or candidate_count < 0
                or (disposition == "continue") != (candidate_count == 0)
            ):
                continue
            expected = "passed" if disposition == "continue" else "blocked"
            if gate.decision != expected:
                continue
        else:
            if gate.policy_version != _COMPLETENESS_POLICY_VERSION:
                continue
            expected_by_disposition = {
                "ready": "passed",
                "incomplete": "failed",
                "conflict": "failed",
                "stagnated": "blocked",
                "triage_blocked": "blocked",
            }
            if gate.decision != expected_by_disposition.get(details["disposition"]):
                continue
            if any(
                name in details and not isinstance(details[name], list)
                for name in ("covered_dimensions", "missing_required", "conflicting_dimensions")
            ):
                continue
        eligible.append(gate)

    if not eligible:
        return ()
    latest_version = max(item.input_state_version for item in eligible)
    latest = [item for item in eligible if item.input_state_version == latest_version]
    groups: dict[uuid.UUID | None, list[GateResult]] = {}
    for gate in latest:
        groups.setdefault(gate.graph_run_id, []).append(gate)

    complete_groups = [
        group
        for group in groups.values()
        if len(group) == 2 and {item.gate_name for item in group} == {"triage", "completeness"}
    ]
    if len(complete_groups) == 1:
        selected = complete_groups[0]
    elif len(groups) == 1:
        selected = next(iter(groups.values()))
        if len(selected) != 1 or selected[0].gate_name != "triage":
            return ()
    else:
        return ()
    return tuple(sorted(selected, key=lambda item: 0 if item.gate_name == "triage" else 1))


def _artifact_gate(
    artifact: ArtifactRevision,
    payload: ArtifactRevisionPayload,
    rows: tuple[GateAuthorityRow, ...],
    *,
    gate_name: str,
    gate_policies: tuple[str, ...],
) -> GateResult | None:
    matching = [
        row.gate
        for row in rows
        if _valid_gate_authority(row, artifact.session_id)
        and row.gate.graph_run_id == artifact.produced_by_run_id
        and row.gate.input_state_version == artifact.input_state_version
        and row.gate.gate_name == gate_name
        and row.gate.policy_version in gate_policies
        and row.gate.decision == "passed"
        and isinstance(row.gate.details, dict)
        and row.gate.details.get("artifact_digest") == payload.content_digest
    ]
    canonical = [
        row.gate
        for row in rows
        if _valid_gate_authority(row, artifact.session_id)
        and row.gate.graph_run_id == artifact.produced_by_run_id
        and row.gate.input_state_version == artifact.input_state_version
        and row.gate.gate_name == _CANONICAL_GATE_NAME
        and row.gate.policy_version == _CANONICAL_GATE_POLICY
        and row.gate.decision == "passed"
    ]
    if len(matching) != 1 or len(canonical) != 1:
        return None
    return matching[0]


def _trusted_artifact(
    session: ConsultSession,
    row: ArtifactAuthorityRow,
    gates: tuple[GateAuthorityRow, ...],
) -> _TrustedArtifact | None:
    artifact = row.artifact
    payload = row.payload
    run = row.graph_run
    if (
        artifact.session_id != session.id
        or artifact.artifact_type not in _ARTIFACT_TYPES
        or artifact.artifact_id != _expected_artifact_id(session.id, artifact.artifact_type)
        or artifact.status != "current"
        or artifact.input_state_version >= session.state_version
        or payload is None
        or run is None
        or run.id != artifact.produced_by_run_id
        or run.session_id != session.id
        or run.input_state_version != artifact.input_state_version
        or run.status != "completed"
        or payload.artifact_revision_id != artifact.id
        or payload.session_id != artifact.session_id
        or payload.artifact_id != artifact.artifact_id
        or payload.revision != artifact.revision
    ):
        return None

    raw_payload = payload.payload
    if not isinstance(raw_payload, dict):
        return None
    try:
        digest = artifact_payload_digest(payload.payload_schema_version, cast(dict[str, object], raw_payload))
    except (TypeError, ValueError, OverflowError):
        return None
    if digest != payload.content_digest:
        return None

    if artifact.artifact_type == _SYNDROME_ARTIFACT_TYPE:
        expected_schema = _SYNDROME_PAYLOAD_SCHEMA_VERSION
        gate_name = _SYNDROME_GATE_NAME
        gate_policies = _SYNDROME_GATE_POLICIES
        output_type: type[SyndromeDraft] | type[FormulaDraft] = SyndromeDraft
    else:
        expected_schema = _FORMULA_PAYLOAD_SCHEMA_VERSION
        gate_name = _FORMULA_GATE_NAME
        gate_policies = (_FORMULA_GATE_POLICY,)
        output_type = FormulaDraft
    if payload.payload_schema_version != expected_schema or raw_payload.get("kind") != artifact.artifact_type:
        return None
    raw_input = raw_payload.get("input_payload")
    raw_output = raw_payload.get("output")
    verification = raw_payload.get("verification")
    if (
        not isinstance(raw_input, dict)
        or raw_input.get("state_version") != artifact.input_state_version
        or not isinstance(raw_output, dict)
        or not isinstance(verification, dict)
        or verification.get("passed") is not True
    ):
        return None
    if artifact.artifact_type == _FORMULA_ARTIFACT_TYPE:
        consistency = raw_payload.get("consistency")
        if not isinstance(consistency, dict) or consistency.get("passed") is not True:
            return None

    gate = _artifact_gate(artifact, payload, gates, gate_name=gate_name, gate_policies=gate_policies)
    if gate is None:
        return None
    try:
        typed_output = output_type.model_validate(raw_output)
    except (ValidationError, TypeError, ValueError):
        return None
    if typed_output.review_required is not True:
        return None
    unresolved = tuple(str(item) for item in typed_output.missing_inputs)
    projection = SessionArtifactReadModelV1(
        artifact_id=artifact.artifact_id,
        artifact_type=cast(Literal["syndrome_draft", "formula_draft"], artifact.artifact_type),
        revision=artifact.revision,
        input_state_version=artifact.input_state_version,
        status="current",
        produced_by_run_id=artifact.produced_by_run_id,
        payload_schema_version=payload.payload_schema_version,
        content_digest=payload.content_digest,
        decision=cast(
            Literal["completed", "needs_more_info", "abstained"],
            typed_output.decision.value,
        ),
        evidence_mode=typed_output.evidence_mode,
        review_required=typed_output.review_required,
        unresolved=unresolved,
        verification_gate=_gate_projection(gate),
        output=typed_output.model_dump(mode="json"),
    )
    return _TrustedArtifact(projection=projection, typed_output=typed_output)


def _unresolved_from_gates(gates: tuple[GateResult, ...]) -> list[SessionUnresolvedReadModelV1]:
    unresolved: list[SessionUnresolvedReadModelV1] = []
    for gate in gates:
        details = gate.details or {}
        if gate.gate_name == "triage" and details.get("disposition") != "continue":
            unresolved.append(
                SessionUnresolvedReadModelV1(
                    source="triage",
                    kind="red_flag",
                    key=str(details.get("disposition") or "blocked"),
                )
            )
        if gate.gate_name != "completeness":
            continue
        for item in details.get("missing_required", []):
            if isinstance(item, str) and item:
                unresolved.append(
                    SessionUnresolvedReadModelV1(source="completeness", kind="missing_required", key=item[:128])
                )
        for item in details.get("conflicting_dimensions", []):
            key = item.get("dimension") if isinstance(item, dict) else item
            if isinstance(key, str) and key:
                unresolved.append(SessionUnresolvedReadModelV1(source="completeness", kind="conflict", key=key[:128]))
    return unresolved


def _sufficiency_report(gates: tuple[GateResult, ...]) -> dict[str, Any] | None:
    gate = next((item for item in gates if item.gate_name == "completeness"), None)
    if gate is None or not isinstance(gate.details, dict):
        return None
    details = gate.details
    covered = details.get("covered_dimensions")
    missing = details.get("missing_required")
    if not isinstance(covered, list) or not isinstance(missing, list):
        return None
    return {
        "sufficient": details.get("disposition") == "ready" and gate.decision == "passed",
        "covered": [item for item in covered if isinstance(item, str)],
        "missing": [item for item in missing if isinstance(item, str)],
        "suggestions": [],
        "next_question": None,
        "disposition": details.get("disposition"),
        "policy_version": gate.policy_version,
        "input_state_version": gate.input_state_version,
    }


def _legacy_syndrome(output: SyndromeDraft | FormulaDraft | None) -> dict[str, Any] | None:
    if not isinstance(output, SyndromeDraft) or output.decision.value != "completed":
        return None
    if output.syndrome is None or output.treatment_principle is None:
        return None
    return {
        "syndrome": output.syndrome,
        "syndrome_basis": [item.claim for item in output.syndrome_basis],
        "differential": [item.claim for item in output.differential],
        "treatment_principle": output.treatment_principle,
        "citations": [],
        "confidence": output.confidence,
    }


def _legacy_formula(formula: FormulaComposition | None) -> dict[str, Any] | None:
    if formula is None:
        return None
    return {
        "name": formula.name,
        "composition": [item.model_dump(mode="json") for item in formula.composition],
        "source": None,
        "rationale": formula.rationale,
        "citations": [],
    }


def project_session_read_model(
    session: ConsultSession,
    *,
    latest_graph_run: GraphRun | None = None,
    gates: tuple[GateAuthorityRow, ...] = (),
    artifacts: tuple[ArtifactAuthorityRow, ...] = (),
) -> SessionReadProjection:
    """Pure projection used by the service and integrity regression tests."""

    graph = SessionGraphReadModelV1(revision=session.state_version)
    if (
        session.agent_runtime == "langgraph"
        and latest_graph_run is not None
        and latest_graph_run.session_id == session.id
    ):
        graph = SessionGraphReadModelV1(
            graph_run_id=latest_graph_run.id,
            graph_version=latest_graph_run.graph_version,
            revision=session.state_version,
            input_state_version=latest_graph_run.input_state_version,
            status=cast(
                Literal["running", "completed", "failed", "cancelled"],
                latest_graph_run.status,
            ),
        )
    if session.agent_runtime != "langgraph":
        return SessionReadProjection(read_model=SessionReadModelV1(agent_runtime="legacy", graph=graph))

    intake_gates = _authoritative_intake_gates(session.id, gates)
    unresolved = _unresolved_from_gates(intake_gates)
    trusted: dict[str, _TrustedArtifact] = {}
    rows_by_type: dict[str, list[ArtifactAuthorityRow]] = {name: [] for name in _ARTIFACT_TYPES}
    for row in artifacts:
        if row.artifact.artifact_type in rows_by_type:
            rows_by_type[row.artifact.artifact_type].append(row)
    for artifact_type, rows in rows_by_type.items():
        if not rows:
            continue
        if len(rows) != 1:
            unresolved.append(
                SessionUnresolvedReadModelV1(source="read_model", kind="artifact_unavailable", key=artifact_type)
            )
            continue
        candidate = _trusted_artifact(session, rows[0], gates)
        if candidate is None:
            unresolved.append(
                SessionUnresolvedReadModelV1(source="read_model", kind="artifact_unavailable", key=artifact_type)
            )
            continue
        trusted[artifact_type] = candidate

    syndrome = trusted.get(_SYNDROME_ARTIFACT_TYPE)
    formula = trusted.get(_FORMULA_ARTIFACT_TYPE)
    if formula is not None:
        raw_formula_input = rows_by_type[_FORMULA_ARTIFACT_TYPE][0].payload
        formula_input_payload = (
            raw_formula_input.payload.get("input_payload") if raw_formula_input is not None else None
        )
        upstream = formula_input_payload.get("syndrome_draft") if isinstance(formula_input_payload, dict) else None
        expected = syndrome.typed_output.model_dump(mode="json") if syndrome is not None else None
        if (
            not isinstance(syndrome, _TrustedArtifact)
            or syndrome.typed_output.decision.value != "completed"
            or upstream != expected
        ):
            trusted.pop(_FORMULA_ARTIFACT_TYPE, None)
            formula = None
            unresolved.append(
                SessionUnresolvedReadModelV1(
                    source="read_model", kind="artifact_unavailable", key=_FORMULA_ARTIFACT_TYPE
                )
            )

    for artifact_type in _ARTIFACT_TYPES:
        item = trusted.get(artifact_type)
        if item is None:
            continue
        for key in item.projection.unresolved:
            safe_key = key.strip()[:128] or "unspecified"
            unresolved.append(
                SessionUnresolvedReadModelV1(
                    source=cast(Literal["syndrome_draft", "formula_draft"], artifact_type),
                    kind="missing_input",
                    key=safe_key,
                )
            )

    artifact_projections = tuple(trusted[name].projection for name in _ARTIFACT_TYPES if name in trusted)
    evidence_mode = artifact_projections[-1].evidence_mode if artifact_projections else None
    review_required = any(item.review_required for item in artifact_projections)
    read_model = SessionReadModelV1(
        agent_runtime="langgraph",
        graph=graph,
        gates=tuple(_gate_projection(item) for item in intake_gates),
        artifacts=artifact_projections,
        evidence_mode=evidence_mode,
        review_required=review_required,
        unresolved=tuple(unresolved),
    )

    syndrome_output = syndrome.typed_output if syndrome is not None else None
    formula_output = formula.typed_output if formula is not None else None
    base_formula = None
    modified_formula = None
    modifications = None
    if isinstance(formula_output, FormulaDraft) and formula_output.decision.value == "completed":
        base_formula = _legacy_formula(formula_output.base_formula)
        modified_formula = _legacy_formula(formula_output.candidate_formula)
        modifications = []
        for modification in formula_output.modifications:
            legacy_item = modification.model_dump(mode="json", exclude={"basis"})
            if legacy_item["action"] == "dose_adjust":
                legacy_item["action"] = "adjust"
            modifications.append(legacy_item)
    return SessionReadProjection(
        read_model=read_model,
        sufficiency_report=_sufficiency_report(intake_gates),
        syndrome_result=_legacy_syndrome(syndrome_output),
        base_formula=base_formula,
        modified_formula=modified_formula,
        modifications=modifications,
    )


def _merge_pending_safety_assertions(
    projection: SessionReadProjection,
    pending_fields: tuple[str, ...],
) -> SessionReadProjection:
    """Project confirmation work without changing clinical-review semantics.

    A proposed safety assertion is deliberately not authoritative, so the
    completeness gate may still report its dimension as missing.  For the
    operator-facing unresolved list, however, the more actionable confirmation
    item replaces that duplicate missing marker until the assertion is settled.
    """

    fields = tuple(
        sorted(set(pending_fields) - _TRIAGE_OWNED_SAFETY_FIELDS)
    )
    if not fields:
        return projection
    pending_dimensions = {
        dimension
        for field_name in fields
        if (dimension := _SAFETY_FIELD_TO_COMPLETENESS_DIMENSION.get(field_name)) is not None
    }
    unresolved = tuple(
        item
        for item in projection.read_model.unresolved
        if not (item.source == "completeness" and item.kind == "missing_required" and item.key in pending_dimensions)
        and not (item.source == "safety_confirmation" and item.kind == "unconfirmed_safety_fact" and item.key in fields)
    ) + tuple(
        SessionUnresolvedReadModelV1(
            source="safety_confirmation",
            kind="unconfirmed_safety_fact",
            key=field_name,
        )
        for field_name in fields
    )
    read_model = projection.read_model.model_copy(update={"unresolved": unresolved})
    return projection.model_copy(update={"read_model": read_model})


class SessionReadModelService:
    """Load authoritative persistence rows and build a versioned projection."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def build(self, session: ConsultSession) -> SessionReadProjection:
        if session.agent_runtime != "langgraph":
            return project_session_read_model(session)

        latest_graph_run = await self._db.scalar(
            select(GraphRun)
            .where(GraphRun.session_id == session.id)
            .order_by(
                GraphRun.input_state_version.desc(),
                GraphRun.created_at.desc(),
                GraphRun.id.desc(),
            )
            .limit(1)
        )
        gate_result = await self._db.execute(
            select(GateResult, GraphRun)
            .outerjoin(GraphRun, GateResult.graph_run_id == GraphRun.id)
            .where(
                GateResult.session_id == session.id,
                GateResult.gate_name.in_(
                    (
                        "triage",
                        "completeness",
                        _CANONICAL_GATE_NAME,
                        _SYNDROME_GATE_NAME,
                        _FORMULA_GATE_NAME,
                    )
                ),
            )
            .order_by(GateResult.input_state_version, GateResult.created_at, GateResult.id)
        )
        gate_rows = tuple(GateAuthorityRow(gate=gate, graph_run=graph_run) for gate, graph_run in gate_result.all())
        artifact_result = await self._db.execute(
            select(ArtifactRevision, ArtifactRevisionPayload, GraphRun)
            .outerjoin(
                ArtifactRevisionPayload,
                ArtifactRevisionPayload.artifact_revision_id == ArtifactRevision.id,
            )
            .outerjoin(GraphRun, ArtifactRevision.produced_by_run_id == GraphRun.id)
            .where(
                ArtifactRevision.session_id == session.id,
                ArtifactRevision.artifact_type.in_(_ARTIFACT_TYPES),
                ArtifactRevision.status == "current",
            )
            .order_by(ArtifactRevision.artifact_type, ArtifactRevision.revision.desc())
        )
        artifact_rows = tuple(
            ArtifactAuthorityRow(artifact=artifact, payload=payload, graph_run=graph_run)
            for artifact, payload, graph_run in artifact_result.all()
        )
        projection = project_session_read_model(
            session,
            latest_graph_run=latest_graph_run,
            gates=gate_rows,
            artifacts=artifact_rows,
        )
        pending_fields = tuple(
            sorted(
                set(
                    await self._db.scalars(
                        select(SafetyFactAssertion.field_name).where(
                            SafetyFactAssertion.session_id == session.id,
                            SafetyFactAssertion.status == "proposed",
                        )
                    )
                )
            )
        )
        if not pending_fields:
            return projection
        return _merge_pending_safety_assertions(projection, pending_fields)


__all__ = [
    "ArtifactAuthorityRow",
    "GateAuthorityRow",
    "SessionReadModelService",
    "project_session_read_model",
]
