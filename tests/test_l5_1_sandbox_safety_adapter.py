from __future__ import annotations

import ast
import copy
import gc
import hashlib
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
    MAX_CANONICAL_BYTES,
    MAX_FORMULA_ITEMS,
    MAX_ISSUES,
    SandboxEvaluationCaseV1,
    SandboxEvaluatorAuthorityV1,
    SandboxFormulaItemV1,
    SandboxIdentifierScanV1,
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
    SandboxSyntheticManifestV1,
    canonical_json_bytes,
    canonical_result_bytes,
)


@dataclass
class _StatefulCallable:
    calls: int = 0
    exception_payload: str | None = None
    alternate: bool = False

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
        return _evaluation(decision=decision)


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


def _issues(count: int = 1) -> tuple[SandboxSafetyIssueV1, ...]:
    return tuple(
        SandboxSafetyIssueV1(
            issue_id=f"sandbox.issue.{index:03d}",
            rule_id="sandbox.rule.fixed.v1",
            severity=SandboxSafetySeverity.HIGH,
            execution_order=index,
        )
        for index in range(count)
    )


def _evaluation(
    *,
    issue_count: int = 1,
    decision: SandboxSafetyDecision = SandboxSafetyDecision.BLOCK,
) -> SandboxSafetyEvaluationV1:
    return SandboxSafetyEvaluationV1(
        decision=decision,
        issues=_issues(issue_count),
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


def _manifest(*, item_count: int = 2) -> SandboxSyntheticManifestV1:
    return SandboxSyntheticManifestV1.build(
        dataset_name="fixed-fictitious-manual-fixture",
        dataset_version="1.0.0",
        formula_items=_formula_items(item_count),
        profile_facts=_profile_facts(),
    )


def _bundle(
    *,
    item_count: int = 2,
    issue_count: int = 1,
    decision: SandboxSafetyDecision = SandboxSafetyDecision.BLOCK,
    rules: tuple[SandboxRuleV1, ...] | None = None,
    manifest: SandboxSyntheticManifestV1 | None = None,
) -> SandboxRuleBundleV1:
    manifest = _manifest(item_count=item_count) if manifest is None else manifest
    evaluation_case = SandboxEvaluationCaseV1.build(
        case_id="fixed-fictitious-case-001",
        formula_items=_formula_items(item_count),
        profile_facts=_profile_facts(),
        manifest=manifest,
        evaluation=_evaluation(issue_count=issue_count, decision=decision),
    )
    authority = SandboxEvaluatorAuthorityV1.build(cases=(evaluation_case,))
    return SandboxRuleBundleV1.build(
        rule_bundle_version="sandbox-rule-bundle.v1",
        rules=_rules() if rules is None else rules,
        evaluator_authority=authority,
    )


def _subject(
    *,
    item_count: int = 2,
    issue_count: int = 1,
    decision: SandboxSafetyDecision = SandboxSafetyDecision.BLOCK,
    rule_bundle_digest: str | None = None,
    bundle: SandboxRuleBundleV1 | None = None,
    manifest: SandboxSyntheticManifestV1 | None = None,
) -> SandboxSafetySubjectV1:
    manifest = _manifest(item_count=item_count) if manifest is None else manifest
    bundle = (
        _bundle(
            item_count=item_count,
            issue_count=issue_count,
            decision=decision,
            manifest=manifest,
        )
        if bundle is None
        else bundle
    )
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
        rule_bundle_version=bundle.rule_bundle_version,
        rule_bundle_digest=(
            bundle.rule_bundle_digest if rule_bundle_digest is None else rule_bundle_digest
        ),
        evaluator_authority_digest=bundle.evaluator_authority.authority_digest,
        synthetic_manifest=manifest,
    )


def _fixture(
    *,
    item_count: int = 2,
    issue_count: int = 1,
    decision: SandboxSafetyDecision = SandboxSafetyDecision.BLOCK,
) -> tuple[SandboxSafetySubjectV1, SandboxRuleBundleV1]:
    manifest = _manifest(item_count=item_count)
    bundle = _bundle(
        item_count=item_count,
        issue_count=issue_count,
        decision=decision,
        manifest=manifest,
    )
    subject = _subject(
        item_count=item_count,
        issue_count=issue_count,
        decision=decision,
        bundle=bundle,
        manifest=manifest,
    )
    return subject, bundle


def _run(
    *,
    subject: object | None = None,
    bundle: object | None = None,
    command_id: str = "sandbox-command-001",
    run_id: str = "sandbox-run-001",
    trace_id: str = "sandbox-trace-001",
):
    default_subject, default_bundle = _fixture()
    return SandboxSafetyRuleAdapter().evaluate(
        default_subject if subject is None else subject,
        default_bundle if bundle is None else bundle,
        command_id=command_id,
        run_id=run_id,
        trace_id=trace_id,
    )


def _assert_error(
    code: SandboxSafetyFailureCode,
    *,
    subject: object | None = None,
    bundle: object | None = None,
) -> SandboxSafetyAdapterError:
    with pytest.raises(SandboxSafetyAdapterError) as raised:
        _run(subject=subject, bundle=bundle)
    assert raised.value.code is code
    assert str(raised.value) == code.value
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


def _hash_seed_probe(seed: str) -> str:
    script = textwrap.dedent(
        """
        import runpy
        from app.agent_runtime.sandbox_safety import (
            SandboxSafetyRuleAdapter, canonical_result_bytes,
        )

        namespace = runpy.run_path("tests/test_l5_1_sandbox_safety_adapter.py")
        subject, bundle = namespace["_fixture"]()
        result = SandboxSafetyRuleAdapter().evaluate(
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
    first = _run()
    second = _run()

    assert first == second
    assert first.result_digest == second.result_digest
    assert canonical_result_bytes(first) == canonical_result_bytes(second)
    assert _hash_seed_probe("1") == _hash_seed_probe("99")


def test_l5_1_run_envelope_does_not_change_decision_digest() -> None:
    first = _run(
        command_id="sandbox-command-001",
        run_id="sandbox-run-001",
        trace_id="sandbox-trace-001",
    )
    second = _run(
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
    subject, _ = _fixture()
    changed_rule = SandboxRuleV1(
        rule_id="sandbox.rule.fixed.v1",
        rule_revision=2,
        parameters=(SandboxRuleParameterV1(name="threshold", value="changed-technical-value"),),
    )
    changed_bundle = _bundle(rules=(changed_rule,))

    _assert_error(
        SandboxSafetyFailureCode.DIGEST_MISMATCH,
        subject=subject,
        bundle=changed_bundle,
    )

    allow_bundle = _bundle(decision=SandboxSafetyDecision.ALLOW)
    assert allow_bundle.evaluator_authority.authority_digest != (
        _bundle().evaluator_authority.authority_digest
    )
    _assert_error(
        SandboxSafetyFailureCode.DIGEST_MISMATCH,
        subject=subject,
        bundle=allow_bundle,
    )


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
        _assert_error(code, subject=candidate)


def test_l5_1_stale_formula_or_profile_digest_rejected_before_evaluation() -> None:
    subject = _subject()
    stale_formula = subject.model_copy(update={"formula_content_digest": "0" * 64})
    stale_profile = subject.model_copy(update={"profile_content_digest": "1" * 64})

    for candidate in (stale_formula, stale_profile):
        _assert_error(SandboxSafetyFailureCode.DIGEST_MISMATCH, subject=candidate)


def test_l5_1_result_and_nested_issues_are_immutable() -> None:
    subject, bundle = _fixture()
    result = _run(subject=subject, bundle=bundle)

    with pytest.raises(ValidationError):
        result.decision = SandboxSafetyDecision.ALLOW  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.issues[0].severity = SandboxSafetySeverity.LOW  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.issues.append(result.issues[0])  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        subject.synthetic_manifest.case_count = 2  # type: ignore[misc,assignment]
    with pytest.raises(ValidationError):
        bundle.evaluator_authority.cases[0].evaluation.decision = (  # type: ignore[misc]
            SandboxSafetyDecision.ALLOW
        )

    source_rules = list(_rules())
    authority = bundle.evaluator_authority
    copied_bundle = SandboxRuleBundleV1.build(
        rule_bundle_version="sandbox-rule-bundle.v1",
        rules=source_rules,
        evaluator_authority=authority,
    )
    source_rules.append(source_rules[0].model_copy(update={"rule_id": "sandbox.rule.alias.v1"}))
    assert len(copied_bundle.rules) == 1


def test_l5_1_evaluator_exception_is_chainless_and_contains_no_payload() -> None:
    payload = "FIXTURE_PAYLOAD_DO_NOT_EXPOSE"
    callable_authority = _StatefulCallable(exception_payload=payload)

    with pytest.raises(TypeError) as raised:
        SandboxSafetyRuleAdapter(callable_authority)  # type: ignore[call-arg]

    assert payload not in f"{raised.value!s} {raised.value!r}"
    assert callable_authority.calls == 0
    assert not hasattr(SandboxSafetyRuleAdapter(), "_evaluator")


def test_l5_1_limit_plus_one_rejected_before_evaluator_call() -> None:
    assert MAX_FORMULA_ITEMS == 64
    bounded_subject, oversized_bundle = _fixture(item_count=MAX_FORMULA_ITEMS)
    subject_fields = {
        field_name: getattr(bounded_subject, field_name)
        for field_name in SandboxSafetySubjectV1.model_fields
    }
    oversized_subject = SandboxSafetySubjectV1.model_construct(
        **{
            **subject_fields,
            "formula_items": _formula_items(MAX_FORMULA_ITEMS + 1),
        }
    )
    _assert_error(
        SandboxSafetyFailureCode.LIMIT_EXCEEDED,
        subject=oversized_subject,
        bundle=oversized_bundle,
    )

    too_many_subject, too_many_bundle = _fixture(issue_count=MAX_ISSUES + 1)
    _assert_error(
        SandboxSafetyFailureCode.LIMIT_EXCEEDED,
        subject=too_many_subject,
        bundle=too_many_bundle,
    )


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
        "re",
        "typing",
        "unicodedata",
    }
    assert not any(
        name.startswith(("app.core", "app.db", "app.services", "app.repositories"))
        for name in imported_modules
    )
    assert "SandboxSafetyEvaluator" not in source
    assert ".env" not in source
    assert "data/" not in source
    assert _run().decision is SandboxSafetyDecision.BLOCK


def test_l5_1_thousand_runs_are_reproducible_and_resource_bounded() -> None:
    subject, bundle = _fixture(item_count=MAX_FORMULA_ITEMS)
    adapter = SandboxSafetyRuleAdapter()
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
        "formula_items=64 issues=1 samples=1000 warmup=20 "
        "method=perf_counter_ns+process_rss "
        f"p95_ms={p95_ms:.3f} p99_ms={p99_ms:.3f} rss_growth_bytes={rss_growth}\n"
    )
    assert p95_ms < 50
    assert p99_ms < 100
    assert rss_growth < 64 * 1024 * 1024


def test_l5_1_pairwise_state_drift_cannot_change_identical_request_result() -> None:
    @dataclass
    class PairwiseDriftEvaluator:
        calls: int = 0

        def evaluate(
            self,
            subject: SandboxSafetySubjectV1,
            bundle: SandboxRuleBundleV1,
        ) -> SandboxSafetyEvaluationV1:
            del subject, bundle
            self.calls += 1
            decision = (
                SandboxSafetyDecision.BLOCK
                if self.calls <= 2
                else SandboxSafetyDecision.ALLOW
            )
            return _evaluation(decision=decision)

    attacker = PairwiseDriftEvaluator()
    with pytest.raises(TypeError):
        SandboxSafetyRuleAdapter(attacker)  # type: ignore[call-arg]
    assert attacker.calls == 0

    subject, bundle = _fixture()
    adapter = SandboxSafetyRuleAdapter()
    results = tuple(
        adapter.evaluate(
            subject,
            bundle,
            command_id="sandbox-command-pairwise",
            run_id="sandbox-run-pairwise",
            trace_id="sandbox-trace-pairwise",
        )
        for _ in range(4)
    )
    assert len({canonical_result_bytes(result) for result in results}) == 1
    assert len({result.result_digest for result in results}) == 1


def test_l5_1_arbitrary_stateful_callable_is_not_a_deterministic_authority() -> None:
    callable_authority = _StatefulCallable(alternate=True)

    with pytest.raises(TypeError):
        SandboxSafetyRuleAdapter(callable_authority)  # type: ignore[call-arg]

    assert callable_authority.calls == 0
    assert SandboxSafetyRuleAdapter.__slots__ == ()


def test_l5_1_inline_fixture_manifest_is_complete_strict_and_digest_bound() -> None:
    subject, bundle = _fixture()
    manifest = subject.synthetic_manifest
    scan = manifest.prohibited_identifier_scan
    fixture_content = {
        "cases": (
            {
                "formula_items": subject.formula_items,
                "profile_facts": subject.profile_facts,
            },
        )
    }
    expected_content_digest = hashlib.sha256(canonical_json_bytes(fixture_content)).hexdigest()
    expected_manifest_digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    expected_dataset_digest = hashlib.sha256(
        canonical_json_bytes({"fixture_content": fixture_content, "manifest": manifest})
    ).hexdigest()

    assert manifest.schema_version == "sandbox-synthetic-manifest.v2"
    assert manifest.dataset_name == subject.synthetic_dataset_name
    assert manifest.dataset_version == subject.synthetic_dataset_version
    assert manifest.admission_scope == "personal_learning_synthetic_only"
    assert manifest.provenance_type == "constructed_fixture"
    assert manifest.fixture_provenance == "fixed_fictitious_manual"
    assert manifest.usage_scope == "sandbox_only"
    assert manifest.source_statement == (
        "not_from_real_medical_records_personal_records_production_logs_chat_records_or_external_datasets"
    )
    assert {
        manifest.generator_path,
        manifest.generator_version,
        manifest.generator_digest,
        manifest.seed,
    } == {"not_applicable"}
    assert manifest.construction_evidence == (
        "manually_constructed_fixed_fictitious_technical_fixture"
    )
    assert manifest.case_count == 1
    assert manifest.content_sha256 == expected_content_digest
    assert manifest.created_at == "2000-01-01T00:00:00Z"
    assert manifest.created_by_test_role == "sandbox_fixture_author"
    assert scan == SandboxIdentifierScanV1.passed()
    assert scan.result == "passed_no_configured_identifier_pattern_matches"
    assert manifest.label_status == "not_clinically_adjudicated"
    assert subject.synthetic_manifest_digest == expected_manifest_digest
    assert subject.synthetic_dataset_digest == expected_dataset_digest
    assert _run(subject=subject, bundle=bundle).decision is SandboxSafetyDecision.BLOCK

    with pytest.raises(ValidationError):
        manifest.dataset_name = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        scan.result = "failed"  # type: ignore[misc,assignment]

    valid = subject.model_dump(mode="python")

    def changed_manifest(field: str, value: object, *, remove: bool = False) -> dict[str, object]:
        candidate = copy.deepcopy(valid)
        raw_manifest = candidate["synthetic_manifest"]
        assert isinstance(raw_manifest, dict)
        if remove:
            raw_manifest.pop(field)
        else:
            raw_manifest[field] = value
        return candidate

    schema_invalid = (
        changed_manifest("dataset_name", None, remove=True),
        changed_manifest("admission_scope", None, remove=True),
        changed_manifest("generator_digest", None, remove=True),
        changed_manifest("prohibited_identifier_scan", None, remove=True),
        changed_manifest("label_status", None, remove=True),
        changed_manifest("unexpected", "forbidden"),
        changed_manifest("provenance_type", "external_dataset"),
        changed_manifest("case_count", 2),
    )
    failed_scan = copy.deepcopy(valid)
    raw_manifest = failed_scan["synthetic_manifest"]
    assert isinstance(raw_manifest, dict)
    raw_scan = raw_manifest["prohibited_identifier_scan"]
    assert isinstance(raw_scan, dict)
    raw_scan["result"] = "failed_matches_present"

    for candidate in (*schema_invalid, failed_scan):
        _assert_error(
            SandboxSafetyFailureCode.SCHEMA_INVALID,
            subject=candidate,
            bundle=bundle,
        )

    bad_content = changed_manifest("content_sha256", "0" * 64)
    _assert_error(
        SandboxSafetyFailureCode.DIGEST_MISMATCH,
        subject=bad_content,
        bundle=bundle,
    )
    bad_manifest_digest = subject.model_copy(update={"synthetic_manifest_digest": "1" * 64})
    _assert_error(
        SandboxSafetyFailureCode.DIGEST_MISMATCH,
        subject=bad_manifest_digest,
        bundle=bundle,
    )


def test_l5_1_thousand_true_maximum_results_are_resource_bounded() -> None:
    subject, bundle = _fixture(item_count=MAX_FORMULA_ITEMS, issue_count=MAX_ISSUES)
    adapter = SandboxSafetyRuleAdapter()
    expected = adapter.evaluate(
        subject,
        bundle,
        command_id="sandbox-command-true-maximum",
        run_id="sandbox-run-true-maximum",
        trace_id="sandbox-trace-true-maximum",
    )
    expected_bytes = canonical_result_bytes(expected)
    assert len(subject.formula_items) == MAX_FORMULA_ITEMS
    assert len(expected.issues) == MAX_ISSUES
    assert len({issue.issue_id for issue in expected.issues}) == MAX_ISSUES
    assert len(canonical_json_bytes(subject)) <= MAX_CANONICAL_BYTES
    assert len(canonical_json_bytes(bundle)) <= MAX_CANONICAL_BYTES
    assert len(expected_bytes) <= MAX_CANONICAL_BYTES

    for _ in range(20):
        actual = adapter.evaluate(
            subject,
            bundle,
            command_id="sandbox-command-true-maximum",
            run_id="sandbox-run-true-maximum",
            trace_id="sandbox-trace-true-maximum",
        )
        assert canonical_result_bytes(actual) == expected_bytes

    gc.collect()
    rss_before = _rss_bytes()
    timings_ms: list[float] = []
    for _ in range(1_000):
        started = time.perf_counter_ns()
        actual = adapter.evaluate(
            subject,
            bundle,
            command_id="sandbox-command-true-maximum",
            run_id="sandbox-run-true-maximum",
            trace_id="sandbox-trace-true-maximum",
        )
        timings_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        assert canonical_result_bytes(actual) == expected_bytes
    gc.collect()
    rss_growth = max(0, _rss_bytes() - rss_before)
    ordered = sorted(timings_ms)
    p95_ms = ordered[949]
    p99_ms = ordered[989]
    processor = platform.processor().replace(" ", "_") or platform.machine()

    sys.stdout.write(
        "L5_1_R1_RESOURCE "
        f"python={platform.python_version()} cpu={processor} "
        f"formula_items={len(subject.formula_items)} issues={len(expected.issues)} "
        f"subject_bytes={len(canonical_json_bytes(subject))} "
        f"bundle_bytes={len(canonical_json_bytes(bundle))} result_bytes={len(expected_bytes)} "
        "samples=1000 warmup=20 method=perf_counter_ns+process_working_set_rss "
        f"p95_ms={p95_ms:.3f} p99_ms={p99_ms:.3f} rss_growth_bytes={rss_growth}\n"
    )
    assert p95_ms < 50
    assert p99_ms < 100
    assert rss_growth < 64 * 1024 * 1024
