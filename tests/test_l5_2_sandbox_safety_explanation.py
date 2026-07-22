from __future__ import annotations

import ast
import gc
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_runtime.sandbox_explanation import (
    MAX_EXPLANATION_BYTES,
    MAX_EXPLANATION_ISSUES,
    SANDBOX_EXPLANATION_DISCLAIMER,
    SandboxExplanationAllowlistBundleV1,
    SandboxExplanationAllowlistEntryV1,
    SandboxExplanationCandidateStatementV1,
    SandboxExplanationCandidateV1,
    SandboxExplanationIssueRefV1,
    SandboxExplanationPortInputV1,
    SandboxExplanationResultV1,
    SandboxExplanationStatus,
    SandboxSafetyExplanationAgent,
    canonical_explanation_bytes,
)
from app.agent_runtime.sandbox_safety import (
    SandboxEvaluationCaseV1,
    SandboxEvaluatorAuthorityV1,
    SandboxFormulaItemV1,
    SandboxProfileFactV1,
    SandboxRuleBundleV1,
    SandboxRuleV1,
    SandboxSafetyDecision,
    SandboxSafetyEvaluationV1,
    SandboxSafetyIssueV1,
    SandboxSafetyResultV1,
    SandboxSafetyRuleAdapter,
    SandboxSafetySeverity,
    SandboxSafetySubjectV1,
    SandboxSyntheticManifestV1,
    canonical_result_bytes,
)


class _FakeExplanationPort:
    def __init__(
        self,
        response: object | Callable[[SandboxExplanationPortInputV1], object],
        *,
        capture_inputs: bool = True,
    ) -> None:
        self.response = response
        self.capture_inputs = capture_inputs
        self.calls = 0
        self.inputs: list[SandboxExplanationPortInputV1] = []

    def generate(self, request: SandboxExplanationPortInputV1) -> object:
        self.calls += 1
        if self.capture_inputs:
            self.inputs.append(request)
        if callable(self.response):
            return self.response(request)
        return self.response


def _formula_items() -> tuple[SandboxFormulaItemV1, ...]:
    return (
        SandboxFormulaItemV1(
            item_id="synthetic-item-001",
            component="fixed-fictitious-component",
            amount_milliunits=1,
            unit="synthetic_unit",
        ),
    )


def _profile_facts() -> tuple[SandboxProfileFactV1, ...]:
    return (
        SandboxProfileFactV1(
            fact_id="synthetic-profile-fact-001",
            name="fixed-fictitious-technical-profile",
            value="bounded-test-value",
        ),
    )


def _rule_id(index: int) -> str:
    return f"sx.r.{index:03d}"


def _rules(count: int) -> tuple[SandboxRuleV1, ...]:
    return tuple(
        SandboxRuleV1(
            rule_id=_rule_id(index),
            rule_revision=1,
            parameters=(),
        )
        for index in range(count)
    )


def _issues(count: int) -> tuple[SandboxSafetyIssueV1, ...]:
    return tuple(
        SandboxSafetyIssueV1(
            issue_id=f"sx.i.{index:03d}",
            rule_id=_rule_id(index),
            severity=(
                SandboxSafetySeverity.HIGH
                if index % 2 == 0
                else SandboxSafetySeverity.MEDIUM
            ),
            execution_order=index,
        )
        for index in range(count)
    )


def _source_result(
    issue_count: int = 3,
    *,
    decision: SandboxSafetyDecision = SandboxSafetyDecision.BLOCK,
) -> SandboxSafetyResultV1:
    formula_items = _formula_items()
    profile_facts = _profile_facts()
    manifest = SandboxSyntheticManifestV1.build(
        dataset_name="fixed-fictitious-explanation-fixture",
        dataset_version="1.0.0",
        formula_items=formula_items,
        profile_facts=profile_facts,
    )
    evaluation_case = SandboxEvaluationCaseV1.build(
        case_id="fixed-fictitious-explanation-case-001",
        formula_items=formula_items,
        profile_facts=profile_facts,
        manifest=manifest,
        evaluation=SandboxSafetyEvaluationV1(
            decision=decision,
            issues=_issues(issue_count),
        ),
    )
    authority = SandboxEvaluatorAuthorityV1.build(cases=(evaluation_case,))
    bundle = SandboxRuleBundleV1.build(
        rule_bundle_version="sandbox-explanation-rules.v1",
        rules=_rules(issue_count),
        evaluator_authority=authority,
    )
    subject = SandboxSafetySubjectV1.build(
        test_session_id="sandbox-explanation-session-001",
        domain_state_version=1,
        formula_artifact_id="synthetic-explanation-formula-001",
        formula_revision=1,
        formula_items=formula_items,
        profile_artifact_id="synthetic-explanation-profile-001",
        profile_revision=1,
        profile_facts=profile_facts,
        graph_version="sandbox-explanation-graph.v1",
        rule_bundle_version=bundle.rule_bundle_version,
        rule_bundle_digest=bundle.rule_bundle_digest,
        evaluator_authority_digest=authority.authority_digest,
        synthetic_manifest=manifest,
    )
    return SandboxSafetyRuleAdapter().evaluate(
        subject,
        bundle,
        command_id="sandbox-explanation-command-001",
        run_id="sandbox-explanation-run-001",
        trace_id="sandbox-explanation-trace-001",
    )


def _allowlisted_text(index: int) -> str:
    return f"fixed sandbox explanation for rule {index:03d}"


def _allowlist(
    source: SandboxSafetyResultV1,
    *,
    texts: tuple[str, ...] | None = None,
    extra_entries: tuple[SandboxExplanationAllowlistEntryV1, ...] = (),
) -> SandboxExplanationAllowlistBundleV1:
    unique_rule_ids = tuple(sorted({issue.rule_id for issue in source.issues}))
    if texts is None:
        texts = tuple(_allowlisted_text(index) for index in range(len(unique_rule_ids)))
    assert len(texts) == len(unique_rule_ids)
    entries = tuple(
        SandboxExplanationAllowlistEntryV1(rule_id=rule_id, text=text)
        for rule_id, text in zip(unique_rule_ids, texts, strict=True)
    )
    return SandboxExplanationAllowlistBundleV1.build(entries=(*entries, *extra_entries))


def _candidate(
    source: SandboxSafetyResultV1,
    allowlist: SandboxExplanationAllowlistBundleV1,
    *,
    issue_indexes: tuple[int, ...] | None = None,
) -> SandboxExplanationCandidateV1:
    text_by_rule = {entry.rule_id: entry.text for entry in allowlist.entries}
    indexes = range(len(source.issues)) if issue_indexes is None else issue_indexes
    return SandboxExplanationCandidateV1(
        statements=tuple(
            SandboxExplanationCandidateStatementV1(
                issue_id=source.issues[index].issue_id,
                rule_id=source.issues[index].rule_id,
                text=text_by_rule[source.issues[index].rule_id],
            )
            for index in indexes
        )
    )


def _explain(
    source: SandboxSafetyResultV1,
    allowlist: object,
    response: object | Callable[[SandboxExplanationPortInputV1], object],
) -> tuple[SandboxExplanationResultV1, _FakeExplanationPort]:
    port = _FakeExplanationPort(response)
    result = SandboxSafetyExplanationAgent(port).explain(source, allowlist)
    return result, port


def _assert_unavailable(result: SandboxExplanationResultV1, source_digest: str) -> None:
    assert result.source_result_digest == source_digest
    assert result.status is SandboxExplanationStatus.EXPLANATION_UNAVAILABLE
    assert result.statements == ()
    assert result.disclaimer == SANDBOX_EXPLANATION_DISCLAIMER
    assert len(result.explanation_digest) == 64
    assert set(type(result).model_fields) == {
        "source_result_digest",
        "status",
        "statements",
        "disclaimer",
        "explanation_digest",
    }


def _raise(error: Exception) -> Callable[[SandboxExplanationPortInputV1], object]:
    def raise_error(request: SandboxExplanationPortInputV1) -> object:
        del request
        raise error

    return raise_error


def _with_candidate_size(
    source: SandboxSafetyResultV1,
    target_bytes: int,
) -> tuple[SandboxExplanationAllowlistBundleV1, SandboxExplanationCandidateV1]:
    base_text = "x"
    base_allowlist = _allowlist(source, texts=(base_text,))
    base_candidate = _candidate(source, base_allowlist)
    growth = target_bytes - len(canonical_explanation_bytes(base_candidate))
    assert growth >= 0
    text = base_text + ("x" * growth)
    allowlist = _allowlist(source, texts=(text,))
    candidate = _candidate(source, allowlist)
    assert len(canonical_explanation_bytes(candidate)) == target_bytes
    return allowlist, candidate


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


def test_l5_2_valid_exact_references_attach_without_changing_l5_1_result() -> None:
    source = _source_result()
    source_before = canonical_result_bytes(source)
    allowlist = _allowlist(source)
    candidate = _candidate(source, allowlist, issue_indexes=(0, 2))

    result, port = _explain(source, allowlist, candidate)

    assert result.status is SandboxExplanationStatus.ATTACHED
    assert result.source_result_digest == source.result_digest
    assert result.disclaimer == "sandbox_test_only_not_medical_advice"
    assert tuple(statement.issue_id for statement in result.statements) == (
        source.issues[0].issue_id,
        source.issues[2].issue_id,
    )
    assert tuple(statement.text for statement in result.statements) == (
        allowlist.entries[0].text,
        allowlist.entries[2].text,
    )
    assert result.explanation_digest != source.result_digest
    assert port.calls == 1
    assert canonical_result_bytes(source) == source_before
    assert set(result.model_dump(mode="python")) == {
        "source_result_digest",
        "status",
        "statements",
        "disclaimer",
        "explanation_digest",
    }
    assert not hasattr(result, "decision")
    assert not hasattr(result, "severity")
    assert not hasattr(result, "issues")


def test_l5_2_attempted_decision_severity_or_issue_mutation_is_unavailable() -> None:
    source = _source_result(issue_count=1)
    allowlist = _allowlist(source)
    statement = _candidate(source, allowlist).statements[0].model_dump(mode="python")
    attempted_mutations = (
        {"statements": (statement,), "decision": "allow"},
        {"statements": ({**statement, "severity": "low"},)},
        {"statements": (statement,), "issues": ()},
        {"statements": (statement,), "disposition": "continue"},
        {"statements": (statement,), "formula": "forbidden-candidate-payload"},
        {"statements": (statement,), "review_action": "confirm"},
        {"statements": (statement,), "result_digest": "0" * 64},
    )

    unavailable_bytes: set[bytes] = set()
    for malicious in attempted_mutations:
        result, port = _explain(source, allowlist, malicious)
        _assert_unavailable(result, source.result_digest)
        unavailable_bytes.add(canonical_explanation_bytes(result))
        assert port.calls == 1
    assert len(unavailable_bytes) == 1


def test_l5_2_unsupported_text_wrong_reference_and_duplicate_are_unavailable() -> None:
    source = _source_result(issue_count=2)
    allowlist = _allowlist(source)
    valid = _candidate(source, allowlist)
    first = valid.statements[0].model_dump(mode="python")
    second = valid.statements[1].model_dump(mode="python")
    invalid_candidates = (
        {"statements": ({**first, "text": f"{first['text']} extra advice"},)},
        {"statements": ({**first, "issue_id": "sandbox.issue.unknown"},)},
        {"statements": ({**first, "rule_id": source.issues[1].rule_id},)},
        {"statements": (first, first)},
        {"statements": (first, second, first)},
    )

    expected_unavailable: bytes | None = None
    for candidate in invalid_candidates:
        result, _ = _explain(source, allowlist, candidate)
        _assert_unavailable(result, source.result_digest)
        actual = canonical_explanation_bytes(result)
        expected_unavailable = actual if expected_unavailable is None else expected_unavailable
        assert actual == expected_unavailable


def test_l5_2_timeout_exception_bad_schema_are_fixed_chainless_unavailable() -> None:
    source = _source_result(issue_count=1)
    allowlist = _allowlist(source)
    unsafe_marker = "forbidden-candidate-or-exception-payload"
    failures: tuple[object | Callable[[SandboxExplanationPortInputV1], object], ...] = (
        _raise(TimeoutError(unsafe_marker)),
        _raise(RuntimeError(unsafe_marker)),
        None,
        "{not-json",
        b"[1,2,3]",
        {},
        {"statements": ({"issue_id": source.issues[0].issue_id},)},
        {"statements": (), "unknown": unsafe_marker},
    )

    unavailable_bytes: set[bytes] = set()
    for failure in failures:
        result, port = _explain(source, allowlist, failure)
        _assert_unavailable(result, source.result_digest)
        unavailable_bytes.add(canonical_explanation_bytes(result))
        assert port.calls == 1
        assert unsafe_marker not in repr(result)
    assert len(unavailable_bytes) == 1


def test_l5_2_issue_limit_plus_one_rejected_before_port_call() -> None:
    source = _source_result(issue_count=MAX_EXPLANATION_ISSUES + 1)
    allowlist = _allowlist(source)
    port = _FakeExplanationPort({"statements": ()})

    result = SandboxSafetyExplanationAgent(port).explain(source, allowlist)

    assert len(source.issues) == 65
    _assert_unavailable(result, source.result_digest)
    assert port.calls == 0


def test_l5_2_output_8kib_plus_one_is_unavailable_without_truncation() -> None:
    source = _source_result(issue_count=1)
    small_allowlist = _allowlist(source, texts=("x",))
    small_candidate = _candidate(source, small_allowlist)
    small_result, _ = _explain(source, small_allowlist, small_candidate)
    assert small_result.status is SandboxExplanationStatus.ATTACHED

    exact_text = "x" + ("x" * (MAX_EXPLANATION_BYTES - len(canonical_explanation_bytes(small_result))))
    exact_allowlist = _allowlist(source, texts=(exact_text,))
    exact_candidate = _candidate(source, exact_allowlist)
    exact_result, _ = _explain(source, exact_allowlist, exact_candidate)
    assert exact_result.status is SandboxExplanationStatus.ATTACHED
    assert len(canonical_explanation_bytes(exact_result)) == MAX_EXPLANATION_BYTES

    plus_one_text = f"{exact_text}x"
    plus_one_allowlist = _allowlist(source, texts=(plus_one_text,))
    plus_one_candidate = _candidate(source, plus_one_allowlist)
    plus_one_result, plus_one_port = _explain(
        source,
        plus_one_allowlist,
        plus_one_candidate,
    )
    _assert_unavailable(plus_one_result, source.result_digest)
    assert plus_one_port.calls == 1
    assert len(plus_one_result.statements) == 0

    oversized_allowlist, oversized_candidate = _with_candidate_size(
        source,
        MAX_EXPLANATION_BYTES + 1,
    )
    oversized_result, oversized_port = _explain(
        source,
        oversized_allowlist,
        oversized_candidate,
    )
    _assert_unavailable(oversized_result, source.result_digest)
    assert oversized_port.calls == 1
    assert len(canonical_explanation_bytes(oversized_candidate)) == 8 * 1024 + 1


def test_l5_2_result_and_nested_statements_are_immutable() -> None:
    source = _source_result(issue_count=1)
    allowlist = _allowlist(source)
    candidate = _candidate(source, allowlist)
    result, port = _explain(source, allowlist, candidate)
    assert result.status is SandboxExplanationStatus.ATTACHED
    request = port.inputs[0]

    with pytest.raises(ValidationError):
        result.status = SandboxExplanationStatus.EXPLANATION_UNAVAILABLE
    with pytest.raises(ValidationError):
        result.statements[0].text = "changed"
    with pytest.raises(ValidationError):
        request.decision = SandboxSafetyDecision.ALLOW
    with pytest.raises(ValidationError):
        request.issue_refs[0].severity = SandboxSafetySeverity.LOW
    with pytest.raises(ValidationError):
        allowlist.entries[0].text = "changed"
    assert isinstance(result.statements, tuple)
    assert isinstance(request.issue_refs, tuple)
    assert isinstance(request.allowlist_entries, tuple)


def test_l5_2_source_result_and_digest_are_byte_identical_across_all_paths() -> None:
    source = _source_result(issue_count=2)
    allowlist = _allowlist(source)
    before_bytes = canonical_result_bytes(source)
    before_fields = (
        source.decision,
        source.issues,
        source.decision_subject_digest,
        source.run_envelope_digest,
        source.result_digest,
    )
    paths: tuple[object | Callable[[SandboxExplanationPortInputV1], object], ...] = (
        _candidate(source, allowlist),
        {"statements": (), "decision": "allow"},
        {"statements": ({"issue_id": "wrong", "rule_id": "wrong", "text": "wrong"},)},
        _raise(TimeoutError("hidden-timeout-payload")),
        _raise(RuntimeError("hidden-exception-payload")),
        None,
        "{bad-json",
    )

    for response in paths:
        SandboxSafetyExplanationAgent(_FakeExplanationPort(response)).explain(
            source,
            allowlist,
        )
        assert canonical_result_bytes(source) == before_bytes
        assert (
            source.decision,
            source.issues,
            source.decision_subject_digest,
            source.run_envelope_digest,
            source.result_digest,
        ) == before_fields

    no_issue_source = _source_result(issue_count=0, decision=SandboxSafetyDecision.ALLOW)
    no_issue_before = canonical_result_bytes(no_issue_source)
    no_issue_port = _FakeExplanationPort({"statements": ()})
    no_issue_result = SandboxSafetyExplanationAgent(no_issue_port).explain(
        no_issue_source,
        SandboxExplanationAllowlistBundleV1.build(entries=()),
    )
    _assert_unavailable(no_issue_result, no_issue_source.result_digest)
    assert no_issue_port.calls == 0
    assert canonical_result_bytes(no_issue_source) == no_issue_before


def test_l5_2_port_input_is_minimal_and_contains_no_fixture_or_secret_fields() -> None:
    source = _source_result(issue_count=2)
    allowlist = _allowlist(source)
    candidate = _candidate(source, allowlist, issue_indexes=(1,))
    result, port = _explain(source, allowlist, candidate)
    assert result.status is SandboxExplanationStatus.ATTACHED
    request = port.inputs[0]

    assert set(type(request).model_fields) == {
        "result_digest",
        "decision",
        "issue_refs",
        "allowlist_entries",
    }
    assert set(SandboxExplanationIssueRefV1.model_fields) == {
        "issue_id",
        "rule_id",
        "severity",
    }
    assert set(SandboxExplanationAllowlistEntryV1.model_fields) == {"rule_id", "text"}
    assert request.result_digest == source.result_digest
    assert request.decision is source.decision
    assert len(request.issue_refs) == len(source.issues)
    assert request.allowlist_entries == allowlist.entries
    assert not hasattr(request, "source_result")

    dumped = request.model_dump(mode="python")
    top_keys = set(dumped)
    nested_keys = {
        key
        for collection_name in ("issue_refs", "allowlist_entries")
        for item in dumped[collection_name]
        for key in item
    }
    forbidden = {
        "subject",
        "formula",
        "profile",
        "manifest",
        "artifact_payload",
        "name",
        "contact",
        "identity",
        "prompt",
        "credential",
        "nonce",
        "signature",
        "context",
        "run_envelope_digest",
        "decision_subject_digest",
    }
    assert top_keys.isdisjoint(forbidden)
    assert nested_keys.isdisjoint(forbidden)
    serialized = canonical_explanation_bytes(request)
    assert b"fixed-fictitious-component" not in serialized
    assert b"bounded-test-value" not in serialized
    assert b"sandbox-test-key-not-a-secret" not in serialized


def test_l5_2_no_settings_env_data_model_network_gateway_legacy_review_record_export_import() -> None:
    module_path = Path("app/agent_runtime/sandbox_explanation.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")

    assert set(imported_modules) <= {
        "__future__",
        "collections.abc",
        "enum",
        "hashlib",
        "json",
        "typing",
        "pydantic",
        "app.agent_runtime.sandbox_safety",
    }
    forbidden_roots = {
        "aiohttp",
        "http",
        "httpx",
        "openai",
        "os",
        "pathlib",
        "pymilvus",
        "redis",
        "requests",
        "socket",
        "sqlite3",
        "subprocess",
        "urllib",
    }
    assert all(module.split(".", 1)[0] not in forbidden_roots for module in imported_modules)
    app_imports = tuple(module for module in imported_modules if module.startswith("app."))
    assert app_imports == ("app.agent_runtime.sandbox_safety",)

    forbidden_calls = {"open", "print", "breakpoint", "exec", "eval", "compile"}
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called_names.isdisjoint(forbidden_calls)


def test_l5_2_allowlist_is_exact_unique_sorted_strict_and_digest_bound() -> None:
    source = _source_result(issue_count=2)
    allowlist = _allowlist(source)
    candidate = _candidate(source, allowlist)
    valid_result, _ = _explain(source, allowlist, candidate)
    assert valid_result.status is SandboxExplanationStatus.ATTACHED

    valid = allowlist.model_dump(mode="python")
    missing = SandboxExplanationAllowlistBundleV1.build(
        entries=allowlist.entries[:-1]
    )
    extra_entry = SandboxExplanationAllowlistEntryV1(
        rule_id="sx.r.extra",
        text="fixed extra text",
    )
    extra = SandboxExplanationAllowlistBundleV1.build(
        entries=(*allowlist.entries, extra_entry)
    )
    duplicate = {**valid, "entries": (valid["entries"][0], valid["entries"][0])}
    reversed_entries = {**valid, "entries": tuple(reversed(valid["entries"]))}
    bad_digest = {**valid, "allowlist_digest": "0" * 64}
    unknown = {**valid, "unexpected": "forbidden"}

    for invalid in (missing, extra, duplicate, reversed_entries, bad_digest, unknown):
        port = _FakeExplanationPort(candidate)
        result = SandboxSafetyExplanationAgent(port).explain(source, invalid)
        _assert_unavailable(result, source.result_digest)
        assert port.calls == 0


def test_l5_2_invalid_l5_1_digest_and_schema_never_reach_port() -> None:
    source = _source_result(issue_count=1)
    allowlist = _allowlist(source)
    candidate = _candidate(source, allowlist)
    valid = source.model_dump(mode="python")
    invalid_sources = (
        {**valid, "result_digest": "0" * 64},
        {**valid, "decision": "not-a-decision"},
        {**valid, "unexpected": "forbidden"},
        {key: value for key, value in valid.items() if key != "issues"},
        None,
        "{bad-json",
    )

    for invalid in invalid_sources:
        port = _FakeExplanationPort(candidate)
        result = SandboxSafetyExplanationAgent(port).explain(invalid, allowlist)
        assert result.status is SandboxExplanationStatus.EXPLANATION_UNAVAILABLE
        assert result.statements == ()
        assert result.disclaimer == SANDBOX_EXPLANATION_DISCLAIMER
        assert port.calls == 0


def test_l5_2_candidate_and_result_repr_hide_explanation_text() -> None:
    source = _source_result(issue_count=1)
    unsafe_marker = "candidate-free-text-must-not-appear-in-repr"
    allowlist = _allowlist(source, texts=(unsafe_marker,))
    candidate = _candidate(source, allowlist)
    result, _ = _explain(source, allowlist, candidate)
    assert result.status is SandboxExplanationStatus.ATTACHED

    assert unsafe_marker not in repr(allowlist)
    assert unsafe_marker not in repr(candidate)
    assert unsafe_marker not in repr(result)
    assert result.statements[0].text == unsafe_marker


def test_l5_2_strict_dtos_reject_coercion_and_unknown_fields() -> None:
    source = _source_result(issue_count=1)
    allowlist = _allowlist(source)
    candidate = _candidate(source, allowlist)
    result, _ = _explain(source, allowlist, candidate)
    assert result.status is SandboxExplanationStatus.ATTACHED

    with pytest.raises(ValidationError):
        SandboxExplanationCandidateV1.model_validate(
            {"statements": (), "unexpected": "forbidden"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        SandboxExplanationCandidateStatementV1.model_validate(
            {
                "issue_id": 7,
                "rule_id": source.issues[0].rule_id,
                "text": allowlist.entries[0].text,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        SandboxExplanationResultV1.model_validate(
            {**result.model_dump(mode="python"), "decision": "allow"},
            strict=True,
        )


def test_l5_2_thousand_maximum_explanations_are_resource_bounded() -> None:
    source = _source_result(issue_count=MAX_EXPLANATION_ISSUES)
    base_texts = tuple(
        f"fixed allowlisted explanation statement {index:03d}"
        for index in range(MAX_EXPLANATION_ISSUES)
    )
    base_allowlist = _allowlist(source, texts=base_texts)
    base_candidate = _candidate(source, base_allowlist)
    base_result, _ = _explain(source, base_allowlist, base_candidate)
    assert base_result.status is SandboxExplanationStatus.ATTACHED

    target_bytes = MAX_EXPLANATION_BYTES - 16
    padding = target_bytes - len(canonical_explanation_bytes(base_result))
    assert padding > 0
    maximum_texts = (f"{base_texts[0]}{'x' * padding}", *base_texts[1:])
    maximum_allowlist = _allowlist(source, texts=maximum_texts)
    maximum_candidate = _candidate(source, maximum_allowlist)
    port = _FakeExplanationPort(maximum_candidate, capture_inputs=False)
    agent = SandboxSafetyExplanationAgent(port)
    expected = agent.explain(source, maximum_allowlist)
    expected_bytes = canonical_explanation_bytes(expected)

    assert len(source.issues) == MAX_EXPLANATION_ISSUES
    assert len(expected.statements) == MAX_EXPLANATION_ISSUES
    assert expected.status is SandboxExplanationStatus.ATTACHED
    assert len(expected_bytes) == target_bytes
    assert len(canonical_explanation_bytes(maximum_candidate)) <= MAX_EXPLANATION_BYTES

    for _ in range(20):
        actual = agent.explain(source, maximum_allowlist)
        assert canonical_explanation_bytes(actual) == expected_bytes

    gc.collect()
    rss_before = _rss_bytes()
    timings_ms: list[float] = []
    for _ in range(1_000):
        started = time.perf_counter_ns()
        actual = agent.explain(source, maximum_allowlist)
        timings_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        assert canonical_explanation_bytes(actual) == expected_bytes
    gc.collect()
    rss_growth = max(0, _rss_bytes() - rss_before)
    ordered = sorted(timings_ms)
    p95_ms = ordered[949]
    p99_ms = ordered[989]
    processor = platform.processor().replace(" ", "_") or platform.machine()

    sys.stdout.write(
        "L5_2_RESOURCE "
        f"python={platform.python_version()} cpu={processor} "
        f"issues={len(source.issues)} statements={len(expected.statements)} "
        f"candidate_bytes={len(canonical_explanation_bytes(maximum_candidate))} "
        f"result_bytes={len(expected_bytes)} samples=1000 warmup=20 "
        "method=perf_counter_ns+process_working_set_rss "
        f"p95_ms={p95_ms:.3f} p99_ms={p99_ms:.3f} "
        f"rss_growth_bytes={rss_growth}\n"
    )
    assert p95_ms < 50
    assert p99_ms < 100
    assert rss_growth < 64 * 1024 * 1024
