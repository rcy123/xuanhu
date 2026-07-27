"""L6-1 record DTO and authority-bound assembler regressions."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_runtime.sandbox_record import (
    RECORD_SCHEMA_VERSION,
    SANDBOX_RECORD_DISCLAIMER,
    SandboxMedicalRecordData,
    SandboxRecordAssembler,
    SandboxRecordError,
    canonical_review_bytes,
)
from app.agent_runtime.sandbox_review import (
    SandboxResumeCommandV1,
    SandboxReviewAction,
)
from app.agent_runtime.sandbox_safety import (
    SandboxFormulaItemV1,
    SandboxSafetyResultV1,
)
from tests.test_l5_4_sandbox_modify_full_recheck import (
    _NAMESPACE,
    _NEW_CHECKPOINT,
    _SESSION,
    _THREAD,
    _coordinator,
    _revision_command,
    _subject_and_bundle,
    _submission,
)


def _confirm_review_required_state(
    action: SandboxReviewAction = SandboxReviewAction.CONFIRM,
):
    coordinator, *_ = _coordinator()
    outcome = coordinator.apply_revision(_revision_command(coordinator))
    assert outcome.delivery is not None
    staged = coordinator.stage_current_review(_submission(outcome.delivery, action))
    assert staged.resume_attempt_ref is not None
    resumed = coordinator.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    assert resumed.status == "applied"
    return coordinator


def _confirm_distinct_review_required_state():
    coordinator, *_ = _coordinator()
    candidate, bundle = _subject_and_bundle(
        domain_state_version=8,
        formula_revision=4,
        amount_milliunits=23,
        dataset_version="distinct-record.2",
    )
    outcome = coordinator.apply_revision(
        _revision_command(
            coordinator,
            candidate=candidate,
            bundle=bundle,
        )
    )
    assert outcome.delivery is not None
    staged = coordinator.stage_current_review(
        _submission(outcome.delivery, SandboxReviewAction.CONFIRM)
    )
    assert staged.resume_attempt_ref is not None
    resumed = coordinator.resume_current_review(
        SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
    )
    assert resumed.status == "applied"
    return coordinator


def _assemble(coordinator=None) -> SandboxMedicalRecordData:
    authority = (
        _confirm_review_required_state()
        if coordinator is None
        else coordinator
    )
    return SandboxRecordAssembler().assemble(
        authority,
        namespace=_NAMESPACE,
        session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    )


def test_l6_1_confirmed_coordinator_produces_typed_v2_record() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    snapshot = coordinator.snapshot()
    confirm_event = tuple(
        event
        for event in snapshot.review_snapshot.events
        if event.action is SandboxReviewAction.CONFIRM
    )[-1]

    assert record.session_id == _SESSION
    assert record.assembled_at == confirm_event.applied_at
    assert record.record_version == RECORD_SCHEMA_VERSION
    assert record.disclaimer == SANDBOX_RECORD_DISCLAIMER
    assert record.record_id.startswith("sandbox-record-")
    assert all(
        type(item) is SandboxFormulaItemV1
        for item in record.reviewed_formula
    )
    assert type(record.safety_result) is SandboxSafetyResultV1


def test_l6_1_same_confirmed_authority_is_byte_stable_across_retries() -> None:
    coordinator = _confirm_review_required_state()

    first = _assemble(coordinator)
    second = _assemble(coordinator)

    assert first == second
    assert first.record_id == second.record_id
    assert canonical_review_bytes(first) == canonical_review_bytes(second)


def test_l6_1_nested_values_are_deeply_immutable() -> None:
    record = _assemble()

    with pytest.raises(ValidationError):
        record.reviewed_formula[0].component = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.safety_result.decision = "block"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.assembled_at = 0  # type: ignore[misc]


@pytest.mark.parametrize("kind", ("instance", "dict", "bytes", "str"))
def test_l6_1_raw_snapshot_inputs_are_not_authority(kind: str) -> None:
    coordinator = _confirm_review_required_state()
    snapshot = coordinator.snapshot()
    raw: object
    if kind == "instance":
        raw = snapshot
    elif kind == "dict":
        raw = snapshot.model_dump(mode="python")
    elif kind == "bytes":
        raw = canonical_review_bytes(snapshot)
    else:
        raw = canonical_review_bytes(snapshot).decode("utf-8")

    with pytest.raises(SandboxRecordError):
        SandboxRecordAssembler().assemble(
            raw,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )


def test_l6_1_reject_review_cannot_produce_record() -> None:
    coordinator = _confirm_review_required_state(SandboxReviewAction.REJECT)

    with pytest.raises(SandboxRecordError):
        _assemble(coordinator)


def test_l6_1_scope_mismatch_fails_fixed_closed() -> None:
    coordinator = _confirm_review_required_state()

    with pytest.raises(SandboxRecordError) as raised:
        SandboxRecordAssembler().assemble(
            coordinator,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id="wrong-checkpoint",
        )

    assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l6_1_record_has_no_free_text_diagnosis_or_symptom_fields() -> None:
    assert set(SandboxMedicalRecordData.model_fields) == {
        "record_id",
        "session_id",
        "revision_id",
        "reviewed_formula",
        "safety_result",
        "review_confirm_ref",
        "assembled_at",
        "record_version",
        "disclaimer",
    }


def test_l6_1_module_keeps_offline_import_boundary() -> None:
    source = Path("app/agent_runtime/sandbox_record.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".", maxsplit=1)[0]
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", maxsplit=1)[0])

    assert imported_roots <= {
        "__future__",
        "app",
        "hashlib",
        "json",
        "pydantic",
        "threading",
        "typing",
        "warnings",
    }
    assert not any(
        token in source
        for token in (
            "httpx",
            "requests",
            "sqlalchemy",
            "subprocess",
            "socket",
            "open(",
            ".env",
        )
    )
