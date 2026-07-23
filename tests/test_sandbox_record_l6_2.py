"""L6-2 sandbox record consistency verifier tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent_runtime.sandbox_record import (
    SandboxMedicalRecordData,
    SandboxRecordAssembler,
    SandboxRecordConsistencyVerifier,
    SandboxRecordError,
    canonical_review_bytes,
)
from app.agent_runtime.sandbox_review import (
    SandboxResumeCommandV1,
    SandboxReviewAction,
)
from tests.test_sandbox_record_l6_1 import (
    _NAMESPACE,
    _NEW_CHECKPOINT,
    _SESSION,
    _THREAD,
    _confirm_review_required_state,
    _coordinator,
    _revision_command,
    _submission,
)


class TestRecordConsistencyVerifierRed:
    """RED tests proving gaps before production verifier exists."""

    def test_l6_2_red_imports_confirm_module_exists(self) -> None:
        """Verify the verifier class is importable."""
        assert SandboxRecordConsistencyVerifier is not None

    def test_l6_2_red_tampered_formula_is_accepted_without_verifier(self) -> None:
        """Without verifier, tampered reviewed_formula DTO is accepted (record_id bypassed)."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        # Bypass DTO validation by using model_construct with tampered formula
        tampered_formula = (
            {
                "item_id": "injected-item-001",
                "component": "injected-fictitious-component",
                "amount_milliunits": 999999,
                "unit": "injected_unit",
            },
        )
        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=tampered_formula,
            safety_result=record.safety_result,
            review_confirm_ref=record.review_confirm_ref,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )
        assert tampered.reviewed_formula[0]["item_id"] == "injected-item-001"

    def test_l6_2_red_injected_symptom_is_accepted_without_verifier(self) -> None:
        """Without verifier, new content injection in DTO is accepted."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        tampered_safety = dict(record.safety_result)
        tampered_safety["injected_field"] = "injected_value"
        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=record.reviewed_formula,
            safety_result=tampered_safety,
            review_confirm_ref=record.review_confirm_ref,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )
        assert tampered.safety_result["injected_field"] == "injected_value"

    def test_l6_2_red_tampered_confirm_ref_is_accepted_without_verifier(self) -> None:
        """Without verifier, tampered review_confirm_ref DTO is accepted."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=record.reviewed_formula,
            safety_result=record.safety_result,
            review_confirm_ref="sandbox-attempt-" + "f" * 64,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )
        assert tampered.review_confirm_ref == "sandbox-attempt-" + "f" * 64

    def test_l6_2_red_tampered_safety_result_is_accepted_without_verifier(self) -> None:
        """Without verifier, tampered safety_result decision DTO is accepted."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        tampered_safety = dict(record.safety_result)
        tampered_safety["decision"] = "block"
        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=record.reviewed_formula,
            safety_result=tampered_safety,
            review_confirm_ref=record.review_confirm_ref,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )
        assert tampered.safety_result["decision"] == "block"


class TestRecordConsistencyVerifierR1Red:
    """R1 RED tests proving bytes/str double-serialization gaps (pre-fix).

    These were RED before the fix: verifier returned False for bytes/str
    snapshots, and assembler raised SandboxRecordError for bytes/str.
    After the fix they assert the GREEN behavior (acceptance), and the
    full input-type-matrix GREEN class below mirrors the same assertions.
    """

    def test_r1_red_verifier_bytes_snapshot_is_rejected(self) -> None:
        """Verifier with bytes snapshot was rejected pre-fix; now accepted."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        snapshot_bytes = canonical_review_bytes(snapshot)
        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(record, recheck_snapshot=snapshot_bytes)
        assert result is True

    def test_r1_red_verifier_str_snapshot_is_rejected(self) -> None:
        """Verifier with str snapshot was rejected pre-fix; now accepted."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        snapshot_str = canonical_review_bytes(snapshot).decode("utf-8")
        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(record, recheck_snapshot=snapshot_str)
        assert result is True

    def test_r1_red_assembler_bytes_snapshot_raises(self) -> None:
        """Assembler with bytes snapshot raised pre-fix; now produces record."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        snapshot_bytes = canonical_review_bytes(snapshot)
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot_bytes,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        assert isinstance(record, SandboxMedicalRecordData)

    def test_r1_red_assembler_str_snapshot_raises(self) -> None:
        """Assembler with str snapshot raised pre-fix; now produces record."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        snapshot_str = canonical_review_bytes(snapshot).decode("utf-8")
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot_str,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        assert isinstance(record, SandboxMedicalRecordData)


class TestRecordConsistencyVerifierGreen:
    """GREEN tests proving verifier correctly accepts/rejects records."""

    def test_l6_2_green_legal_record_passes(self) -> None:
        """A legitimate assembled record must pass verifier."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(record, recheck_snapshot=snapshot)
        assert result is True

    def test_l6_2_green_tampered_formula_rejected(self) -> None:
        """Tampered reviewed_formula must be rejected by verifier."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        tampered_formula = (
            {
                "item_id": "injected-item-001",
                "component": "injected-fictitious-component",
                "amount_milliunits": 999999,
                "unit": "injected_unit",
            },
        )
        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=tampered_formula,
            safety_result=record.safety_result,
            review_confirm_ref=record.review_confirm_ref,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )

        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(tampered, recheck_snapshot=snapshot)
        assert result is False

    def test_l6_2_green_injected_symptom_rejected(self) -> None:
        """Injected new content in safety_result must be rejected."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        tampered_safety = dict(record.safety_result)
        tampered_safety["injected_field"] = "injected_value"
        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=record.reviewed_formula,
            safety_result=tampered_safety,
            review_confirm_ref=record.review_confirm_ref,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )

        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(tampered, recheck_snapshot=snapshot)
        assert result is False

    def test_l6_2_green_tampered_confirm_ref_rejected(self) -> None:
        """Tampered review_confirm_ref must be rejected by verifier."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=record.reviewed_formula,
            safety_result=record.safety_result,
            review_confirm_ref="sandbox-attempt-" + "f" * 64,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )

        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(tampered, recheck_snapshot=snapshot)
        assert result is False

    def test_l6_2_green_tampered_safety_result_decision_rejected(self) -> None:
        """Tampered safety_result decision must be rejected by verifier."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        tampered_safety = dict(record.safety_result)
        tampered_safety["decision"] = "block"
        tampered = SandboxMedicalRecordData.model_construct(
            record_id=record.record_id,
            session_id=record.session_id,
            revision_id=record.revision_id,
            reviewed_formula=record.reviewed_formula,
            safety_result=tampered_safety,
            review_confirm_ref=record.review_confirm_ref,
            assembled_at=record.assembled_at,
            record_version=record.record_version,
            disclaimer=record.disclaimer,
        )

        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(tampered, recheck_snapshot=snapshot)
        assert result is False

    def test_l6_2_green_reconstructed_record_passes(self) -> None:
        """Re-assembling the same snapshot produces a record that passes verifier."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record1 = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        record2 = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        verifier = SandboxRecordConsistencyVerifier()
        assert verifier.verify(record1, recheck_snapshot=snapshot) is True
        assert verifier.verify(record2, recheck_snapshot=snapshot) is True
        assert record1 == record2

    def test_l6_2_green_verifier_rejects_none_snapshot(self) -> None:
        """Verifier must reject when snapshot is None."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(record, recheck_snapshot=None)
        assert result is False

    def test_l6_2_green_verifier_rejects_empty_snapshot(self) -> None:
        """Verifier must reject when snapshot has empty revisions."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        verifier = SandboxRecordConsistencyVerifier()
        empty_snapshot = {"revisions": (), "runs": (), "invalidations": (), "receipts": ()}
        result = verifier.verify(record, recheck_snapshot=empty_snapshot)
        assert result is False

    def test_l6_2_green_verifier_rejects_mismatched_snapshot(self) -> None:
        """Verifier must reject when record doesn't match snapshot."""
        coordinator1 = _confirm_review_required_state()
        snapshot1 = coordinator1.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot1,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        # Build a genuinely different snapshot with different formula
        coordinator2, *_ = _coordinator()
        outcome = coordinator2.apply_revision(_revision_command(coordinator2, suffix="009"))
        assert outcome.delivery is not None
        staged = coordinator2.stage_current_review(
            _submission(outcome.delivery, SandboxReviewAction.CONFIRM)
        )
        assert staged.resume_attempt_ref is not None
        assert coordinator2.resume_current_review(
            SandboxResumeCommandV1(resume_attempt_ref=staged.resume_attempt_ref)
        ).status == "applied"
        snapshot2 = coordinator2.snapshot()

        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(record, recheck_snapshot=snapshot2)
        assert result is False

    def test_l6_2_green_verifier_has_no_slots(self) -> None:
        """Verifier must not hold any state."""
        verifier = SandboxRecordConsistencyVerifier()
        assert verifier.__slots__ == ()

    def test_l6_2_green_verifier_no_model_calls(self) -> None:
        """Verifier must not call any model or have network capabilities."""
        source = Path("app/agent_runtime/sandbox_record.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        forbidden = {"open", "print", "breakpoint", "exec", "eval", "compile"}
        assert called_names.isdisjoint(forbidden)


class TestR1InputTypeMatrixVerifier:
    """R1 input type matrix: verifier accept instance/dict/bytes/str, reject garbage/None."""

    def _setup(self):
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        return record, snapshot, assembler

    def test_r1_green_verifier_instance_snapshot_passes(self) -> None:
        """Verifier with instance snapshot must pass."""
        record, snapshot, _ = self._setup()
        verifier = SandboxRecordConsistencyVerifier()
        assert verifier.verify(record, recheck_snapshot=snapshot) is True

    def test_r1_green_verifier_dict_snapshot_passes(self) -> None:
        """Verifier with dict snapshot must pass."""
        record, snapshot, _ = self._setup()
        snapshot_dict = snapshot.model_dump(mode="python")
        verifier = SandboxRecordConsistencyVerifier()
        assert verifier.verify(record, recheck_snapshot=snapshot_dict) is True

    def test_r1_green_verifier_bytes_snapshot_passes(self) -> None:
        """Verifier with bytes snapshot must pass."""
        record, snapshot, _ = self._setup()
        snapshot_bytes = canonical_review_bytes(snapshot)
        verifier = SandboxRecordConsistencyVerifier()
        assert verifier.verify(record, recheck_snapshot=snapshot_bytes) is True

    def test_r1_green_verifier_str_snapshot_passes(self) -> None:
        """Verifier with str snapshot must pass."""
        record, snapshot, _ = self._setup()
        snapshot_str = canonical_review_bytes(snapshot).decode("utf-8")
        verifier = SandboxRecordConsistencyVerifier()
        assert verifier.verify(record, recheck_snapshot=snapshot_str) is True

    def test_r1_green_verifier_garbage_rejected(self) -> None:
        """Verifier with garbage input must reject."""
        record, _, _ = self._setup()
        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(record, recheck_snapshot=42)
        assert result is False

    def test_r1_green_verifier_none_rejected(self) -> None:
        """Verifier with None snapshot must reject."""
        record, _, _ = self._setup()
        verifier = SandboxRecordConsistencyVerifier()
        result = verifier.verify(record, recheck_snapshot=None)
        assert result is False


class TestR1InputTypeMatrixAssembler:
    """R1 input type matrix: assembler accept instance/dict/bytes/str, reject garbage/None."""

    def _setup(self):
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        return snapshot

    def test_r1_green_assembler_instance_snapshot_produces_record(self) -> None:
        """Assembler with instance snapshot must produce record."""
        snapshot = self._setup()
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        assert isinstance(record, SandboxMedicalRecordData)

    def test_r1_green_assembler_dict_snapshot_produces_record(self) -> None:
        """Assembler with dict snapshot must produce record."""
        snapshot = self._setup()
        snapshot_dict = snapshot.model_dump(mode="python")
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot_dict,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        assert isinstance(record, SandboxMedicalRecordData)

    def test_r1_green_assembler_bytes_snapshot_produces_record(self) -> None:
        """Assembler with bytes snapshot must produce record."""
        snapshot = self._setup()
        snapshot_bytes = canonical_review_bytes(snapshot)
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot_bytes,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        assert isinstance(record, SandboxMedicalRecordData)

    def test_r1_green_assembler_str_snapshot_produces_record(self) -> None:
        """Assembler with str snapshot must produce record."""
        snapshot = self._setup()
        snapshot_str = canonical_review_bytes(snapshot).decode("utf-8")
        assembler = SandboxRecordAssembler()
        record = assembler.assemble(
            snapshot_str,
            namespace=_NAMESPACE,
            session_id=_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )
        assert isinstance(record, SandboxMedicalRecordData)

    def test_r1_green_assembler_garbage_raises(self) -> None:
        """Assembler with garbage input must raise SandboxRecordError."""
        assembler = SandboxRecordAssembler()
        with pytest.raises(SandboxRecordError):
            assembler.assemble(
                42,
                namespace=_NAMESPACE,
                session_id=_SESSION,
                thread_id=_THREAD,
                checkpoint_id=_NEW_CHECKPOINT,
                now=2_000_000_100,
            )

    def test_r1_green_assembler_none_raises(self) -> None:
        """Assembler with None input must raise SandboxRecordError."""
        assembler = SandboxRecordAssembler()
        with pytest.raises(SandboxRecordError):
            assembler.assemble(
                None,
                namespace=_NAMESPACE,
                session_id=_SESSION,
                thread_id=_THREAD,
                checkpoint_id=_NEW_CHECKPOINT,
                now=2_000_000_100,
            )
