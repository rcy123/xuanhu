"""L8-4 fixed-synthetic evaluation and shadow-gate acceptance tests."""

from __future__ import annotations

import pytest

from app.agent_runtime.sandbox_evaluation import (
    BehaviorCaseV1,
    BehaviorDatasetV1,
    BehaviorDimension,
    EvaluationSuite,
    InjectedModelTrial,
    RealModelTrialGate,
    SandboxEvaluationError,
    SandboxEvaluationFailureCode,
    ShadowComparator,
)


def _case(case_id: str = "case-1") -> BehaviorCaseV1:
    probe = BehaviorCaseV1.build(
        case_id=case_id,
        dimension=BehaviorDimension.INTAKE,
        input_text="synthetic intake",
        expected_output_text="placeholder",
    )
    expected = f"synthetic-response-{probe.input.input_digest[:16]}"
    return BehaviorCaseV1.build(
        case_id=case_id,
        dimension=BehaviorDimension.INTAKE,
        input_text="synthetic intake",
        expected_output_text=expected,
    )


def test_behavior_case_is_versioned_and_deterministic() -> None:
    left = _case()
    right = _case()
    assert left.case_digest == right.case_digest
    assert left.input.input_digest == right.input.input_digest
    assert left.schema_version == "sandbox-evaluation.v1"


def test_evaluation_suite_passes_fixed_synthetic_case() -> None:
    result = EvaluationSuite().run([_case()], run_id="eval-1")
    assert result.total_cases == 1
    assert result.passed_cases == 1
    assert result.failed_cases == 0
    assert result.aggregate_score == 1.0
    assert result.run_digest


def test_bounded_dataset_binds_case_digests() -> None:
    dataset = BehaviorDatasetV1.build(dataset_id="set-1", cases=(_case(),))
    result = EvaluationSuite().run_dataset(dataset)
    assert dataset.dataset_digest
    assert result.run_id == "eval-set-1"


def test_evaluation_suite_records_failed_case_without_aborting() -> None:
    case = BehaviorCaseV1.build(
        case_id="case-fail",
        dimension=BehaviorDimension.SAFETY,
        input_text="synthetic safety",
        expected_output_text="different",
    )
    result = EvaluationSuite().run([case], run_id="eval-fail")
    assert result.failed_cases == 1
    assert result.results[0].failure_attribution


def test_shadow_comparison_is_isolated_and_deterministic() -> None:
    case = _case()
    report = ShadowComparator().compare(
        [case],
        InjectedModelTrial(),
        InjectedModelTrial(),
        run_id="shadow-1",
    )
    assert report.total_comparisons == 1
    assert report.v2_writes_prohibited
    assert report.report_digest
    with pytest.raises(SandboxEvaluationError) as exc_info:
        ShadowComparator().write_business_result({"forbidden": True})
    assert exc_info.value.args[0] == SandboxEvaluationFailureCode.SHADOW_WRITE_PROHIBITED.value


def test_shadow_comparison_limit_is_bounded() -> None:
    cases = [_case(f"case-{i}") for i in range(65)]
    with pytest.raises(SandboxEvaluationError) as exc_info:
        ShadowComparator().compare(cases, InjectedModelTrial(), InjectedModelTrial())
    assert exc_info.value.args[0] == SandboxEvaluationFailureCode.SHADOW_COMPARISON_LIMIT_EXCEEDED.value


def test_real_model_gate_is_external_and_default_off() -> None:
    gate = RealModelTrialGate()
    decision = gate.check()
    assert not decision.enabled
    assert decision.gate_decision == "external_gate_required"
    assert decision.gate_digest


def test_real_model_gate_requires_all_external_conditions() -> None:
    gate = RealModelTrialGate()
    gate.configure(external_approval="approval-1", budget_allocated=True, data_policy_approved=True)
    decision = gate.check()
    assert decision.enabled
    assert decision.gate_decision == "approved"
    assert decision.external_approval == "approval-1"


def test_real_model_gate_rejects_invalid_approval() -> None:
    gate = RealModelTrialGate()
    with pytest.raises(ValueError):
        gate.configure(external_approval="not allowed!")
