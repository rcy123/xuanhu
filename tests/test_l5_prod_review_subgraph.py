from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent_runtime.commands import NODE_REVIEW_PLACEHOLDER, XuanhuCommand
from app.agent_runtime.config import make_run_config
from app.agent_runtime.errors import GraphRunnerError
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.repository import RepositoryError, RepositoryErrorCode
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.state import XuanhuGraphState, default_state
from app.schemas.agent import FormulaResult, HerbDose, SafetyRuleResult
from app.services import langgraph_review


def _review_state() -> XuanhuGraphState:
    session_id = str(uuid.uuid4())
    return default_state(
        session_id=session_id,
        command=XuanhuCommand.REVIEW.value,
        command_id=f"advance:{uuid.uuid4()}",
        graph_version="v1",
        run_id=str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_review_subgraph_checkpoints_before_reference_only_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _review_state()
    formula_id = str(uuid.uuid4())
    safety_id = str(uuid.uuid4())
    submission_id = str(uuid.uuid4())
    applied: list[object] = []

    async def prepare(_state: XuanhuGraphState) -> dict[str, Any]:
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 8,
            "artifact_refs": [
                {"kind": "formula_draft", "artifact_id": formula_id, "revision": 2},
                {"kind": "safety_result", "artifact_id": safety_id, "revision": 1},
            ],
            "gate_results": [
                {
                    "gate_name": "safety_rule_engine",
                    "decision": "passed",
                    "policy_version": "safety-rule-engine.product.v1",
                }
            ],
            "pending_interrupt": {
                "kind": "doctor_review",
                "interrupt_id": f"{safety_id}:1",
                "resume_token_ref": "review_submission_ref",
            },
            "last_error": None,
        }

    async def load(_state: XuanhuGraphState) -> SimpleNamespace:
        return SimpleNamespace(
            interrupt_payload={
                "kind": "doctor_review",
                "request_artifact_id": safety_id,
                "request_revision": "1",
                "request_digest": "a" * 64,
                "state_version": "8",
                "resume_token_ref": "review_submission_ref",
            }
        )

    async def apply(
        _state: XuanhuGraphState,
        *,
        prepared: object,
        resume_value: object,
    ) -> dict[str, Any]:
        applied.extend((prepared, resume_value))
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 10,
            "artifact_refs": [
                {"kind": "doctor_review", "artifact_id": str(uuid.uuid4()), "revision": 1}
            ],
            "gate_results": [
                {
                    "gate_name": "doctor_review",
                    "decision": "passed",
                    "policy_version": "doctor-review-interrupt.product.v1",
                }
            ],
            "pending_interrupt": None,
            "last_error": None,
        }

    monkeypatch.setattr(langgraph_review, "prepare_review_interrupt", prepare)
    monkeypatch.setattr(langgraph_review, "load_prepared_review", load)
    monkeypatch.setattr(langgraph_review, "apply_review_resume", apply)

    graph = build_main_graph(checkpointer=InMemorySaver())
    runner = GraphRunner(graph, timeout_seconds=2)
    config = make_run_config(state["session_id"])
    interrupted = await runner.ainvoke(dict(state), config=config)

    # The outer graph has not completed its nested review node yet.  The
    # prepared update is durable in that subgraph task's checkpoint.
    snapshot = await graph.aget_state(config, subgraphs=True)  # type: ignore[arg-type]
    assert snapshot.tasks and snapshot.tasks[0].state is not None
    nested = snapshot.tasks[0].state
    assert not isinstance(nested, dict)
    assert nested.values["pending_interrupt"]["resume_token_ref"] == "review_submission_ref"
    graph_values = {
        key: value for key, value in nested.values.items() if not key.startswith("__")
    }
    interrupt_values = [item.value for item in nested.values.get("__interrupt__", ())]
    outer_values = {
        key: value for key, value in interrupted.items() if not key.startswith("__")
    }
    serialized = json.dumps(
        {"outer": outer_values, "nested": graph_values, "interrupts": interrupt_values},
        ensure_ascii=False,
    )
    assert formula_id in serialized and safety_id in serialized
    assert "patient" not in serialized.lower()
    assert "composition" not in serialized.lower()
    assert "feedback" not in serialized.lower()

    resumed = await runner.aresume(
        session_id=state["session_id"],
        graph_version="v1",
        resume={"review_submission_ref": submission_id},
        config=config,
    )

    assert applied[1] == {"review_submission_ref": submission_id}
    assert resumed["domain_state_version"] == 10
    assert resumed["pending_interrupt"] is None


@pytest.mark.asyncio
async def test_real_review_command_precondition_failure_raises_for_claim_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _review_state()

    class _Repository:
        def __init__(self, _factory: object) -> None:
            pass

        async def get_state(self, _session_id: uuid.UUID) -> SimpleNamespace:
            return SimpleNamespace(state_version=5, safety_profile=None)

    async def session_meta(_session_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(
            agent_runtime="langgraph",
            current_stage="safety",
            status="active",
            pending_review=False,
            state_version=5,
        )

    monkeypatch.setattr(langgraph_review, "PostgresDomainRepository", _Repository)
    monkeypatch.setattr(langgraph_review, "get_session_factory", lambda: object())
    monkeypatch.setattr(langgraph_review, "_session_meta", session_meta)

    with pytest.raises(RepositoryError) as exc_info:
        await langgraph_review.prepare_review_interrupt(state)

    assert exc_info.value.code is RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID


@pytest.mark.asyncio
async def test_load_prepared_review_authority_failure_is_not_a_normal_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _review_state()

    async def unavailable(_session_id: uuid.UUID) -> object:
        raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)

    monkeypatch.setattr(langgraph_review, "_prepared_from_current", unavailable)

    with pytest.raises(RepositoryError) as exc_info:
        await langgraph_review.load_prepared_review(state)

    assert exc_info.value.code is RepositoryErrorCode.TRANSACTION_FAILED


@pytest.mark.asyncio
async def test_invalid_resume_can_be_followed_by_legal_resume_on_same_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _review_state()
    submission_id = str(uuid.uuid4())

    async def prepare(_state: XuanhuGraphState) -> dict[str, Any]:
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 8,
            "pending_interrupt": {
                "kind": "doctor_review",
                "interrupt_id": "safety:1",
                "resume_token_ref": "review_submission_ref",
            },
            "last_error": None,
        }

    async def load(_state: XuanhuGraphState) -> SimpleNamespace:
        return SimpleNamespace(
            interrupt_payload={
                "kind": "doctor_review",
                "request_artifact_id": str(uuid.uuid4()),
                "request_revision": "1",
                "request_digest": "a" * 64,
                "state_version": "8",
                "resume_token_ref": "review_submission_ref",
            }
        )

    async def apply(
        _state: XuanhuGraphState,
        *,
        prepared: object,
        resume_value: object,
    ) -> dict[str, Any]:
        del prepared
        if resume_value != {"review_submission_ref": submission_id}:
            raise langgraph_review.ReviewResumeRejected(
                code="REVIEW_RESUME_REF_INVALID",
                detail="review resume ref is invalid",
            )
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 9,
            "pending_interrupt": None,
            "last_error": None,
        }

    monkeypatch.setattr(langgraph_review, "prepare_review_interrupt", prepare)
    monkeypatch.setattr(langgraph_review, "load_prepared_review", load)
    monkeypatch.setattr(langgraph_review, "apply_review_resume", apply)

    graph = build_main_graph(checkpointer=InMemorySaver())
    runner = GraphRunner(graph, timeout_seconds=2)
    config = make_run_config(state["session_id"])
    await runner.ainvoke(dict(state), config=config)

    await runner.aresume(
        session_id=state["session_id"],
        graph_version="v1",
        resume={"unexpected_ref": "not-a-submission"},
        config=config,
    )
    snapshot = await graph.aget_state(config, subgraphs=True)  # type: ignore[arg-type]
    assert snapshot.tasks and snapshot.tasks[0].state is not None
    nested = snapshot.tasks[0].state
    assert not isinstance(nested, dict)
    interrupt_values = [
        item.value for item in nested.values.get("__interrupt__", ())
    ]
    interrupt_values.extend(
        item.value
        for task in nested.tasks
        for item in task.interrupts
    )
    assert interrupt_values[-1]["retry_error_code"] == "REVIEW_RESUME_REF_INVALID"

    resumed = await runner.aresume(
        session_id=state["session_id"],
        graph_version="v1",
        resume={"review_submission_ref": submission_id},
        config=config,
    )
    assert resumed["domain_state_version"] == 9
    assert resumed["pending_interrupt"] is None


@pytest.mark.asyncio
async def test_transient_pre_interrupt_failure_retries_on_same_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _review_state()
    submission_id = str(uuid.uuid4())
    load_attempts = 0

    async def prepare(_state: XuanhuGraphState) -> dict[str, Any]:
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 8,
            "pending_interrupt": {
                "kind": "doctor_review",
                "interrupt_id": "safety:1",
                "resume_token_ref": "review_submission_ref",
            },
            "last_error": None,
        }

    async def load(_state: XuanhuGraphState) -> SimpleNamespace:
        nonlocal load_attempts
        load_attempts += 1
        if load_attempts == 1:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
        return SimpleNamespace(
            interrupt_payload={
                "kind": "doctor_review",
                "request_artifact_id": str(uuid.uuid4()),
                "request_revision": "1",
                "request_digest": "a" * 64,
                "state_version": "8",
                "resume_token_ref": "review_submission_ref",
            }
        )

    async def apply(
        _state: XuanhuGraphState,
        *,
        prepared: object,
        resume_value: object,
    ) -> dict[str, Any]:
        del prepared
        assert resume_value == {"review_submission_ref": submission_id}
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 9,
            "pending_interrupt": None,
            "last_error": None,
        }

    monkeypatch.setattr(langgraph_review, "prepare_review_interrupt", prepare)
    monkeypatch.setattr(langgraph_review, "load_prepared_review", load)
    monkeypatch.setattr(langgraph_review, "apply_review_resume", apply)

    graph = build_main_graph(checkpointer=InMemorySaver())
    runner = GraphRunner(graph, timeout_seconds=2)
    config = make_run_config(state["session_id"])
    with pytest.raises(GraphRunnerError):
        await runner.ainvoke(dict(state), config=config)

    resumed = await runner.aresume(
        session_id=state["session_id"],
        graph_version="v1",
        resume={"review_submission_ref": submission_id},
        config=config,
    )
    assert load_attempts == 2
    assert resumed["domain_state_version"] == 9
    assert resumed["pending_interrupt"] is None


@pytest.mark.asyncio
async def test_transient_apply_failure_replays_valid_resume_on_same_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _review_state()
    submission_id = str(uuid.uuid4())
    apply_attempts = 0

    async def prepare(_state: XuanhuGraphState) -> dict[str, Any]:
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 8,
            "pending_interrupt": {
                "kind": "doctor_review",
                "interrupt_id": "safety:1",
                "resume_token_ref": "review_submission_ref",
            },
            "last_error": None,
        }

    async def load(_state: XuanhuGraphState) -> SimpleNamespace:
        return SimpleNamespace(
            interrupt_payload={
                "kind": "doctor_review",
                "request_artifact_id": str(uuid.uuid4()),
                "request_revision": "1",
                "request_digest": "a" * 64,
                "state_version": "8",
                "resume_token_ref": "review_submission_ref",
            }
        )

    async def apply(
        _state: XuanhuGraphState,
        *,
        prepared: object,
        resume_value: object,
    ) -> dict[str, Any]:
        nonlocal apply_attempts
        del prepared
        apply_attempts += 1
        assert resume_value == {"review_submission_ref": submission_id}
        if apply_attempts == 1:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": 9,
            "pending_interrupt": None,
            "last_error": None,
        }

    monkeypatch.setattr(langgraph_review, "prepare_review_interrupt", prepare)
    monkeypatch.setattr(langgraph_review, "load_prepared_review", load)
    monkeypatch.setattr(langgraph_review, "apply_review_resume", apply)

    graph = build_main_graph(checkpointer=InMemorySaver())
    runner = GraphRunner(graph, timeout_seconds=2)
    config = make_run_config(state["session_id"])
    await runner.ainvoke(dict(state), config=config)
    with pytest.raises(GraphRunnerError):
        await runner.aresume(
            session_id=state["session_id"],
            graph_version="v1",
            resume={"review_submission_ref": submission_id},
            config=config,
        )

    resumed = await runner.aresume(
        session_id=state["session_id"],
        graph_version="v1",
        resume={"review_submission_ref": submission_id},
        config=config,
    )
    assert apply_attempts == 2
    assert resumed["domain_state_version"] == 9
    assert resumed["pending_interrupt"] is None


@pytest.mark.asyncio
async def test_invalid_review_command_refs_remain_side_effect_free() -> None:
    state = default_state(command=XuanhuCommand.REVIEW.value)

    result = await langgraph_review.prepare_review_interrupt(state)

    assert result["last_error"]["code"] == "REVIEW_COMMAND_REF_INVALID"
    assert result["pending_interrupt"] is None


def test_blocked_modify_uses_non_authoritative_attempt_artifacts() -> None:
    assert langgraph_review._review_artifact_types(safety_passed=False) == (
        langgraph_review.REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE,
        langgraph_review.SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE,
    )
    assert langgraph_review._review_artifact_types(safety_passed=True) == (
        langgraph_review.REVIEWED_FORMULA_ARTIFACT_TYPE,
        langgraph_review.SAFETY_ARTIFACT_TYPE,
    )


def test_safety_projection_binds_every_clinical_field() -> None:
    session_id = uuid.uuid4()
    formula = FormulaResult(
        name="unit formula",
        composition=[HerbDose(herb="unit-herb", dose=3, unit="g")],
        rationale="unit-only",
    )
    result = SafetyRuleResult(
        passed=True,
        normalized_formula=formula,
        rule_version="unit-rule-v1",
    )
    authority = langgraph_review.FormulaAuthority(
        record=SimpleNamespace(artifact_type=langgraph_review.FORMULA_ARTIFACT_TYPE),
        formula=formula,
    )
    patient_snapshot = {"allergies": [], "pregnancy_status": "unknown"}
    values: dict[str, object] = {
        "session_id": session_id,
        "passed": True,
        "rule_version": "unit-rule-v1",
        "formula_source": "agent_output",
        "agent_run_id": None,
        "trace_id": "unit-trace",
        "formula_snapshot": formula.model_dump(mode="json"),
        "normalized_formula": formula.model_dump(mode="json"),
        "issues": [],
        "patient_snapshot": patient_snapshot,
    }

    assert langgraph_review._safety_projection_matches(
        SimpleNamespace(**values),
        session_id=session_id,
        formula=authority,
        result=result,
        patient_snapshot=patient_snapshot,
        agent_run_id=None,
        trace_id="unit-trace",
    )

    mutations: dict[str, object] = {
        "formula_source": "doctor_override",
        "agent_run_id": uuid.uuid4(),
        "trace_id": "tampered-trace",
        "formula_snapshot": {"name": "tampered"},
        "normalized_formula": {"name": "tampered"},
        "issues": [{"type": "tampered"}],
        "patient_snapshot": {"allergies": ["tampered"]},
    }
    for field, bad_value in mutations.items():
        tampered = dict(values)
        tampered[field] = bad_value
        assert not langgraph_review._safety_projection_matches(
            SimpleNamespace(**tampered),
            session_id=session_id,
            formula=authority,
            result=result,
            patient_snapshot=patient_snapshot,
            agent_run_id=None,
            trace_id="unit-trace",
        ), field


@pytest.mark.asyncio
async def test_injected_review_executor_preserves_historical_top_level_node() -> None:
    seen: list[str] = []

    async def executor(state: XuanhuGraphState) -> dict[str, Any]:
        seen.append(state["session_id"])
        return {"route": NODE_REVIEW_PLACEHOLDER, "domain_state_version": 3}

    state = _review_state()
    runner = GraphRunner(build_main_graph(review_executor=executor), timeout_seconds=2)
    result = await runner.ainvoke(
        dict(state),
        config=make_run_config(state["session_id"]),
    )

    assert seen == [state["session_id"]]
    assert result["route"] == NODE_REVIEW_PLACEHOLDER
    assert result["domain_state_version"] == 3
