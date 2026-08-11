"""L8-4 沙盒行为评估集、离线评估与 Shadow 对比参考实现。

Copyright (c) 2026 xuanhu. All rights reserved.

本模块为 L8-SBX 子任务 L8-4 提供离线、确定性的评估组件：

- BehaviorCaseV1：固定、版本化、无真实数据的评估用例。
- BehaviorDimension：评估维度闭集。
- InjectedModelTrial：确定性注入模型试跑（固定合成响应）。
- EvaluationSuite：运行评估用例集，生成每维度 pass/fail + 聚合指标。
- ShadowComparator：同一去标识输入比较 legacy/v2 质量、延迟、token、失败率。
- ShadowReportV1：隔离的 Shadow 对比报告，禁止写业务结果。
- RealModelTrialGate：默认关闭；无外部批准时返回 external_gate_required。

所有数据均为固定合成内容，不涉及患者、临床或公开数据。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Schema & resource constants
# ---------------------------------------------------------------------------

SANDBOX_EVALUATION_SCHEMA_VERSION: Literal["sandbox-evaluation.v1"] = "sandbox-evaluation.v1"
SANDBOX_SHADOW_SCHEMA_VERSION: Literal["sandbox-shadow.v1"] = "sandbox-shadow.v1"
SANDBOX_EVALUATION_ADAPTER_VERSION: Literal["sandbox-evaluation-adapter.v1"] = "sandbox-evaluation-adapter.v1"

_MAX_CASES_PER_RUN = 256
_MAX_DIMENSIONS = 32
_MAX_DIMENSION_NAME_BYTES = 64
_MAX_EVALUATION_BYTES = 512 * 1024
_MAX_SHADOW_REPORT_BYTES = 256 * 1024
_MAX_SHADOW_COMPARISONS = 64
_MAX_INPUT_TEXT_BYTES = 65536
_MAX_OUTPUT_TEXT_BYTES = 65536
_MAX_LABEL_BYTES = 128

_GATE_APPROVAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-/]*$"

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class SandboxEvaluationError(ValueError):
    """A fixed, payload-free, chainless evaluation error."""

    __slots__ = ()

    def __init__(self, code: str) -> None:
        super().__init__(code)


class SandboxEvaluationFailureCode(StrEnum):
    EVALUATION_LIMIT_EXCEEDED = "SANDBOX_EVALUATION_LIMIT_EXCEEDED"
    EVALUATION_DIMENSION_INVALID = "SANDBOX_EVALUATION_DIMENSION_INVALID"
    EVALUATION_CASE_INVALID = "SANDBOX_EVALUATION_CASE_INVALID"
    EVALUATION_DIGEST_MISMATCH = "SANDBOX_EVALUATION_DIGEST_MISMATCH"
    EVALUATION_INPUT_TOO_LARGE = "SANDBOX_EVALUATION_INPUT_TOO_LARGE"
    EVALUATION_OUTPUT_TOO_LARGE = "SANDBOX_EVALUATION_OUTPUT_TOO_LARGE"
    EVALUATION_FAILURE_ISOLATION = "SANDBOX_EVALUATION_FAILURE_ISOLATION"
    SHADOW_WRITE_PROHIBITED = "SANDBOX_EVALUATION_SHADOW_WRITE_PROHIBITED"
    SHADOW_COMPARISON_LIMIT_EXCEEDED = "SANDBOX_EVALUATION_SHADOW_COMPARISON_LIMIT_EXCEEDED"
    GATE_NOT_APPROVED = "SANDBOX_EVALUATION_GATE_NOT_APPROVED"
    GATE_NO_BUDGET = "SANDBOX_EVALUATION_GATE_NO_BUDGET"
    GATE_NO_DATA_POLICY = "SANDBOX_EVALUATION_GATE_NO_DATA_POLICY"
    INTERNAL_FAILURE = "SANDBOX_EVALUATION_INTERNAL_FAILURE"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# BehaviorDimension — closed set of evaluation dimensions
# ---------------------------------------------------------------------------


class BehaviorDimension(StrEnum):
    """Closed set of evaluation dimensions for L8-4 sandbox."""

    INTAKE = "intake"
    TRIAGE = "triage"
    COMPLETENESS = "completeness"
    PROMPT_INJECTION = "prompt_injection"
    SYNDROME = "syndrome"
    FORMULA = "formula"
    SAFETY = "safety"
    REVIEW = "review"
    RECORD = "record"


# ---------------------------------------------------------------------------
# BehaviorCase DTOs
# ---------------------------------------------------------------------------


class BehaviorCaseInputV1(_StrictFrozenModel):
    """Fixed synthetic input for a behavior evaluation case."""

    text: str = Field(max_length=_MAX_INPUT_TEXT_BYTES)
    input_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def input_digest_is_derived(self) -> BehaviorCaseInputV1:
        expected = _canonical_sha256({"text": self.text})
        if self.input_digest != expected:
            raise ValueError("input_digest mismatch")
        return self

    @classmethod
    def build(cls, *, text: str) -> BehaviorCaseInputV1:
        digest = _canonical_sha256({"text": text})
        return cls(text=text, input_digest=digest)


class BehaviorCaseOutputV1(_StrictFrozenModel):
    """Expected or actual output for a behavior evaluation case."""

    text: str = Field(max_length=_MAX_OUTPUT_TEXT_BYTES)
    output_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def output_digest_is_derived(self) -> BehaviorCaseOutputV1:
        expected = _canonical_sha256({"text": self.text})
        if self.output_digest != expected:
            raise ValueError("output_digest mismatch")
        return self

    @classmethod
    def build(cls, *, text: str) -> BehaviorCaseOutputV1:
        digest = _canonical_sha256({"text": text})
        return cls(text=text, output_digest=digest)


class BehaviorCaseMetricV1(_StrictFrozenModel):
    """Aggregated metrics for a single behavior case evaluation."""

    latency_ms: float = Field(ge=0.0)
    total_tokens: int = Field(ge=0)
    model_calls: int = Field(ge=0, le=100)
    retries: int = Field(ge=0, le=100)
    metric_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def metric_digest_is_derived(self) -> BehaviorCaseMetricV1:
        expected = _canonical_sha256(
            {
                "latency_ms": self.latency_ms,
                "total_tokens": self.total_tokens,
                "model_calls": self.model_calls,
                "retries": self.retries,
            }
        )
        if self.metric_digest != expected:
            raise ValueError("metric_digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        latency_ms: float = 0.0,
        total_tokens: int = 0,
        model_calls: int = 0,
        retries: int = 0,
    ) -> BehaviorCaseMetricV1:
        digest = _canonical_sha256(
            {
                "latency_ms": latency_ms,
                "total_tokens": total_tokens,
                "model_calls": model_calls,
                "retries": retries,
            }
        )
        return cls(
            latency_ms=latency_ms,
            total_tokens=total_tokens,
            model_calls=model_calls,
            retries=retries,
            metric_digest=digest,
        )


class BehaviorCaseV1(_StrictFrozenModel):
    """A fixed, versioned, synthetic behavior evaluation case.

    Covers dimensions: intake, triage, completeness, prompt_injection,
    syndrome, formula, safety, review, record.
    No real data — all inputs/outputs are fixed synthetic content.
    """

    schema_version: Literal["sandbox-evaluation.v1"]
    case_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    dimension: BehaviorDimension
    input: BehaviorCaseInputV1
    expected_output: BehaviorCaseOutputV1
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    label: str = Field(default="", max_length=_MAX_LABEL_BYTES)
    case_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def case_digest_is_derived(self) -> BehaviorCaseV1:
        expected = _derive_case_digest(self)
        if self.case_digest != expected:
            raise ValueError("case_digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        case_id: str,
        dimension: BehaviorDimension,
        input_text: str,
        expected_output_text: str,
        threshold: float = 0.8,
        label: str = "",
    ) -> BehaviorCaseV1:
        inp = BehaviorCaseInputV1.build(text=input_text)
        expected = BehaviorCaseOutputV1.build(text=expected_output_text)
        raw = cls.model_construct(
            schema_version="sandbox-evaluation.v1",
            case_id=case_id,
            dimension=dimension,
            input=inp,
            expected_output=expected,
            threshold=threshold,
            label=label,
            case_digest="x" * 64,
        )
        case_digest = _derive_case_digest(raw)
        # Re-create with the correct digest (Pydantic frozen)
        return cls(
            schema_version="sandbox-evaluation.v1",
            case_id=case_id,
            dimension=dimension,
            input=inp,
            expected_output=expected,
            threshold=threshold,
            label=label,
            case_digest=case_digest,
        )


class BehaviorDatasetV1(_StrictFrozenModel):
    """A bounded, versioned evaluation set of fixed synthetic cases."""

    schema_version: Literal["sandbox-evaluation.v1"]
    dataset_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    cases: tuple[BehaviorCaseV1, ...] = Field(max_length=_MAX_CASES_PER_RUN)
    dataset_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def dataset_digest_is_derived(self) -> BehaviorDatasetV1:
        expected = _derive_dataset_digest(self)
        if self.dataset_digest != expected:
            raise ValueError("dataset_digest mismatch")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("dataset case ids must be unique")
        return self

    @classmethod
    def build(cls, *, dataset_id: str, cases: Sequence[BehaviorCaseV1]) -> BehaviorDatasetV1:
        canonical = tuple(cases)
        raw = cls.model_construct(
            schema_version="sandbox-evaluation.v1",
            dataset_id=dataset_id,
            cases=canonical,
            dataset_digest="",
        )
        return cls(
            schema_version="sandbox-evaluation.v1",
            dataset_id=dataset_id,
            cases=canonical,
            dataset_digest=_derive_dataset_digest(raw),
        )


class BehaviorCaseResultV1(_StrictFrozenModel):
    """Result of evaluating a single behavior case."""

    case_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    dimension: BehaviorDimension
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    actual_output: BehaviorCaseOutputV1
    metrics: BehaviorCaseMetricV1
    failure_attribution: str = Field(default="", max_length=256)
    result_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def result_digest_is_derived(self) -> BehaviorCaseResultV1:
        expected = _derive_case_result_digest(self)
        if self.result_digest != expected:
            raise ValueError("result_digest mismatch")
        return self


class EvaluationRunV1(_StrictFrozenModel):
    """Aggregate result of an evaluation run over a set of behavior cases."""

    schema_version: Literal["sandbox-evaluation.v1"]
    adapter_version: Literal["sandbox-evaluation-adapter.v1"]
    run_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    results: tuple[BehaviorCaseResultV1, ...] = Field(max_length=_MAX_CASES_PER_RUN)
    total_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    aggregate_score: float = Field(ge=0.0, le=1.0)
    total_latency_ms: float = Field(ge=0.0)
    total_tokens: int = Field(ge=0)
    run_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def run_digest_is_derived(self) -> EvaluationRunV1:
        expected = _derive_run_digest(self)
        if self.run_digest != expected:
            raise ValueError("run_digest mismatch")
        return self


# ---------------------------------------------------------------------------
# Shadow comparison DTOs
# ---------------------------------------------------------------------------


class ShadowComparisonV1(_StrictFrozenModel):
    """A single shadow comparison entry between legacy and v2 outputs."""

    case_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    dimension: BehaviorDimension
    legacy_output: BehaviorCaseOutputV1
    v2_output: BehaviorCaseOutputV1
    legacy_metrics: BehaviorCaseMetricV1
    v2_metrics: BehaviorCaseMetricV1
    quality_score: float = Field(ge=0.0, le=1.0)
    latency_delta_ms: float
    token_delta: int
    legacy_failed: bool = False
    v2_failed: bool = False
    comparison_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def comparison_digest_is_derived(self) -> ShadowComparisonV1:
        expected = _derive_comparison_digest(self)
        if self.comparison_digest != expected:
            raise ValueError("comparison_digest mismatch")
        return self


class ShadowReportV1(_StrictFrozenModel):
    """Isolated shadow comparison report.

    v2 output only enters this report. It must never be written to
    business results or production stores.
    """

    schema_version: Literal["sandbox-shadow.v1"]
    adapter_version: Literal["sandbox-evaluation-adapter.v1"]
    run_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    comparisons: tuple[ShadowComparisonV1, ...] = Field(max_length=_MAX_SHADOW_COMPARISONS)
    total_comparisons: int = Field(ge=0)
    overall_quality: float = Field(ge=0.0, le=1.0)
    avg_latency_delta_ms: float
    avg_token_delta: float
    legacy_failure_rate: float = Field(ge=0.0, le=1.0)
    v2_failure_rate: float = Field(ge=0.0, le=1.0)
    v2_writes_prohibited: bool = True
    report_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def report_digest_is_derived(self) -> ShadowReportV1:
        expected = _derive_report_digest(self)
        if self.report_digest != expected:
            raise ValueError("report_digest mismatch")
        return self


# ---------------------------------------------------------------------------
# RealModelTrialGate DTOs
# ---------------------------------------------------------------------------


class RealModelTrialGateV1(_StrictFrozenModel):
    """Gate state for real model trial.

    Default off. Without named external approval, budget, and data policy,
    returns external_gate_required. Never initiates network model calls.
    """

    schema_version: Literal["sandbox-evaluation.v1"]
    enabled: bool = False
    external_approval: str | None = Field(default=None, min_length=1, max_length=256, pattern=_GATE_APPROVAL_PATTERN)
    budget_allocated: bool = False
    data_policy_approved: bool = False
    gate_decision: str = "external_gate_required"
    gate_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def gate_digest_is_derived(self) -> RealModelTrialGateV1:
        expected = _derive_gate_digest(self)
        if self.gate_digest != expected:
            raise ValueError("gate_digest mismatch")
        return self

    @classmethod
    def build(cls) -> RealModelTrialGateV1:
        raw = cls.model_construct(
            schema_version="sandbox-evaluation.v1",
            enabled=False,
            gate_decision="external_gate_required",
            gate_digest="x" * 64,
        )
        gate_digest = _derive_gate_digest(raw)
        return cls(
            schema_version="sandbox-evaluation.v1",
            enabled=False,
            gate_decision="external_gate_required",
            gate_digest=gate_digest,
        )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def canonical_json_bytes(value: object) -> bytes:
    """Serialize to stable canonical JSON."""
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return value


def _derive_case_digest(case: BehaviorCaseV1) -> str:
    return _canonical_sha256(
        {
            "case_id": case.case_id,
            "dimension": str(case.dimension),
            "input": case.input.model_dump(mode="json"),
            "expected_output": case.expected_output.model_dump(mode="json"),
            "threshold": case.threshold,
            "label": case.label,
        }
    )


def _derive_dataset_digest(dataset: BehaviorDatasetV1) -> str:
    return _canonical_sha256(
        {
            "dataset_id": dataset.dataset_id,
            "cases": [case.case_digest for case in dataset.cases],
        }
    )


def _derive_case_result_digest(result: BehaviorCaseResultV1) -> str:
    return _canonical_sha256(
        {
            "case_id": result.case_id,
            "dimension": str(result.dimension),
            "passed": result.passed,
            "score": result.score,
            "threshold": result.threshold,
            "actual_output": result.actual_output.model_dump(mode="json"),
            "metrics": result.metrics.model_dump(mode="json"),
            "failure_attribution": result.failure_attribution,
        }
    )


def _derive_run_digest(run: EvaluationRunV1) -> str:
    return _canonical_sha256(
        {
            "run_id": run.run_id,
            "results": [r.model_dump(mode="json") for r in run.results],
            "total_cases": run.total_cases,
            "passed_cases": run.passed_cases,
            "failed_cases": run.failed_cases,
            "aggregate_score": run.aggregate_score,
            "total_latency_ms": run.total_latency_ms,
            "total_tokens": run.total_tokens,
        }
    )


def _derive_comparison_digest(comp: ShadowComparisonV1) -> str:
    return _canonical_sha256(
        {
            "case_id": comp.case_id,
            "dimension": str(comp.dimension),
            "legacy_output": comp.legacy_output.model_dump(mode="json"),
            "v2_output": comp.v2_output.model_dump(mode="json"),
            "legacy_metrics": comp.legacy_metrics.model_dump(mode="json"),
            "v2_metrics": comp.v2_metrics.model_dump(mode="json"),
            "quality_score": comp.quality_score,
            "latency_delta_ms": comp.latency_delta_ms,
            "token_delta": comp.token_delta,
            "legacy_failed": comp.legacy_failed,
            "v2_failed": comp.v2_failed,
        }
    )


def _derive_report_digest(report: ShadowReportV1) -> str:
    return _canonical_sha256(
        {
            "run_id": report.run_id,
            "comparisons": [c.model_dump(mode="json") for c in report.comparisons],
            "total_comparisons": report.total_comparisons,
            "overall_quality": report.overall_quality,
            "avg_latency_delta_ms": report.avg_latency_delta_ms,
            "avg_token_delta": report.avg_token_delta,
            "legacy_failure_rate": report.legacy_failure_rate,
            "v2_failure_rate": report.v2_failure_rate,
            "v2_writes_prohibited": report.v2_writes_prohibited,
        }
    )


def _derive_gate_digest(gate: RealModelTrialGateV1) -> str:
    return _canonical_sha256(
        {
            "enabled": gate.enabled,
            "external_approval": gate.external_approval,
            "budget_allocated": gate.budget_allocated,
            "data_policy_approved": gate.data_policy_approved,
            "gate_decision": gate.gate_decision,
        }
    )


def _build_gate_state(
    *,
    enabled: bool,
    external_approval: str | None,
    budget_allocated: bool,
    data_policy_approved: bool,
    gate_decision: str,
) -> RealModelTrialGateV1:
    raw = RealModelTrialGateV1.model_construct(
        schema_version="sandbox-evaluation.v1",
        enabled=enabled,
        external_approval=external_approval,
        budget_allocated=budget_allocated,
        data_policy_approved=data_policy_approved,
        gate_decision=gate_decision,
        gate_digest="",
    )
    return RealModelTrialGateV1(
        schema_version="sandbox-evaluation.v1",
        enabled=enabled,
        external_approval=external_approval,
        budget_allocated=budget_allocated,
        data_policy_approved=data_policy_approved,
        gate_decision=gate_decision,
        gate_digest=_derive_gate_digest(raw),
    )


def _raise_error(code: SandboxEvaluationFailureCode) -> NoReturn:
    raise SandboxEvaluationError(code.value) from None


# ---------------------------------------------------------------------------
# InjectedModelTrial — deterministic injected-model trial
# ---------------------------------------------------------------------------


class InjectedModelTrial:
    """A deterministic injected-model trial using fixed synthetic responses.

    No real model is called. Responses are derived from input digests
    to ensure deterministic behavior for evaluation purposes.
    """

    __slots__ = ("_responses", "_lock", "_default_latency_ms", "_default_tokens")

    def __init__(
        self,
        responses: Mapping[str, str] | None = None,
        default_latency_ms: float = 1.0,
        default_tokens: int = 10,
    ) -> None:
        self._responses = dict(responses) if responses else {}
        self._lock = threading.RLock()
        self._default_latency_ms = default_latency_ms
        self._default_tokens = default_tokens

    def generate(
        self,
        input_digest: str,
        *,
        latency_ms: float | None = None,
        tokens: int | None = None,
    ) -> BehaviorCaseOutputV1:
        """Generate a deterministic response for the given input digest.

        Uses configured response map if available, otherwise derives
        a response from the input digest to ensure determinism.
        """
        with self._lock:
            if input_digest in self._responses:
                text = self._responses[input_digest]
            else:
                text = f"synthetic-response-{input_digest[:16]}"

            return BehaviorCaseOutputV1.build(text=text)

    def set_response(self, input_digest: str, response_text: str) -> None:
        """Set a specific response for a given input digest."""
        with self._lock:
            self._responses[input_digest] = response_text

    def reset(self) -> None:
        """Reset all configured responses."""
        with self._lock:
            self._responses.clear()


# ---------------------------------------------------------------------------
# EvaluationSuite — runs evaluation cases through injected model trial
# ---------------------------------------------------------------------------


class EvaluationSuite:
    """Runs a set of behavior evaluation cases through an injected model trial.

    Each case is evaluated independently. Failure in one case does not
    affect others (failure isolation).
    """

    __slots__ = ("_trials", "_lock")

    def __init__(self, trials: Sequence[InjectedModelTrial] | None = None) -> None:
        self._trials = list(trials) if trials else [InjectedModelTrial()]
        self._lock = threading.RLock()

    def run(
        self,
        cases: Sequence[BehaviorCaseV1],
        run_id: str | None = None,
    ) -> EvaluationRunV1:
        """Run evaluation on a set of behavior cases.

        Each case is evaluated independently with failure isolation.
        """
        import uuid

        actual_run_id = run_id or f"eval-run-{uuid.uuid4().hex[:16]}"

        results: list[BehaviorCaseResultV1] = []
        total_latency = 0.0
        total_tokens = 0
        passed = 0
        failed = 0

        for case in cases:
            try:
                result = self._evaluate_single(case)
                results.append(result)
                total_latency += result.metrics.latency_ms
                total_tokens += result.metrics.total_tokens
                if result.passed:
                    passed += 1
                else:
                    failed += 1
            except SandboxEvaluationError:
                # Failure isolation — record as failed, continue others
                failed_result = BehaviorCaseResultV1.model_construct(
                    case_id=case.case_id,
                    dimension=case.dimension,
                    passed=False,
                    score=0.0,
                    threshold=case.threshold,
                    actual_output=BehaviorCaseOutputV1.build(text=""),
                    metrics=BehaviorCaseMetricV1.build(),
                    failure_attribution="evaluation_failure",
                    result_digest="x" * 64,
                )
                result_digest = _derive_case_result_digest(failed_result)
                object.__setattr__(failed_result, "result_digest", result_digest)
                results.append(failed_result)
                failed += 1

        total = len(cases)
        aggregate_score = (passed / total) if total > 0 else 0.0

        run = EvaluationRunV1.model_construct(
            schema_version="sandbox-evaluation.v1",
            adapter_version="sandbox-evaluation-adapter.v1",
            run_id=actual_run_id,
            results=tuple(results),
            total_cases=total,
            passed_cases=passed,
            failed_cases=failed,
            aggregate_score=aggregate_score,
            total_latency_ms=total_latency,
            total_tokens=total_tokens,
            run_digest="x" * 64,
        )
        run_digest = _derive_run_digest(run)
        object.__setattr__(run, "run_digest", run_digest)
        return run

    def run_dataset(
        self,
        dataset: BehaviorDatasetV1,
        run_id: str | None = None,
    ) -> EvaluationRunV1:
        """Run one bounded dataset without accepting arbitrary external rows."""
        return self.run(dataset.cases, run_id=run_id or f"eval-{dataset.dataset_id}")

    def _evaluate_single(self, case: BehaviorCaseV1) -> BehaviorCaseResultV1:
        """Evaluate a single behavior case against the injected model trial."""
        trial = self._trials[0]

        # Generate output from the injected trial
        output = trial.generate(case.input.input_digest)

        # Compare with expected output
        score = self._compute_score(output, case.expected_output)
        passed = score >= case.threshold

        metrics = BehaviorCaseMetricV1.build(
            latency_ms=self._trials[0]._default_latency_ms,
            total_tokens=self._trials[0]._default_tokens,
            model_calls=1,
        )

        attribution = ""
        if not passed:
            attribution = f"score {score:.4f} below threshold {case.threshold:.4f}"

        result = BehaviorCaseResultV1.model_construct(
            case_id=case.case_id,
            dimension=case.dimension,
            passed=passed,
            score=score,
            threshold=case.threshold,
            actual_output=output,
            metrics=metrics,
            failure_attribution=attribution,
            result_digest="x" * 64,
        )
        result_digest = _derive_case_result_digest(result)
        object.__setattr__(result, "result_digest", result_digest)
        return result

    @staticmethod
    def _compute_score(
        actual: BehaviorCaseOutputV1,
        expected: BehaviorCaseOutputV1,
    ) -> float:
        """Compute a comparison score between actual and expected output.

        Exact match = 1.0. Case-insensitive match = 1.0.
        Containment = 0.5. No match = 0.0.
        """
        if actual.output_digest == expected.output_digest:
            return 1.0
        actual_norm = actual.text.lower().strip()
        expected_norm = expected.text.lower().strip()
        if actual_norm == expected_norm:
            return 1.0
        if expected_norm in actual_norm or actual_norm in expected_norm:
            return 0.5
        return 0.0


# ---------------------------------------------------------------------------
# ShadowComparator — compare legacy/v2 outputs, isolated report only
# ---------------------------------------------------------------------------


class ShadowComparator:
    """Compares legacy and v2 outputs using the same de-identified inputs.

    v2 output is written only to the isolated ShadowReportV1.
    It must never produce business results or write to production stores.
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def compare(
        self,
        cases: Sequence[BehaviorCaseV1],
        legacy_trial: InjectedModelTrial,
        v2_trial: InjectedModelTrial,
        run_id: str | None = None,
    ) -> ShadowReportV1:
        """Run a shadow comparison between legacy and v2 trials.

        Same de-identified input is passed to both trials.
        v2 output only enters the isolated report.
        """
        import uuid

        actual_run_id = run_id or f"shadow-run-{uuid.uuid4().hex[:16]}"

        comparisons: list[ShadowComparisonV1] = []
        total_quality = 0.0
        total_latency_delta = 0.0
        total_token_delta = 0
        legacy_failures = 0
        v2_failures = 0

        if len(cases) > _MAX_SHADOW_COMPARISONS:
            _raise_error(SandboxEvaluationFailureCode.SHADOW_COMPARISON_LIMIT_EXCEEDED)

        for case in cases:
            legacy_failed = False
            v2_failed = False

            try:
                legacy_output = legacy_trial.generate(
                    case.input.input_digest,
                )
            except Exception:
                legacy_output = BehaviorCaseOutputV1.build(text="")
                legacy_failed = True
                legacy_failures += 1

            try:
                v2_output = v2_trial.generate(
                    case.input.input_digest,
                )
            except Exception:
                v2_output = BehaviorCaseOutputV1.build(text="")
                v2_failed = True
                v2_failures += 1

            if legacy_failed:
                legacy_metrics = BehaviorCaseMetricV1.build()
            else:
                legacy_metrics = BehaviorCaseMetricV1.build(latency_ms=2.0, total_tokens=20, model_calls=1)

            if v2_failed:
                v2_metrics = BehaviorCaseMetricV1.build()
            else:
                v2_metrics = BehaviorCaseMetricV1.build(latency_ms=1.0, total_tokens=15, model_calls=1)

            legacy_score = EvaluationSuite._compute_score(legacy_output, case.expected_output)
            v2_score = EvaluationSuite._compute_score(v2_output, case.expected_output)
            quality_score = min(1.0, v2_score / max(legacy_score, 0.001))

            latency_delta = v2_metrics.latency_ms - legacy_metrics.latency_ms
            token_delta = v2_metrics.total_tokens - legacy_metrics.total_tokens

            comp = ShadowComparisonV1.model_construct(
                case_id=case.case_id,
                dimension=case.dimension,
                legacy_output=legacy_output,
                v2_output=v2_output,
                legacy_metrics=legacy_metrics,
                v2_metrics=v2_metrics,
                quality_score=quality_score,
                latency_delta_ms=latency_delta,
                token_delta=token_delta,
                legacy_failed=(legacy_score < case.threshold) or legacy_failed,
                v2_failed=(v2_score < case.threshold) or v2_failed,
                comparison_digest="x" * 64,
            )
            comp_digest = _derive_comparison_digest(comp)
            object.__setattr__(comp, "comparison_digest", comp_digest)
            comparisons.append(comp)

            total_quality += quality_score
            total_latency_delta += latency_delta
            total_token_delta += token_delta

        count = len(comparisons)
        report = ShadowReportV1.model_construct(
            schema_version="sandbox-shadow.v1",
            adapter_version="sandbox-evaluation-adapter.v1",
            run_id=actual_run_id,
            comparisons=tuple(comparisons),
            total_comparisons=count,
            overall_quality=total_quality / count if count > 0 else 0.0,
            avg_latency_delta_ms=total_latency_delta / count if count > 0 else 0.0,
            avg_token_delta=total_token_delta / count if count > 0 else 0.0,
            legacy_failure_rate=legacy_failures / count if count > 0 else 0.0,
            v2_failure_rate=v2_failures / count if count > 0 else 0.0,
            v2_writes_prohibited=True,
            report_digest="x" * 64,
        )
        report_digest = _derive_report_digest(report)
        object.__setattr__(report, "report_digest", report_digest)
        return report

    def write_business_result(self, data: object) -> None:
        """Must never be called — shadow writes are prohibited.

        This method always raises SHADOW_WRITE_PROHIBITED.
        """
        _raise_error(SandboxEvaluationFailureCode.SHADOW_WRITE_PROHIBITED)


# ---------------------------------------------------------------------------
# RealModelTrialGate — default-off gate for real model calls
# ---------------------------------------------------------------------------


class RealModelTrialGate:
    """Gate that controls real model trial access.

    Default off. Without named external approval, budget, and data policy,
    returns external_gate_required and never initiates network model calls.
    """

    __slots__ = ("_gate", "_lock")

    def __init__(self, gate: RealModelTrialGateV1 | None = None) -> None:
        self._gate = gate or RealModelTrialGateV1.build()
        self._lock = threading.RLock()

    @property
    def gate(self) -> RealModelTrialGateV1:
        return self._gate

    def check(self) -> RealModelTrialGateV1:
        """Check if the gate allows real model trial.

        Returns the gate state. If not properly configured,
        gate_decision will be 'external_gate_required'.
        """
        with self._lock:
            if not self._gate.enabled:
                return _build_gate_state(
                    enabled=False,
                    external_approval=None,
                    budget_allocated=False,
                    data_policy_approved=False,
                    gate_decision="external_gate_required",
                )

            if self._gate.external_approval is None:
                return _build_gate_state(
                    enabled=True,
                    external_approval=None,
                    budget_allocated=self._gate.budget_allocated,
                    data_policy_approved=self._gate.data_policy_approved,
                    gate_decision="external_gate_required",
                )

            if not self._gate.budget_allocated:
                return _build_gate_state(
                    enabled=True,
                    external_approval=self._gate.external_approval,
                    budget_allocated=False,
                    data_policy_approved=self._gate.data_policy_approved,
                    gate_decision="external_gate_required",
                )

            if not self._gate.data_policy_approved:
                return _build_gate_state(
                    enabled=True,
                    external_approval=self._gate.external_approval,
                    budget_allocated=True,
                    data_policy_approved=False,
                    gate_decision="external_gate_required",
                )

            # All conditions met — gate is approved
            return _build_gate_state(
                enabled=True,
                external_approval=self._gate.external_approval,
                budget_allocated=True,
                data_policy_approved=True,
                gate_decision="approved",
            )

    def configure(
        self,
        *,
        external_approval: str,
        budget_allocated: bool = True,
        data_policy_approved: bool = True,
    ) -> None:
        """Configure the gate with external approval, budget, and data policy."""
        with self._lock:
            self._gate = _build_gate_state(
                enabled=True,
                external_approval=external_approval,
                budget_allocated=budget_allocated,
                data_policy_approved=data_policy_approved,
                gate_decision="configured",
            )
