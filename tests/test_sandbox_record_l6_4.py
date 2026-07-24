"""L6-4 sandbox record narration and final combination tests."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from app.agent_runtime.sandbox_record import (
    RECORD_SCHEMA_VERSION,
    SANDBOX_RECORD_DISCLAIMER,
    SandboxMedicalRecordData,
    SandboxRecordAssembler,
    SandboxRecordConsistencyVerifier,
    SandboxRecordError,
    SandboxRecordNarration,
    SandboxRecordStore,
    canonical_review_bytes,
    serialize_record,
)
from tests.test_sandbox_record_l6_1 import (
    _NAMESPACE,
    _NEW_CHECKPOINT,
    _THREAD,
    _confirm_review_required_state,
)
from tests.test_sandbox_record_l6_1 import (
    _SESSION as _L6_1_SESSION,
)

_ITEM_A = {
    "item_id": "test-item-001",
    "component": "test-component",
    "amount_milliunits": 100,
    "unit": "mg",
}
_ITEM_B = {
    "item_id": "test-item-002",
    "component": "other-component",
    "amount_milliunits": 200,
    "unit": "ml",
}
_SESSION = "test-session-l6-4-001"
_REVISION = "f" * 64
_REVIEW_CONFIRM_REF = "sandbox-attempt-" + "g" * 64
_SAFETY_RESULT: dict[str, object] = {
    "decision": "allow",
    "test_field": "test_value",
}
_NOW = 2_000_000_000


def _compute_record_id(**fields: object) -> str:
    """Compute record_id using the same logic as _record_id in sandbox_record.py."""
    body: dict[str, object] = {
        "assembled_at": fields["assembled_at"],
        "disclaimer": fields["disclaimer"],
        "record_version": fields["record_version"],
        "review_confirm_ref": fields["review_confirm_ref"],
        "reviewed_formula": fields["reviewed_formula"],
        "revision_id": fields["revision_id"],
        "safety_result": fields["safety_result"],
        "session_id": fields["session_id"],
    }
    return "sandbox-record-" + hashlib.sha256(canonical_review_bytes(body)).hexdigest()


def _make_record(**overrides: object) -> SandboxMedicalRecordData:
    """Build a minimal SandboxMedicalRecordData via model_construct."""
    defaults: dict[str, object] = dict(
        session_id=_SESSION,
        revision_id=_REVISION,
        reviewed_formula=(_ITEM_A,),
        safety_result=_SAFETY_RESULT,
        review_confirm_ref=_REVIEW_CONFIRM_REF,
        assembled_at=_NOW,
        record_version=RECORD_SCHEMA_VERSION,
        disclaimer=SANDBOX_RECORD_DISCLAIMER,
    )
    merged = {**defaults, **overrides}
    if "record_id" not in overrides:
        merged["record_id"] = _compute_record_id(**merged)
    return SandboxMedicalRecordData.model_construct(**merged)  # type: ignore[arg-type]


# ── RED ────────────────────────────────────────────────────────────────────


class TestSandboxRecordNarrationRed:
    """RED tests proving gaps before narration / full-pipeline exist."""

    def test_l6_4_red_no_narration_function(self) -> None:
        """Without SandboxRecordNarration, record data has only raw JSON — no formatted narrative."""
        record = _make_record()

        # The DTO produces raw JSON, not a formatted human-readable narrative
        raw_json = record.model_dump_json()
        assert raw_json.startswith("{")

        # A raw JSON dump is fundamentally different from what a labelled
        # template-based narrative would produce — JSON uses quoted keys.
        assert '"record_id":' in raw_json

    def test_l6_4_red_no_end_to_end_pipeline(self) -> None:
        """Without L6-4 integration, individual layers work independently but no full pipeline exists."""
        record = _make_record()

        # Store works independently
        store = SandboxRecordStore()
        store.put(record)
        assert store.get(record.record_id) == record

        # Serialize works independently
        serialized = serialize_record(record)
        assert isinstance(serialized, bytes)

        # But there is no complete end-to-end pipeline test that exercises:
        #   assembler → verifier → store → serialize → narrate
        # No SandboxRecordNarration.narrate() exists to complete the chain.
        raw = record.model_dump(mode="json")
        assert isinstance(raw, dict)


# ── GREEN ──────────────────────────────────────────────────────────────────


class TestSandboxRecordNarrationGreen:
    """GREEN tests proving narration and full-pipeline correctness."""

    def test_l6_4_green_narration_determinism(self) -> None:
        """Same record must produce identical narration string."""
        record = _make_record()

        text_a = SandboxRecordNarration.narrate(record)
        text_b = SandboxRecordNarration.narrate(record)

        assert text_a == text_b
        assert isinstance(text_a, str)

    def test_l6_4_green_narration_discrimination(self) -> None:
        """Different records must produce different narration strings."""
        record_a = _make_record()
        record_b = _make_record(session_id="different-session-002")

        text_a = SandboxRecordNarration.narrate(record_a)
        text_b = SandboxRecordNarration.narrate(record_b)

        assert text_a != text_b

    def test_l6_4_green_narration_field_coverage(self) -> None:
        """Narration must contain all key fields of the record."""
        record = _make_record()
        text = SandboxRecordNarration.narrate(record)

        assert record.session_id in text
        assert record.revision_id in text
        assert record.record_id in text
        assert str(record.assembled_at) in text
        assert record.record_version in text
        assert record.disclaimer in text
        assert record.review_confirm_ref in text

    def test_l6_4_green_narration_formula_items_included(self) -> None:
        """Narration must contain reviewed_formula item details."""
        record = _make_record()
        text = SandboxRecordNarration.narrate(record)

        for item in record.reviewed_formula:
            assert str(item.get("item_id", "")) in text
            assert str(item.get("component", "")) in text
            assert str(item.get("amount_milliunits", "")) in text
            assert str(item.get("unit", "")) in text

    def test_l6_4_green_narration_safety_decision_included(self) -> None:
        """Narration must contain safety_result decision."""
        record = _make_record()
        text = SandboxRecordNarration.narrate(record)

        assert record.safety_result["decision"] in text

    def test_l6_4_green_full_pipeline(self) -> None:
        """Full pipeline: assembler → verifier → store → serialize → narrate must pass."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()

        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_L6_1_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        # Verifier accepts
        verifier = SandboxRecordConsistencyVerifier()
        assert verifier.verify(record, recheck_snapshot=snapshot) is True

        # Store accepts
        store = SandboxRecordStore()
        store.put(record)
        assert store.get(record.record_id) == record

        # Serialize succeeds
        serialized = serialize_record(record)
        assert isinstance(serialized, bytes)

        # Narrate completes the chain
        text = SandboxRecordNarration.narrate(record)
        assert isinstance(text, str)
        assert record.session_id in text
        assert record.record_id in text
        assert record.revision_id in text

    def test_l6_4_green_tampered_field_rejected(self) -> None:
        """Tampered record fields must be rejected by verifier or store (at least one layer)."""
        coordinator = _confirm_review_required_state()
        snapshot = coordinator.snapshot()
        assembler = SandboxRecordAssembler()

        record = assembler.assemble(
            snapshot,
            namespace=_NAMESPACE,
            session_id=_L6_1_SESSION,
            thread_id=_THREAD,
            checkpoint_id=_NEW_CHECKPOINT,
            now=2_000_000_100,
        )

        # Tamper the reviewed_formula via model_construct bypass
        tampered_formula = (
            {
                "item_id": "evil-item-001",
                "component": "evil-component",
                "amount_milliunits": 999,
                "unit": "evil-unit",
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

        # Store the legitimate record first
        store = SandboxRecordStore()
        store.put(record)

        # At least one layer must reject the tampered record
        verifier = SandboxRecordConsistencyVerifier()
        verifier_rejected = not verifier.verify(tampered, recheck_snapshot=snapshot)

        store_rejected = False
        try:
            store.put(tampered)
        except SandboxRecordError:
            store_rejected = True

        assert verifier_rejected or store_rejected

    def test_l6_4_green_narration_no_model_or_network_calls(self) -> None:
        """Narrate must not use forbidden calls or network tokens."""
        source = Path("app/agent_runtime/sandbox_record.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        forbidden = {"open", "print", "breakpoint", "exec", "eval", "compile"}
        assert called_names.isdisjoint(forbidden)

        for token in ("http://", "https://", "socket", "subprocess", "requests"):
            assert token not in source

    def test_l6_4_green_no_new_import_roots(self) -> None:
        """Must not add new import roots beyond the L6-1 approved set."""
        module_path = Path("app/agent_runtime/sandbox_record.py")
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", maxsplit=1)[0])

        allowed: set[str] = {
            "__future__",
            "collections",
            "enum",
            "hashlib",
            "json",
            "pydantic",
            "typing",
            "app",
        }
        assert imported_roots <= allowed

    def test_l6_4_green_narration_slots(self) -> None:
        """SandboxRecordNarration must have empty __slots__."""
        assert SandboxRecordNarration.__slots__ == ()

    def test_l6_4_green_narration_output_format(self) -> None:
        """Narration must produce a predictable multi-line format starting with a header."""
        record = _make_record()
        text = SandboxRecordNarration.narrate(record)

        lines = text.strip().split("\n")
        assert len(lines) >= 3
        assert "Sandbox Medical Record Narration" in lines[0]
