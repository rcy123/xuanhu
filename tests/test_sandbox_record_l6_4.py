"""L6-4 allowlist narration and real reference-pipeline regressions."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent_runtime.sandbox_recheck import (
    SandboxAuthorizedRecordProjectionV1,
    SandboxRecheckCoordinator,
)
from app.agent_runtime.sandbox_record import (
    SandboxRecordConsistencyVerifier,
    SandboxRecordError,
    SandboxRecordNarration,
    SandboxRecordPipeline,
    SandboxRecordPipelineResult,
    SandboxRecordStore,
    deserialize_record,
)
from app.agent_runtime.sandbox_safety import SandboxFormulaItemV1
from tests.test_l5_4_sandbox_modify_full_recheck import (
    _NAMESPACE,
    _NEW_CHECKPOINT,
    _SESSION,
    _THREAD,
)
from tests.test_sandbox_record_l6_1 import (
    _assemble,
    _confirm_review_required_state,
)
from tests.test_sandbox_record_l6_2 import _model_construct_record


def test_l6_4_narration_is_deterministic_and_exactly_verifiable() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)

    first = SandboxRecordNarration.narrate(record)
    second = SandboxRecordNarration.narrate(record)

    assert first == second
    assert SandboxRecordConsistencyVerifier().verify(
        record,
        recheck_coordinator=coordinator,
        narration=first,
    )


def test_l6_4_distinct_record_inputs_have_distinct_narrations() -> None:
    record = _assemble()
    changed = _model_construct_record(
        record,
        assembled_at=record.assembled_at + 1,
    )

    assert SandboxRecordNarration.narrate(record) != (
        SandboxRecordNarration.narrate(changed)
    )


def test_l6_4_narration_covers_the_published_record_fields() -> None:
    record = _assemble()
    text = SandboxRecordNarration.narrate(record)

    assert record.record_id in text
    assert record.session_id in text
    assert record.revision_id in text
    assert record.disclaimer in text
    assert record.review_confirm_ref in text
    assert record.safety_result.decision.value in text
    assert str(record.assembled_at) in text
    assert record.record_version in text
    assert record.safety_result.result_digest not in text
    assert all(item.item_id in text for item in record.reviewed_formula)
    assert all(item.component in text for item in record.reviewed_formula)
    assert all(
        str(item.amount_milliunits) in text
        for item in record.reviewed_formula
    )
    assert all(item.unit in text for item in record.reviewed_formula)


def test_l6_4_free_text_control_sequences_cannot_forge_labels() -> None:
    record = _assemble()
    injected_item = SandboxFormulaItemV1(
        **{
            **record.reviewed_formula[0].model_dump(mode="python"),
            "component": (
                "synthetic\nDiagnosis: forged\r\n"
                "\x1b[31mSafety Result: forged\u202e"
            ),
        }
    )
    injected_record = _model_construct_record(
        record,
        reviewed_formula=(
            injected_item,
            *record.reviewed_formula[1:],
        ),
    )

    text = SandboxRecordNarration.narrate(injected_record)

    assert "\nDiagnosis: forged" not in text
    assert "\nSafety Result: forged" not in text
    assert "\x1b" not in text
    assert "\u202e" not in text
    assert "\\nDiagnosis: forged" in text
    assert "\\u001b" in text
    assert "\\u202e" in text


def test_l6_4_hidden_or_stale_record_fails_with_fixed_error() -> None:
    hidden = _assemble().model_copy(update={"diagnosis": "hidden"})

    with pytest.raises(SandboxRecordError) as raised:
        SandboxRecordNarration.narrate(hidden)

    assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l6_4_pipeline_consumes_each_previous_layer_output() -> None:
    coordinator = _confirm_review_required_state()
    pipeline = SandboxRecordPipeline(recheck_coordinator=coordinator)

    result = pipeline.run(
        namespace=_NAMESPACE,
        session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    )

    assert type(result) is SandboxRecordPipelineResult
    assert deserialize_record(result.serialized_record) == result.record
    assert SandboxRecordNarration.narrate(result.record) == result.narration


def test_l6_4_pipeline_failure_short_circuits_before_store() -> None:
    coordinator = _confirm_review_required_state()
    store = SandboxRecordStore()
    pipeline = SandboxRecordPipeline(
        recheck_coordinator=coordinator,
        store=store,
    )

    with pytest.raises(SandboxRecordError):
        pipeline.run(
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id="wrong-checkpoint",
        )

    assert store._records == {}


def test_l6_4_pipeline_rejects_raw_snapshot_at_composition_boundary() -> None:
    coordinator = _confirm_review_required_state()

    with pytest.raises(SandboxRecordError):
        SandboxRecordPipeline(
            recheck_coordinator=coordinator.snapshot(),
        )


def test_l6_4_pipeline_rejects_store_substitution() -> None:
    coordinator = _confirm_review_required_state()

    class _BypassStore(SandboxRecordStore):
        def put(self, record: object, *, recheck_coordinator: object) -> None:
            del record, recheck_coordinator

    with pytest.raises(SandboxRecordError):
        SandboxRecordPipeline(
            recheck_coordinator=coordinator,
            store=_BypassStore(),
        )


def test_l6_4_pipeline_rejects_post_construction_store_substitution() -> None:
    coordinator = _confirm_review_required_state()
    pipeline = SandboxRecordPipeline(recheck_coordinator=coordinator)

    class _BypassStore(SandboxRecordStore):
        pass

    with pytest.raises(AttributeError):
        pipeline._store = _BypassStore()

    object.__setattr__(pipeline, "_store", _BypassStore())
    with pytest.raises(SandboxRecordError):
        pipeline.run(
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
        )


def test_l6_4_pipeline_uses_one_active_authority_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _confirm_review_required_state()
    original_projection = SandboxRecheckCoordinator.authorized_record_projection
    calls = 0

    def counted_projection(
        value: SandboxRecheckCoordinator,
    ) -> SandboxAuthorizedRecordProjectionV1:
        nonlocal calls
        calls += 1
        return original_projection(value)

    monkeypatch.setattr(
        SandboxRecheckCoordinator,
        "authorized_record_projection",
        counted_projection,
    )
    pipeline = SandboxRecordPipeline(recheck_coordinator=coordinator)
    calls = 0

    result = pipeline.run(
        namespace=_NAMESPACE,
        session_id=_SESSION,
        thread_id=_THREAD,
        checkpoint_id=_NEW_CHECKPOINT,
    )

    assert result.record.session_id == _SESSION
    assert calls == 1


def test_l6_4_changed_narration_is_not_consistent() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    narration = SandboxRecordNarration.narrate(record)

    assert not SandboxRecordConsistencyVerifier().verify(
        record,
        recheck_coordinator=coordinator,
        narration=narration + "New recommendation: forged",
    )


def test_l6_4_narration_and_pipeline_are_slot_only() -> None:
    assert SandboxRecordNarration.__slots__ == ()
    assert SandboxRecordPipeline.__slots__ == (
        "_assembler",
        "_recheck_coordinator",
        "_store",
        "_verifier",
    )


def test_l6_4_has_no_llm_gateway_or_network_boundary() -> None:
    source = Path("app/agent_runtime/sandbox_record.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not any(
        module.startswith(
            (
                "app.agent_runtime.runtime",
                "app.core",
                "app.db",
                "app.services",
            )
        )
        for module in imported_modules
    )
    assert not any(
        token in source
        for token in ("httpx", "requests", "socket", "subprocess")
    )
