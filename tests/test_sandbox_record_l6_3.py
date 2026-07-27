"""L6-3 bounded canonical store and serialization regressions."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.agent_runtime.sandbox_record import (
    SandboxMedicalRecordData,
    SandboxRecordError,
    SandboxRecordStore,
    canonical_review_bytes,
    deserialize_record,
    serialize_record,
)
from app.agent_runtime.sandbox_review import SandboxReviewAction
from tests.test_sandbox_record_l6_1 import (
    _assemble,
    _confirm_distinct_review_required_state,
    _confirm_review_required_state,
)
from tests.test_sandbox_record_l6_2 import _model_construct_record


def test_l6_3_put_is_idempotent_and_get_returns_canonical_clone() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    store = SandboxRecordStore()

    store.put(record, recheck_coordinator=coordinator)
    store.put(record, recheck_coordinator=coordinator)
    first = store.get(record.record_id)
    second = store.get(record.record_id)

    assert first == second == record
    assert first is not record
    assert first is not second


def test_l6_3_store_keeps_multiple_distinct_authority_bound_records() -> None:
    first_coordinator = _confirm_review_required_state()
    second_coordinator = _confirm_distinct_review_required_state()
    first = _assemble(first_coordinator)
    second = _assemble(second_coordinator)
    store = SandboxRecordStore()

    store.put(first, recheck_coordinator=first_coordinator)
    store.put(second, recheck_coordinator=second_coordinator)

    assert first.record_id != second.record_id
    assert store.get(first.record_id) == first
    assert store.get(second.record_id) == second


def test_l6_3_first_write_requires_l5_authority() -> None:
    confirmed = _confirm_review_required_state()
    rejected = _confirm_review_required_state(SandboxReviewAction.REJECT)
    record = _assemble(confirmed)
    store = SandboxRecordStore()

    with pytest.raises(SandboxRecordError):
        store.put(record, recheck_coordinator=rejected)
    with pytest.raises(SandboxRecordError):
        store.get(record.record_id)


@pytest.mark.parametrize(
    "tamper",
    (
        lambda record: record.model_copy(
            update={"diagnosis": "hidden-field"}
        ),
        lambda record: SandboxMedicalRecordData.model_construct(
            **{
                **record.model_dump(mode="python"),
                "record_id": "sandbox-record-" + "0" * 64,
            }
        ),
    ),
)
def test_l6_3_invalid_first_write_is_rejected(tamper) -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    store = SandboxRecordStore()

    with pytest.raises(SandboxRecordError):
        store.put(
            tamper(record),
            recheck_coordinator=coordinator,
        )


def test_l6_3_concurrent_same_record_put_is_atomic_and_idempotent() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    store = SandboxRecordStore()

    def put_once(_: int) -> None:
        store.put(record, recheck_coordinator=coordinator)

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(put_once, range(64)))

    assert store.get(record.record_id) == record
    assert len(store._records) == 1  # type: ignore[attr-defined]


def test_l6_3_canonical_round_trip_is_byte_identical() -> None:
    record = _assemble()

    encoded = serialize_record(record)
    decoded = deserialize_record(encoded)

    assert decoded == record
    assert serialize_record(decoded) == encoded
    assert encoded == canonical_review_bytes(record.model_dump(mode="json"))


def test_l6_3_distinct_record_inputs_have_distinct_canonical_bytes() -> None:
    record = _assemble()
    changed = _model_construct_record(
        record,
        assembled_at=record.assembled_at + 1,
    )

    assert serialize_record(record) != serialize_record(changed)
    assert record.record_id != changed.record_id


def test_l6_3_get_rejects_storage_key_record_id_mismatch() -> None:
    record = _assemble()
    store = SandboxRecordStore()
    wrong_key = "sandbox-record-" + "0" * 64
    store._records[wrong_key] = serialize_record(record)  # type: ignore[attr-defined]

    with pytest.raises(SandboxRecordError):
        store.get(wrong_key)


def test_l6_3_deserialize_rejects_noncanonical_json_text() -> None:
    encoded = serialize_record(_assemble())

    with pytest.raises(SandboxRecordError):
        deserialize_record(b" " + encoded)
    with pytest.raises(SandboxRecordError):
        deserialize_record(encoded + b"\n")

    assert deserialize_record(encoded.decode("utf-8")) == _assemble()


@pytest.mark.parametrize(
    "payload",
    (
        b"not-json",
        "{}",
        b"x" * (256 * 1024 + 1),
        bytearray(b"{}"),
        None,
    ),
    ids=("bad-json", "empty-object", "oversized", "bytearray", "none"),
)
def test_l6_3_deserialize_rejects_invalid_or_oversized_payload(payload) -> None:
    with pytest.raises(SandboxRecordError) as raised:
        deserialize_record(payload)

    assert str(raised.value) == "SANDBOX_RECORD_UNAVAILABLE"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_l6_3_serialize_rejects_hidden_or_stale_state() -> None:
    record = _assemble()
    hidden = record.model_copy(update={"symptom": "forged"})

    with pytest.raises(SandboxRecordError):
        serialize_record(hidden)


def test_l6_3_get_miss_and_bad_key_fail_fixed_closed() -> None:
    store = SandboxRecordStore()

    for key in ("sandbox-record-" + "f" * 64, [], None):
        with pytest.raises(SandboxRecordError) as raised:
            store.get(key)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None


def test_l6_3_store_uses_lock_and_canonical_bytes_only() -> None:
    coordinator = _confirm_review_required_state()
    record = _assemble(coordinator)
    store = SandboxRecordStore()
    store.put(record, recheck_coordinator=coordinator)

    assert SandboxRecordStore.__slots__ == ("_lock", "_records")
    assert type(store._records[record.record_id]) is bytes  # type: ignore[attr-defined]


def test_l6_3_module_has_no_io_network_or_process_calls() -> None:
    source = Path("app/agent_runtime/sandbox_record.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"open", "print", "breakpoint", "exec", "eval", "compile"}

    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in forbidden_calls
        for node in ast.walk(tree)
    )
    assert not any(
        token in source
        for token in ("httpx", "requests", "socket", "subprocess", "sqlalchemy")
    )
