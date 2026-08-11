"""4d80c7b review should-fix: 自动重试成功后审计 run_id 指向实际成功调用的单测。"""

from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_intake_retry_success_records_success_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首轮失败(STRUCTURED_OUTPUT_INVALID)→ 自动重试成功 → extraction.agent_run_id
    必须等于重试 run_id(而非首轮失败 run_id)——审计追溯不指向失败记录。"""
    import app.services.langgraph_intake as intake_module
    from app.agent_runtime.reducer import DomainState
    from app.agents.intake_extraction import IntakeExecutionResult, IntakeExecutionStatus
    from app.schemas.intake import (
        IntakeExtractionDecision,
        IntakeExtractionOutput,
    )

    captured: dict[str, object] = {}
    first_run_id = uuid.uuid4()
    calls: list[uuid.UUID] = []

    def fake_run_spec_run_id(run_spec: object) -> uuid.UUID:
        return run_spec.run_id

    async def fake_execute_intake_extraction(*, runtime: object, run_spec: object, input_payload: object):
        calls.append(fake_run_spec_run_id(run_spec))
        if len(calls) == 1:
            return IntakeExecutionResult(
                status=IntakeExecutionStatus.FAILED,
                failure_code=IntakeExtractionDecision.EXTRACTED,  # 占位
            )
        from app.schemas.intake import IntakeExecutionStatus as _S  # noqa: F401

        return IntakeExecutionResult(
            status=IntakeExecutionStatus.SUCCEEDED,
            output=IntakeExtractionOutput(
                decision="extracted",
                observations=(),
            ),
        )

    async def fake_save_intermediate(claim_id: object, patch: dict[str, object], *, step: str) -> None:
        captured.update(patch)

    monkeypatch.setattr(intake_module, "_save_intermediate", fake_save_intermediate)

    # 打桩前置依赖: precheck clear、无 bound 输出、无缓存。
    monkeypatch.setattr(
        intake_module,
        "evaluate_raw_text_triage_precheck",
        lambda *a, **k: type("P", (), {"candidates": ()})(),
    )
    monkeypatch.setattr(intake_module, "_bound_explicit_none_output", lambda *a, **k: None)
    monkeypatch.setattr(intake_module, "_bound_social_reply_output", lambda *a, **k: None)
    monkeypatch.setattr(intake_module, "_INTAKE_OUTPUT_CACHE", {})

    claim = type(
        "Claim",
        (),
        {
            "id": uuid.uuid4(),
            "session_id": uuid.uuid4(),
            "input_state_version": 1,
            "run_id": uuid.uuid4(),
            "idempotency_key": "cmd:test",
        },
    )()
    message = type("Msg", (), {"id": uuid.uuid4(), "content": "没有过敏史。", "structured_delta": None})()
    state = DomainState(
        session_id=claim.session_id,
        state_version=1,
        observations=(),
    )

    # 首轮用固定 run_id(模拟 _stable_intake_extraction_run_id),重试用随机 id:
    # 先替换 execute 为"首轮失败+重试成功"双阶段,再调用。
    monkeypatch.setattr(intake_module, "_stable_intake_extraction_run_id", lambda c: first_run_id)

    async def staged_execute(*, runtime: object, run_spec: object, input_payload: object):
        run_id = fake_run_spec_run_id(run_spec)
        calls.append(run_id)
        if run_id == first_run_id:
            from app.agent_runtime.specs import RuntimeErrorCode

            return IntakeExecutionResult(
                status=IntakeExecutionStatus.FAILED,
                failure_code=RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID,
            )
        from app.agent_runtime.intake_verifier import (
            IntakeCheckResult,
            IntakeCheckStatus,
            IntakeVerificationReport,
            IntakeVerifierName,
        )

        return IntakeExecutionResult(
            status=IntakeExecutionStatus.SUCCEEDED,
            output=IntakeExtractionOutput(decision="extracted", observations=()),
            verification=IntakeVerificationReport(
                passed=True,
                checks=(
                    IntakeCheckResult(
                        verifier=IntakeVerifierName.SCHEMA,
                        status=IntakeCheckStatus.PASSED,
                        failure_code=None,
                    ),
                ),
                failure_code=None,
                subject_digest="0" * 64,
            ),
        )

    monkeypatch.setattr(intake_module, "execute_intake_extraction", staged_execute)
    intake_module._INTAKE_OUTPUT_CACHE.clear()  # type: ignore[attr-defined]

    output = await intake_module._load_or_retry_intake_output(  # noqa: SLF001
        claim,
        message,
        state,
        "trace-test",
    )
    assert output is not None
    assert output.decision is IntakeExtractionDecision.EXTRACTED

    # 两次调用: 首轮失败 + 重试成功。
    assert len(calls) == 2
    extraction = captured.get("extraction")
    assert isinstance(extraction, dict)
    # 审计 run_id = 重试调用(非首轮)。
    assert extraction["agent_run_id"] == str(calls[1])
    assert extraction["agent_run_id"] != str(first_run_id)
