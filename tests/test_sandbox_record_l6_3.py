"""L6-3 sandbox medical record store and deterministic serialize tests."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from app.agent_runtime.sandbox_record import (
    RECORD_SCHEMA_VERSION,
    SANDBOX_RECORD_DISCLAIMER,
    SandboxMedicalRecordData,
    SandboxRecordError,
    SandboxRecordStore,
    canonical_review_bytes,
    serialize_record,
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
_SESSION = "test-session-l6-3-001"
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


class TestSandboxRecordStoreRed:
    """RED tests proving gaps before store / serialize exist."""

    def test_l6_3_red_no_store_tampered_id_accepted(self) -> None:
        """Without SandboxRecordStore, same record_id with different content is accepted."""
        record_a = _make_record()
        record_b = _make_record(
            record_id=record_a.record_id,
            reviewed_formula=(_ITEM_B,),
        )

        assert record_a.record_id == record_b.record_id
        assert record_a != record_b
        # No rejection mechanism exists at DTO level:
        # two different records with the same record_id coexist in memory.
        assert record_a.reviewed_formula != record_b.reviewed_formula

    def test_l6_3_red_no_serialize_dedicated_function(self) -> None:
        """Without serialize_record, no dedicated canonical-serialize function exists."""
        record = _make_record()

        # model_dump_json (field-order) differs from canonical (sort_keys=True)
        field_order_bytes = record.model_dump_json().encode("utf-8")
        canonical_bytes = canonical_review_bytes(record.model_dump(mode="json"))

        # They differ because model_dump_json follows field definition order,
        # not canonical sort_keys order — confirming a dedicated function is needed.
        assert field_order_bytes != canonical_bytes


# ── GREEN ──────────────────────────────────────────────────────────────────


class TestSandboxRecordStoreGreen:
    """GREEN tests proving store and serialize_record behave correctly."""

    def test_l6_3_green_idempotent_put(self) -> None:
        """Same record put twice must not error and must not change storage."""
        store = SandboxRecordStore()
        record = _make_record()

        store.put(record)
        store.put(record)  # second put — must not raise

        retrieved = store.get(record.record_id)
        assert retrieved == record

    def test_l6_3_green_tampered_record_id_rejected(self) -> None:
        """Same record_id but different content must raise SandboxRecordError."""
        store = SandboxRecordStore()
        record = _make_record()

        store.put(record)

        tampered = _make_record(
            record_id=record.record_id,
            reviewed_formula=(_ITEM_B,),
        )

        with pytest.raises(SandboxRecordError) as raised:
            store.put(tampered)

        assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    def test_l6_3_green_get_hit(self) -> None:
        """get must return the original stored record."""
        store = SandboxRecordStore()
        record = _make_record()

        store.put(record)
        retrieved = store.get(record.record_id)

        assert retrieved == record
        assert retrieved.record_id == record.record_id

    def test_l6_3_green_get_miss(self) -> None:
        """get on unknown record_id must raise SandboxRecordError."""
        store = SandboxRecordStore()

        with pytest.raises(SandboxRecordError) as raised:
            store.get("sandbox-record-" + "e" * 64)

        assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    def test_l6_3_green_serialize_determinism(self) -> None:
        """Same record must produce byte-identical serialization."""
        record = _make_record()

        bytes_a = serialize_record(record)
        bytes_b = serialize_record(record)

        assert bytes_a == bytes_b
        assert isinstance(bytes_a, bytes)

    def test_l6_3_green_serialize_different_records_differ(self) -> None:
        """Different records must produce different serialized bytes."""
        record_a = _make_record()
        record_b = _make_record(
            session_id="different-session-002",
        )

        bytes_a = serialize_record(record_a)
        bytes_b = serialize_record(record_b)

        assert bytes_a != bytes_b

    def test_l6_3_green_store_slots(self) -> None:
        """SandboxRecordStore must only have _records slot."""
        store = SandboxRecordStore()
        assert store.__slots__ == ("_records",)

    def test_l6_3_green_store_no_model_or_network_calls(self) -> None:
        """Store and serialize_record must not call open/print/breakpoint/exec/eval/compile nor network."""
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

    def test_l6_3_green_no_new_import_roots(self) -> None:
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

    def test_l6_3_green_store_multiple_records(self) -> None:
        """Store must hold multiple distinct records simultaneously."""
        store = SandboxRecordStore()

        records = [_make_record(session_id=f"session-{i:03d}") for i in range(5)]
        for record in records:
            store.put(record)

        for record in records:
            retrieved = store.get(record.record_id)
            assert retrieved == record

    def test_l6_3_green_store_put_get_preserves_all_fields(self) -> None:
        """Put then get must preserve every field of the original record."""
        store = SandboxRecordStore()
        record = _make_record()

        store.put(record)
        retrieved = store.get(record.record_id)

        assert retrieved.session_id == record.session_id
        assert retrieved.revision_id == record.revision_id
        assert retrieved.reviewed_formula == record.reviewed_formula
        assert retrieved.safety_result == record.safety_result
        assert retrieved.review_confirm_ref == record.review_confirm_ref
        assert retrieved.assembled_at == record.assembled_at
        assert retrieved.record_version == record.record_version
        assert retrieved.disclaimer == record.disclaimer
        assert retrieved.record_id == record.record_id

    def test_l6_3_green_store_get_miss_chainless(self) -> None:
        """Get miss must produce a chainless, payload-free SandboxRecordError."""
        store = SandboxRecordStore()

        try:
            store.get("sandbox-record-" + "0" * 64)
        except SandboxRecordError as err:
            assert err.__cause__ is None
            assert err.__context__ is None
            assert str(err) == "SANDBOX_RECORD_UNAVAILABLE"
            assert err.__slots__ == ()
        else:
            pytest.fail("expected SandboxRecordError")

    def test_l6_3_green_serialize_canonical_format(self) -> None:
        """Serialize output must match canonical_review_bytes of the model dump."""
        record = _make_record()

        expected = canonical_review_bytes(record.model_dump(mode="json"))
        actual = serialize_record(record)

        assert actual == expected
