from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent_runtime.config import DEFAULT_GRAPH_VERSION
from app.core.exceptions import StateRecoveryRequiredError, ValidationError
from app.schemas.recovery import RecoveryRequest
from app.services import langgraph_recovery as recovery


def _meta(
    *,
    stage: str = "blocked",
    status: str = "blocked",
    recovery_status: str = "manual_required",
    blocked_reason: str | None = "reasoning_manual_required",
) -> recovery._SessionMeta:
    return recovery._SessionMeta(
        session_id=uuid.uuid4(),
        current_stage=stage,
        status=status,
        pending_review=False,
        state_version=7,
        recovery_status=recovery_status,
        blocked_reason=blocked_reason,
    )


def _proof(*, route: str = "reasoning_subgraph_v1") -> recovery.RecoveryCheckpointProof:
    return recovery.RecoveryCheckpointProof(
        exists=True,
        domain_state_version=7,
        command="advance",
        route=route,
        has_pending_interrupt=False,
    )


def test_reasoning_retry_returns_to_inquiry_for_fresh_version_bound_gate() -> None:
    source, target = recovery._resolve_target(
        _meta(),
        RecoveryRequest(action="retry_current_stage"),
        _proof(),
    )
    assert (source, target) == ("syndrome", "inquiry")


def test_triage_hold_cannot_be_reopened_by_runtime_recovery() -> None:
    with pytest.raises(StateRecoveryRequiredError):
        recovery._resolve_target(
            _meta(blocked_reason="triage_hold:emergency_referral"),
            RecoveryRequest(action="retry_current_stage"),
            _proof(route="intake_subgraph_v1"),
        )


def test_only_rollback_accepts_a_target_stage() -> None:
    with pytest.raises(ValidationError):
        recovery._resolve_target(
            _meta(stage="safety", status="active", recovery_status="recovering", blocked_reason=None),
            RecoveryRequest(action="retry_current_stage", target_stage="inquiry"),
            _proof(route="review_placeholder"),
        )


def test_checkpoint_rejects_unknown_top_level_payload_fields() -> None:
    meta = _meta(stage="safety", status="active", recovery_status="recovering", blocked_reason=None)
    snapshot = SimpleNamespace(
        values={
            "session_id": str(meta.session_id),
            "graph_version": "v1",
            "domain_state_version": meta.state_version,
            "command": "review",
            "route": "review_placeholder",
            "pending_interrupt": None,
            "formula": {"composition": []},
        }
    )
    with pytest.raises(StateRecoveryRequiredError):
        recovery._checkpoint_proof(snapshot, meta)


def _valid_checkpoint_values(meta: recovery._SessionMeta) -> dict[str, object]:
    return {
        "session_id": str(meta.session_id),
        "domain_state_version": meta.state_version,
        "command": "advance",
        "command_id": f"advance:{'a' * 64}",
        "graph_version": DEFAULT_GRAPH_VERSION,
        "run_id": str(uuid.uuid4()),
        "route": "reasoning_subgraph_v1",
        "intake_route": "",
        "reasoning_route": "manual_required",
        "gate_results": [
            {
                "gate_name": "reasoning_manual_required",
                "decision": "blocked",
                "policy_version": "reasoning-branch-policy.v1",
            }
        ],
        "artifact_refs": [
            {
                "kind": "formula_draft",
                "artifact_id": str(uuid.uuid4()),
                "revision": 1,
            }
        ],
        "pending_interrupt": None,
        "budget": {
            "remaining_steps": 0,
            "remaining_tokens": 0,
            "deadline_ref": "",
        },
        "last_error": None,
    }


def test_checkpoint_accepts_complete_reference_only_runtime_state() -> None:
    meta = _meta(
        stage="safety",
        status="active",
        recovery_status="recovering",
        blocked_reason=None,
    )

    proof = recovery._checkpoint_proof(
        SimpleNamespace(values=_valid_checkpoint_values(meta)),
        meta,
    )

    assert proof == recovery.RecoveryCheckpointProof(
        exists=True,
        domain_state_version=meta.state_version,
        command="advance",
        route="reasoning_subgraph_v1",
        has_pending_interrupt=False,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "gate_results",
            [
                {
                    "gate_name": "reasoning_manual_required",
                    "decision": "blocked",
                    "policy_version": "reasoning-branch-policy.v1",
                    "patient_symptom": "forbidden clinical payload",
                }
            ],
        ),
        (
            "artifact_refs",
            [{"kind": "formula_draft", "artifact_id": "not-a-uuid", "revision": 1}],
        ),
        (
            "artifact_refs",
            [
                {
                    "kind": "formula_draft",
                    "artifact_id": str(uuid.uuid4()),
                    "revision": 0,
                }
            ],
        ),
        (
            "budget",
            {
                "remaining_steps": True,
                "remaining_tokens": 0,
                "deadline_ref": "",
            },
        ),
        (
            "budget",
            {
                "remaining_steps": 0,
                "remaining_tokens": 0,
                "deadline_ref": "",
                "patient_name": "forbidden clinical payload",
            },
        ),
        (
            "last_error",
            {
                "code": "RECOVERY_FAILED",
                "trace_id": str(uuid.uuid4()),
                "detail": "sanitized",
                "prompt": "forbidden clinical payload",
            },
        ),
        (
            "pending_interrupt",
            {
                "kind": "doctor_review",
                "interrupt_id": "not-a-reference",
                "resume_token_ref": "review_submission_ref",
            },
        ),
        (
            "pending_interrupt",
            {
                "kind": "doctor_review",
                "interrupt_id": f"{uuid.uuid4()}:1",
                "resume_token_ref": "review_submission_ref",
                "formula": {"composition": []},
            },
        ),
    ],
)
def test_checkpoint_rejects_invalid_or_clinical_nested_control_payloads(
    field: str,
    value: object,
) -> None:
    meta = _meta(
        stage="safety",
        status="active",
        recovery_status="recovering",
        blocked_reason=None,
    )
    values = _valid_checkpoint_values(meta)
    values[field] = value

    with pytest.raises(StateRecoveryRequiredError):
        recovery._checkpoint_proof(SimpleNamespace(values=values), meta)


@pytest.mark.asyncio
async def test_invalid_nested_checkpoint_is_rejected_before_claim_or_graph_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _meta(
        stage="safety",
        status="active",
        recovery_status="recovering",
        blocked_reason=None,
    )
    values = _valid_checkpoint_values(meta)
    values["artifact_refs"] = [
        {
            "kind": "formula_draft",
            "artifact_id": str(uuid.uuid4()),
            "revision": 1,
            "clinical_payload": {"composition": []},
        }
    ]
    calls = {"claim": 0, "invoke": 0}

    class _Graph:
        async def aget_state(self, _config: object) -> SimpleNamespace:
            return SimpleNamespace(values=values)

    class _Runner:
        async def ainvoke(self, _state: object, *, config: object) -> dict[str, object]:
            del config
            calls["invoke"] += 1
            return {}

    async def forbidden_claim(**_kwargs: object) -> object:
        calls["claim"] += 1
        raise AssertionError("claim must not run")

    monkeypatch.setattr(recovery, "_claim_recovery", forbidden_claim)

    with pytest.raises(StateRecoveryRequiredError):
        await recovery.LangGraphRecoveryService(object())._recover_with_graph(  # type: ignore[arg-type]
            graph=_Graph(),
            runner=_Runner(),  # type: ignore[arg-type]
            config={},
            meta=meta,
            request=RecoveryRequest(action="retry_current_stage"),
            doctor_id=None,
            trace_id="checkpoint-preflight-test",
            command_key="recover:test",
            request_digest="a" * 64,
        )

    assert calls == {"claim": 0, "invoke": 0}


def test_request_marker_stores_only_reason_digest() -> None:
    marker = recovery._request_marker(
        RecoveryRequest(action="terminate", reason="operator-only free text"),
        doctor_id="doctor-1",
    )
    serialized = json.dumps(marker, ensure_ascii=False)
    assert "operator-only free text" not in serialized
    assert len(str(marker["reason_digest"])) == 64


def test_terminate_accepts_unknown_predecessor_without_restoring_it() -> None:
    source, target = recovery._resolve_target(
        _meta(blocked_reason="unknown_failure"),
        RecoveryRequest(action="terminate"),
        _proof(route="unknown_route"),
    )
    assert (source, target) == ("inquiry", "blocked")
