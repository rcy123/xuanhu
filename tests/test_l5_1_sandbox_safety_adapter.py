from __future__ import annotations

import ast
import gc
import os
import platform
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_runtime.sandbox_safety import (
    MAX_FORMULA_ITEMS,
    MAX_ISSUES,
    SandboxFormulaItemV1,
    SandboxProfileFactV1,
    SandboxRuleBundleV1,
    SandboxRuleParameterV1,
    SandboxRuleV1,
    SandboxSafetyAdapterError,
    SandboxSafetyDecision,
    SandboxSafetyEvaluationV1,
    SandboxSafetyFailureCode,
    SandboxSafetyIssueV1,
    SandboxSafetyRuleAdapter,
    SandboxSafetySeverity,
    SandboxSafetySubjectV1,
    canonical_result_bytes,
)


@dataclass
class _FakeEvaluator:
    calls: int = 0
    exception_payload: str | None = None
    alternate: bool = False
    issue_count: int = 1

    def evaluate(
        self,
        subject: SandboxSafetySubjectV1,
        bundle: SandboxRuleBundleV1,
    ) -> SandboxSafetyEvaluationV1:
        del subject, bundle
        self.calls += 1
        if self.exception_payload is not None:
            raise RuntimeError(self.exception_payload)
        decision = (
            SandboxSafetyDecision.ALLOW
            if self.alternate and self.calls % 2 == 0
            else SandboxSafetyDecision.BLOCK
        )
        return SandboxSafetyEvaluationV1(
            decision=decision,
            issues=tuple(
                SandboxSafetyIssueV1(
                    issue_id=f"sandbox.issue.{index:03d}",
                    rule_id="sandbox.rule.fixed.v1",
                    severity=SandboxSafetySeverity.HIGH,
                    execution_order=index,
                )
                for index in range(self.issue_count)
            ),
        )


def _formula_items(count: int = 2) -> tuple[SandboxFormulaItemV1, ...]:
    return tuple(
        SandboxFormulaItemV1(
            item_id=f"synthetic-item-{index:03d}",
            component=f"fixed-fictitious-component-{index:03d}",
            amount_milliunits=index + 1,
            unit="synthetic_unit",
        )
        for index in range(count)
    )


def _profile_facts() -> tuple[SandboxProfileFactV1, ...]:
    return (
        SandboxProfileFactV1(
            fact_id="synthetic-profile-fact-001",
            name="fixed-fictitious-technical-profile",
            value="bounded-test-value",
        ),
    )


def _subject(
    *,
    item_count: int = 2,
    rule_bundle_digest: str | None = None,
) -> SandboxSafetySubjectV1:
    return SandboxSafetySubjectV1.build(
        test_session_id="sandbox-test-session-001",
        domain_state_version=7,
        formula_artifact_id="synthetic-formula-artifact-001",
        formula_revision=3,
        formula_items=_formula_items(item_count),
        profile_artifact_id="synthetic-profile-artifact-001",
        profile_revision=2,
        profile_facts=_profile_facts(),
        graph_version="sandbox-graph.v1",
        rule_bundle_version="sandbox-rule-bundle.v1",
        rule_bundle_digest=(
            _bundle().rule_bundle_digest if rule_bundle_digest is None else rule_bundle_digest
        ),
        synthetic_dataset_name="fixed-fictitious-manual-fixture",
        synthetic_dataset_version="1.0.0",
        dataset_provenance="fixed_fictitious_manual",
        dataset_usage_scope="sandbox_only",
        dataset_label_status="not_clinically_adjudicated",
    )


def _rules() -> tuple[SandboxRuleV1, ...]:
    return (
        SandboxRuleV1(
            rule_id="sandbox.rule.fixed.v1",
            rule_revision=1,
            parameters=(
                SandboxRuleParameterV1(name="threshold", value="fixed-technical-value"),
            ),
        ),
    )


def _bundle(*, rules: tuple[SandboxRuleV1, ...] | None = None) -> SandboxRuleBundleV1:
    return SandboxRuleBundleV1.build(
        rule_bundle_version="sandbox-rule-bundle.v1",
        rules=_rules() if rules is None else rules,
    )


def _run(
    evaluator: _FakeEvaluator,
    *,
    subject: object | None = None,
    bundle: object | None = None,
    command_id: str = "sandbox-command-001",
    run_id: str = "sandbox-run-001",
    trace_id: str = "sandbox-trace-001",
):
    return SandboxSafetyRuleAdapter(evaluator).evaluate(
        _subject() if subject is None else subject,
        _bundle() if bundle is None else bundle,
        command_id=command_id,
        run_id=run_id,
        trace_id=trace_id,
    )


def _assert_error(
    evaluator: _FakeEvaluator,
    code: SandboxSafetyFailureCode,
    *,
    subject: object | None = None,
    bundle: object | None = None,
) -> SandboxSafetyAdapterError:
    with pytest.raises(SandboxSafetyAdapterError) as raised:
        _run(evaluator, subject=subject, bundle=bundle)
    assert raised.value.code is code
    assert str(raised.value) == code.value
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


def _hash_seed_probe(seed: str) -> str:
    script = textwrap.dedent(
        """
        from app.agent_runtime.sandbox_safety import (
            SandboxFormulaItemV1, SandboxProfileFactV1, SandboxRuleBundleV1,
            SandboxRuleParameterV1, SandboxRuleV1, SandboxSafetyDecision,
            SandboxSafetyEvaluationV1, SandboxSafetyIssueV1,
            SandboxSafetyRuleAdapter, SandboxSafetySeverity,
            SandboxSafetySubjectV1, canonical_result_bytes,
        )

        bundle = SandboxRuleBundleV1.build(
            rule_bundle_version="sandbox-rule-bundle.v1",
            rules=(SandboxRuleV1(
                rule_id="sandbox.rule.fixed.v1",
                rule_revision=1,
                parameters=(SandboxRuleParameterV1(
                    name="threshold", value="fixed-technical-value"
                ),),
            ),),
        )
        subject = SandboxSafetySubjectV1.build(
            test_session_id="sandbox-test-session-001",
            domain_state_version=7,
            formula_artifact_id="synthetic-formula-artifact-001",
            formula_revision=3,
            formula_items=(SandboxFormulaItemV1(
                item_id="synthetic-item-000",
                component="fixed-fictitious-component-000",
                amount_milliunits=1,
                unit="synthetic_unit",
            ),),
            profile_artifact_id="synthetic-profile-artifact-001",
            profile_revision=2,
            profile_facts=(SandboxProfileFactV1(
                fact_id="synthetic-profile-fact-001",
                name="fixed-fictitious-technical-profile",
                value="bounded-test-value",
            ),),
            graph_version="sandbox-graph.v1",
            rule_bundle_version="sandbox-rule-bundle.v1",
            rule_bundle_digest=bundle.rule_bundle_digest,
            synthetic_dataset_name="fixed-fictitious-manual-fixture",
            synthetic_dataset_version="1.0.0",
            dataset_provenance="fixed_fictitious_manual",
            dataset_usage_scope="sandbox_only",
            dataset_label_status="not_clinically_adjudicated",
        )
        class Evaluator:
            def evaluate(self, subject, bundle):
                return SandboxSafetyEvaluationV1(
                    decision=SandboxSafetyDecision.BLOCK,
                    issues=(SandboxSafetyIssueV1(
                        issue_id="sandbox.issue.000",
                        rule_id="sandbox.rule.fixed.v1",
                        severity=SandboxSafetySeverity.HIGH,
                        execution_order=0,
                    ),),
                )

        result = SandboxSafetyRuleAdapter(Evaluator()).evaluate(
            subject,
            bundle,
            command_id="sandbox-command-001",
            run_id="sandbox-run-001",
            trace_id="sandbox-trace-001",
        )
        print(canonical_result_bytes(result).hex())
        """
    )
    minimal_env = {
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": str(Path.cwd()),
        "PYTHONUTF8": "1",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\\Windows"),
        "WINDIR": os.environ.get("WINDIR", r"C:\\Windows"),
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=minimal_env,
        timeout=30,
    )
    return completed.stdout.strip()


def _rss_bytes() -> int:
    if sys.platform != "win32":
        import resource

        multiplier = 1 if sys.platform == "darwin" else 1024
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * multiplier

    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    )
    get_process_memory_info.restype = wintypes.BOOL
    process = get_current_process()
    succeeded = get_process_memory_info(
        process,
        ctypes.byref(counters),
        counters.cb,
    )
    assert succeeded
    return int(counters.WorkingSetSize)


def test_l5_1_same_subject_and_bundle_produce_same_result_digest() -> None:
    first = _run(_FakeEvaluator())
    second = _run(_FakeEvaluator())

    assert first == second
    assert first.result_digest == second.result_digest
    assert canonical_result_bytes(first) == canonical_result_bytes(second)
    assert _hash_seed_probe("1") == _hash_seed_probe("99")


def test_l5_1_run_envelope_does_not_change_decision_digest() -> None:
    first = _run(
        _FakeEvaluator(),
        command_id="sandbox-command-001",
        run_id="sandbox-run-001",
        trace_id="sandbox-trace-001",
    )
    second = _run(
        _FakeEvaluator(),
        command_id="sandbox-command-002",
        run_id="sandbox-run-002",
        trace_id="sandbox-trace-002",
    )

    assert first.decision_subject_digest == second.decision_subject_digest
    assert first.result_digest == second.result_digest
    assert first.decision == second.decision
    assert first.issues == second.issues
    assert first.run_envelope_digest != second.run_envelope_digest


def test_l5_1_rule_bundle_digest_change_invalidates_subject() -> None:
    changed_rule = SandboxRuleV1(
        rule_id="sandbox.rule.fixed.v1",
        rule_revision=2,
        parameters=(SandboxRuleParameterV1(name="threshold", value="changed-technical-value"),),
    )
    evaluator = _FakeEvaluator()

    _assert_error(
        evaluator,
        SandboxSafetyFailureCode.DIGEST_MISMATCH,
        subject=_subject(),
        bundle=_bundle(rules=(changed_rule,)),
    )
    assert evaluator.calls == 0


def test_l5_1_missing_extra_parse_error_and_version_mismatch_fail_closed() -> None:
    valid = _subject().model_dump(mode="python")
    missing = dict(valid)
    missing.pop("profile_artifact_id")
    extra = {**valid, "caller_asserted_passed": True}
    mismatch = {**valid, "sandbox_schema_version": "sandbox-safety-subject.v999"}

    cases = (
        (missing, SandboxSafetyFailureCode.SCHEMA_INVALID),
        (extra, SandboxSafetyFailureCode.SCHEMA_INVALID),
        ("{bad-json", SandboxSafetyFailureCode.SCHEMA_INVALID),
        (mismatch, SandboxSafetyFailureCode.VERSION_MISMATCH),
    )
    for candidate, code in cases:
        evaluator = _FakeEvaluator()
        _assert_error(evaluator, code, subject=candidate)
        assert evaluator.calls == 0


def test_l5_1_stale_formula_or_profile_digest_rejected_before_evaluation() -> None:
    subject = _subject()
    stale_formula = subject.model_copy(update={"formula_content_digest": "0" * 64})
    stale_profile = subject.model_copy(update={"profile_content_digest": "1" * 64})

    for candidate in (stale_formula, stale_profile):
        evaluator = _FakeEvaluator()
        _assert_error(evaluator, SandboxSafetyFailureCode.DIGEST_MISMATCH, subject=candidate)
        assert evaluator.calls == 0


def test_l5_1_result_and_nested_issues_are_immutable() -> None:
    evaluator = _FakeEvaluator()
    result = _run(evaluator)

    with pytest.raises(ValidationError):
        result.decision = SandboxSafetyDecision.ALLOW  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.issues[0].severity = SandboxSafetySeverity.LOW  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.issues.append(result.issues[0])  # type: ignore[attr-defined]

    source_rules = list(_rules())
    bundle = SandboxRuleBundleV1.build(
        rule_bundle_version="sandbox-rule-bundle.v1",
        rules=source_rules,
    )
    source_rules.append(source_rules[0].model_copy(update={"rule_id": "sandbox.rule.alias.v1"}))
    assert len(bundle.rules) == 1


def test_l5_1_evaluator_exception_is_chainless_and_contains_no_payload() -> None:
    payload = "FIXTURE_PAYLOAD_DO_NOT_EXPOSE"
    evaluator = _FakeEvaluator(exception_payload=payload)
    error = _assert_error(evaluator, SandboxSafetyFailureCode.EVALUATOR_FAILED)

    rendered = f"{error!s} {error!r}"
    assert payload not in rendered
    assert evaluator.calls == 1

    nondeterministic = _FakeEvaluator(alternate=True)
    _assert_error(nondeterministic, SandboxSafetyFailureCode.EVALUATOR_NONDETERMINISTIC)
    assert nondeterministic.calls == 2


def test_l5_1_limit_plus_one_rejected_before_evaluator_call() -> None:
    assert MAX_FORMULA_ITEMS == 64
    evaluator = _FakeEvaluator()
    oversized_subject = _subject(item_count=MAX_FORMULA_ITEMS + 1)

    _assert_error(evaluator, SandboxSafetyFailureCode.LIMIT_EXCEEDED, subject=oversized_subject)
    assert evaluator.calls == 0

    too_many_issues = _FakeEvaluator(issue_count=MAX_ISSUES + 1)
    _assert_error(too_many_issues, SandboxSafetyFailureCode.LIMIT_EXCEEDED)
    assert too_many_issues.calls == 1


def test_l5_1_no_settings_env_data_gateway_review_record_export_or_network_import() -> None:
    module_path = Path("app/agent_runtime/sandbox_safety.py")
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
            imported_roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_roots.add(node.module.split(".", maxsplit=1)[0])

    assert imported_roots <= {
        "__future__",
        "collections",
        "enum",
        "hashlib",
        "json",
        "pydantic",
        "typing",
    }
    assert not any(
        name.startswith(("app.core", "app.db", "app.services", "app.repositories"))
        for name in imported_modules
    )
    assert ".env" not in source
    assert "data/" not in source
    assert _run(_FakeEvaluator()).decision is SandboxSafetyDecision.BLOCK


def test_l5_1_thousand_runs_are_reproducible_and_resource_bounded() -> None:
    evaluator = _FakeEvaluator()
    adapter = SandboxSafetyRuleAdapter(evaluator)
    subject = _subject(item_count=MAX_FORMULA_ITEMS)
    bundle = _bundle()
    expected = adapter.evaluate(
        subject,
        bundle,
        command_id="sandbox-command-resource",
        run_id="sandbox-run-resource",
        trace_id="sandbox-trace-resource",
    )
    for _ in range(20):
        assert (
            adapter.evaluate(
                subject,
                bundle,
                command_id="sandbox-command-resource",
                run_id="sandbox-run-resource",
                trace_id="sandbox-trace-resource",
            )
            == expected
        )

    gc.collect()
    rss_before = _rss_bytes()
    timings_ms: list[float] = []
    for _ in range(1_000):
        started = time.perf_counter_ns()
        actual = adapter.evaluate(
            subject,
            bundle,
            command_id="sandbox-command-resource",
            run_id="sandbox-run-resource",
            trace_id="sandbox-trace-resource",
        )
        timings_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        assert actual == expected
    gc.collect()
    rss_growth = max(0, _rss_bytes() - rss_before)
    ordered = sorted(timings_ms)
    p95_ms = ordered[949]
    p99_ms = ordered[989]

    sys.stdout.write(
        "L5_1_RESOURCE "
        f"python={platform.python_version()} "
        f"cpu={platform.machine()} "
        "samples=1000 warmup=20 method=perf_counter_ns+process_rss "
        f"p95_ms={p95_ms:.3f} p99_ms={p99_ms:.3f} rss_growth_bytes={rss_growth}\n"
    )
    assert p95_ms < 50
    assert p99_ms < 100
    assert rss_growth < 64 * 1024 * 1024
