"""Bounded offline medical-record reference pipeline for the L6 sandbox.

This module is deliberately isolated from the application runtime, HTTP, DB,
models, and external services.  Its authority input is an already validated
``SandboxRecheckCoordinator`` capability, never a caller-supplied snapshot.
"""

from __future__ import annotations

import hashlib
import json
import threading
import warnings
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_runtime.sandbox_recheck import (
    SandboxRecheckCoordinator,
    SandboxRecheckSnapshotV1,
    SandboxRevisionRecordV1,
)
from app.agent_runtime.sandbox_review import (
    SandboxReviewAction,
    SandboxTestReviewEventV1,
    canonical_review_bytes,
)
from app.agent_runtime.sandbox_safety import (
    MAX_FORMULA_ITEMS,
    MAX_ISSUES,
    SandboxFormulaItemV1,
    SandboxSafetyDecision,
    SandboxSafetyResultV1,
)

_MAX_RECORD_BYTES = 256 * 1024
_MAX_STORED_RECORDS = 1024
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_RECORD_REF_PATTERN = r"^sandbox-record-[0-9a-f]{64}$"
SANDBOX_RECORD_DISCLAIMER: Literal[
    "sandbox_assemble_only_not_a_medical_record"
] = "sandbox_assemble_only_not_a_medical_record"
RECORD_SCHEMA_VERSION: Literal["sandbox-medical-record.v2"] = (
    "sandbox-medical-record.v2"
)
_NO_NARRATION = object()


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SandboxRecordError(ValueError):
    """A fixed, payload-free, chainless record-boundary failure."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("SANDBOX_RECORD_UNAVAILABLE")


class SandboxMedicalRecordData(_StrictFrozenModel):
    """Deeply immutable structured output derived from one confirmed revision."""

    record_id: str = Field(pattern=_RECORD_REF_PATTERN)
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    )
    revision_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    )
    reviewed_formula: tuple[SandboxFormulaItemV1, ...] = Field(
        min_length=1,
        max_length=MAX_FORMULA_ITEMS,
    )
    safety_result: SandboxSafetyResultV1
    review_confirm_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    )
    assembled_at: int = Field(ge=0)
    record_version: Literal["sandbox-medical-record.v2"]
    disclaimer: Literal["sandbox_assemble_only_not_a_medical_record"]

    @model_validator(mode="after")
    def record_is_canonical(self) -> SandboxMedicalRecordData:
        item_ids = tuple(item.item_id for item in self.reviewed_formula)
        if (
            item_ids != tuple(sorted(item_ids))
            or len(item_ids) != len(set(item_ids))
            or self.safety_result.decision is not SandboxSafetyDecision.ALLOW
            or len(self.safety_result.issues) > MAX_ISSUES
        ):
            raise ValueError("record contents are not canonical")
        expected = _record_id(
            session_id=self.session_id,
            revision_id=self.revision_id,
            reviewed_formula=self.reviewed_formula,
            safety_result=self.safety_result,
            review_confirm_ref=self.review_confirm_ref,
            assembled_at=self.assembled_at,
            record_version=self.record_version,
            disclaimer=self.disclaimer,
        )
        if self.record_id != expected:
            raise ValueError("record id mismatch")
        if len(canonical_review_bytes(self.model_dump(mode="json"))) > _MAX_RECORD_BYTES:
            raise ValueError("record size limit exceeded")
        return self


def _trusted_snapshot(value: object) -> SandboxRecheckSnapshotV1 | None:
    """Read a snapshot only through the exact L5 coordinator capability."""

    try:
        if type(value) is not SandboxRecheckCoordinator:
            return None
        snapshot = SandboxRecheckCoordinator.snapshot(value)
        if not _model_graph_is_exact(snapshot):
            return None
        return snapshot
    except Exception:
        return None


def _model_graph_is_exact(value: object) -> bool:
    """Reject hidden Pydantic state anywhere in a nested immutable DTO graph."""

    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        return (
            set(value.__dict__) == set(fields)
            and value.__pydantic_extra__ is None
            and value.__pydantic_private__ is None
            and all(
                _model_graph_is_exact(getattr(value, field_name))
                for field_name in fields
            )
        )
    if type(value) is tuple:
        return all(_model_graph_is_exact(item) for item in value)
    if type(value) is dict:
        return all(
            _model_graph_is_exact(key) and _model_graph_is_exact(item)
            for key, item in value.items()
        )
    return True


def _model_graphs_match(left: object, right: object) -> bool:
    """Compare exact nested types and values after a strict canonical round trip."""

    if isinstance(left, BaseModel) or isinstance(right, BaseModel):
        if type(left) is not type(right) or not isinstance(left, BaseModel):
            return False
        if not isinstance(right, BaseModel):
            return False
        fields = type(left).model_fields
        return (
            set(left.__dict__) == set(fields)
            and set(right.__dict__) == set(fields)
            and left.__pydantic_extra__ is None
            and right.__pydantic_extra__ is None
            and left.__pydantic_private__ is None
            and right.__pydantic_private__ is None
            and all(
                _model_graphs_match(
                    getattr(left, field_name),
                    getattr(right, field_name),
                )
                for field_name in fields
            )
        )
    if type(left) is tuple or type(right) is tuple:
        return (
            type(left) is tuple
            and type(right) is tuple
            and len(left) == len(right)
            and all(
                _model_graphs_match(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    if type(left) is dict or type(right) is dict:
        return (
            type(left) is dict
            and type(right) is dict
            and left.keys() == right.keys()
            and all(
                _model_graphs_match(left[key], right[key])
                for key in left
            )
        )
    return type(left) is type(right) and left == right


def _record_graph_preflight(value: SandboxMedicalRecordData) -> bool:
    """Bound hostile model-construct graphs before JSON serialization."""

    if (
        type(value.reviewed_formula) is not tuple
        or not 1 <= len(value.reviewed_formula) <= MAX_FORMULA_ITEMS
        or type(value.safety_result) is not SandboxSafetyResultV1
        or type(value.safety_result.issues) is not tuple
        or len(value.safety_result.issues) > MAX_ISSUES
    ):
        return False
    budget = _MAX_RECORD_BYTES
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, BaseModel):
            stack.extend(
                getattr(current, field_name)
                for field_name in type(current).model_fields
            )
            budget -= 64
        elif type(current) is tuple:
            if len(current) > MAX_ISSUES:
                return False
            stack.extend(current)
            budget -= 8 * len(current)
        elif type(current) is dict:
            if len(current) > MAX_ISSUES:
                return False
            stack.extend(current.keys())
            stack.extend(current.values())
            budget -= 16 * len(current)
        elif isinstance(current, str):
            budget -= 4 * len(current) + 2
        elif type(current) is bytes:
            budget -= len(current)
        elif isinstance(current, int):
            budget -= 32
        else:
            return False
        if budget < 0:
            return False
    return True


def _normalise_record(value: object) -> SandboxMedicalRecordData | None:
    """Strictly rebuild an exact record, rejecting hidden or stale object state."""

    try:
        if type(value) is not SandboxMedicalRecordData:
            return None
        if not _record_graph_preflight(value):
            return None
        if not _model_graph_is_exact(value):
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            encoded = canonical_review_bytes(value.model_dump(mode="json"))
        if len(encoded) > _MAX_RECORD_BYTES:
            return None
        normalised = SandboxMedicalRecordData.model_validate_json(
            encoded,
            strict=True,
        )
        if encoded != canonical_review_bytes(normalised.model_dump(mode="json")):
            return None
        if not _model_graphs_match(value, normalised):
            return None
        return normalised
    except Exception:
        return None


def _confirmed_authority(
    snapshot: SandboxRecheckSnapshotV1,
) -> tuple[SandboxRevisionRecordV1, SandboxTestReviewEventV1] | None:
    """Return the current revision and its unique applied CONFIRM event."""

    revisions = snapshot.revisions
    if not revisions:
        return None
    current = revisions[-1]
    if (
        current.status != "review_required"
        or current.result is None
        or current.result.decision is not SandboxSafetyDecision.ALLOW
        or current.challenge_ref is None
    ):
        return None
    challenges = tuple(
        challenge
        for challenge in snapshot.review_snapshot.challenges
        if challenge.challenge_ref == current.challenge_ref
        and challenge.state == "applied"
    )
    events = tuple(
        event
        for event in snapshot.review_snapshot.events
        if event.challenge_ref == current.challenge_ref
        and event.action is SandboxReviewAction.CONFIRM
    )
    if len(challenges) != 1 or len(events) != 1:
        return None
    event = events[0]
    attempts = tuple(
        attempt
        for attempt in snapshot.review_snapshot.attempts
        if attempt.resume_attempt_ref == event.resume_attempt_ref
        and attempt.challenge_ref == current.challenge_ref
        and attempt.action is SandboxReviewAction.CONFIRM
        and attempt.state == "applied"
    )
    if len(attempts) != 1:
        return None
    return current, event


def _build_record_from_snapshot(
    snapshot: SandboxRecheckSnapshotV1,
    *,
    namespace: str,
    session_id: str,
    thread_id: str,
    checkpoint_id: str,
) -> SandboxMedicalRecordData | None:
    authority = _confirmed_authority(snapshot)
    if authority is None:
        return None
    current, event = authority
    if (
        current.namespace != namespace
        or current.test_session_id != session_id
        or current.thread_id != thread_id
        or current.checkpoint_id != checkpoint_id
        or current.result is None
    ):
        return None
    reviewed_formula = tuple(current.subject.formula_items)
    safety_result = current.result
    review_confirm_ref = event.resume_attempt_ref
    revision_id = _revision_id(current.revision_ref)
    assembled_at = event.applied_at
    record_id = _record_id(
        session_id=session_id,
        revision_id=revision_id,
        reviewed_formula=reviewed_formula,
        safety_result=safety_result,
        review_confirm_ref=review_confirm_ref,
        assembled_at=assembled_at,
        record_version=RECORD_SCHEMA_VERSION,
        disclaimer=SANDBOX_RECORD_DISCLAIMER,
    )
    return SandboxMedicalRecordData(
        record_id=record_id,
        session_id=session_id,
        revision_id=revision_id,
        reviewed_formula=reviewed_formula,
        safety_result=safety_result,
        review_confirm_ref=review_confirm_ref,
        assembled_at=assembled_at,
        record_version=RECORD_SCHEMA_VERSION,
        disclaimer=SANDBOX_RECORD_DISCLAIMER,
    )


def _record_matches_snapshot(
    record: SandboxMedicalRecordData,
    snapshot: SandboxRecheckSnapshotV1,
    *,
    narration: object = _NO_NARRATION,
) -> bool:
    authority = _confirmed_authority(snapshot)
    if authority is None:
        return False
    current, event = authority
    if current.result is None:
        return False
    expected = {
        "session_id": current.test_session_id,
        "revision_id": _revision_id(current.revision_ref),
        "reviewed_formula": tuple(current.subject.formula_items),
        "safety_result": current.result,
        "review_confirm_ref": event.resume_attempt_ref,
        "assembled_at": event.applied_at,
        "record_version": RECORD_SCHEMA_VERSION,
        "disclaimer": SANDBOX_RECORD_DISCLAIMER,
    }
    if any(
        getattr(record, field_name) != expected_value
        for field_name, expected_value in expected.items()
    ):
        return False
    return narration is _NO_NARRATION or (
        type(narration) is str
        and narration == _render_narration(record)
    )


class SandboxRecordAssembler:
    """Build one deterministic record from an L5 coordinator capability."""

    __slots__ = ()

    def assemble(
        self,
        recheck_coordinator: object,
        *,
        namespace: str,
        session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> SandboxMedicalRecordData:
        record = self._build_record(
            recheck_coordinator,
            namespace=namespace,
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
        )
        if record is None:
            raise SandboxRecordError()
        return record

    @staticmethod
    def _build_record(
        recheck_coordinator: object,
        *,
        namespace: str,
        session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> SandboxMedicalRecordData | None:
        try:
            snapshot = _trusted_snapshot(recheck_coordinator)
            if snapshot is None:
                return None
            return SandboxRecordAssembler._build_from_snapshot(
                snapshot,
                namespace=namespace,
                session_id=session_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
            )
        except Exception:
            return None

    @staticmethod
    def _build_from_snapshot(
        snapshot: SandboxRecheckSnapshotV1,
        *,
        namespace: str,
        session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> SandboxMedicalRecordData | None:
        try:
            return _build_record_from_snapshot(
                snapshot,
                namespace=namespace,
                session_id=session_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
            )
        except Exception:
            return None


class SandboxRecordConsistencyVerifier:
    """Verify record authority and, when supplied, its exact narration."""

    __slots__ = ()

    def verify(
        self,
        record: object,
        *,
        recheck_coordinator: object,
        narration: object = _NO_NARRATION,
    ) -> bool:
        try:
            normalised = _normalise_record(record)
            snapshot = _trusted_snapshot(recheck_coordinator)
            if normalised is None or snapshot is None:
                return False
            return SandboxRecordConsistencyVerifier._verify_snapshot(
                record,
                snapshot,
                narration=narration,
            )
        except Exception:
            return False

    @staticmethod
    def _verify_snapshot(
        record: object,
        snapshot: SandboxRecheckSnapshotV1,
        *,
        narration: object = _NO_NARRATION,
    ) -> bool:
        try:
            normalised = _normalise_record(record)
            return normalised is not None and _record_matches_snapshot(
                normalised,
                snapshot,
                narration=narration,
            )
        except Exception:
            return False


def _record_id(
    *,
    session_id: str,
    revision_id: str,
    reviewed_formula: tuple[SandboxFormulaItemV1, ...],
    safety_result: SandboxSafetyResultV1,
    review_confirm_ref: str,
    assembled_at: int,
    record_version: str,
    disclaimer: str,
) -> str:
    return "sandbox-record-" + _digest(
        {
            "assembled_at": assembled_at,
            "disclaimer": disclaimer,
            "record_version": record_version,
            "review_confirm_ref": review_confirm_ref,
            "reviewed_formula": reviewed_formula,
            "revision_id": revision_id,
            "safety_result": safety_result,
            "session_id": session_id,
        }
    )


def _revision_id(revision_ref: str) -> str:
    return revision_ref.removeprefix("sandbox-recheck-revision-")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_review_bytes(value)).hexdigest()


def serialize_record(record: object) -> bytes:
    """Return canonical bytes for one strictly revalidated record."""

    normalised = _normalise_record(record)
    if normalised is None:
        raise SandboxRecordError()
    return canonical_review_bytes(normalised.model_dump(mode="json"))


def deserialize_record(payload: object) -> SandboxMedicalRecordData:
    """Strictly rebuild one bounded record from canonical JSON bytes or text."""

    parsed: SandboxMedicalRecordData | None = None
    try:
        if type(payload) not in (bytes, str):
            raise ValueError
        raw = cast(bytes | str, payload)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ValueError
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if len(raw_bytes) > _MAX_RECORD_BYTES:
            raise ValueError
        candidate = SandboxMedicalRecordData.model_validate_json(
            raw_bytes,
            strict=True,
        )
        parsed = _normalise_record(candidate)
        if parsed is None or serialize_record(parsed) != raw_bytes:
            parsed = None
    except Exception:
        parsed = None
    if parsed is None:
        raise SandboxRecordError()
    return parsed


def _put_record_with_snapshot(
    store: SandboxRecordStore,
    record: object,
    snapshot: SandboxRecheckSnapshotV1,
) -> None:
    normalised = _normalise_record(record)
    if (
        normalised is None
        or not _record_matches_snapshot(normalised, snapshot)
    ):
        raise SandboxRecordError()
    encoded = serialize_record(normalised)
    with store._lock:
        existing = store._records.get(normalised.record_id)
        if existing is not None:
            if existing != encoded:
                raise SandboxRecordError()
            return
        if len(store._records) >= _MAX_STORED_RECORDS:
            raise SandboxRecordError()
        store._records[normalised.record_id] = encoded


class SandboxRecordStore:
    """Thread-safe bounded canonical store with authority checked on every put."""

    __slots__ = ("_lock", "_records")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, bytes] = {}

    def put(self, record: object, *, recheck_coordinator: object) -> None:
        snapshot = _trusted_snapshot(recheck_coordinator)
        if snapshot is None:
            raise SandboxRecordError()
        _put_record_with_snapshot(self, record, snapshot)

    def get(self, record_id: object) -> SandboxMedicalRecordData:
        encoded: bytes | None = None
        try:
            if type(record_id) is not str:
                raise ValueError
            with self._lock:
                encoded = self._records.get(record_id)
        except Exception:
            encoded = None
        if encoded is None:
            raise SandboxRecordError()
        record = deserialize_record(encoded)
        if record.record_id != record_id:
            raise SandboxRecordError()
        return record


def _render_narration(record: SandboxMedicalRecordData) -> str:
    """Render the accepted L6-4 fields with hostile text JSON-escaped."""

    formula_json = json.dumps(
        tuple(item.model_dump(mode="json") for item in record.reviewed_formula),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return "\n".join(
        (
            "Sandbox Medical Record Narration",
            "=" * 50,
            f"Record ID:      {record.record_id}",
            f"Session ID:     {record.session_id}",
            f"Revision ID:    {record.revision_id}",
            f"Reviewed Formula: {formula_json}",
            f"Safety Decision: {record.safety_result.decision.value}",
            f"Review Confirm Ref: {record.review_confirm_ref}",
            f"Assembled At:   {record.assembled_at}",
            f"Record Version: {record.record_version}",
            f"Disclaimer:     {record.disclaimer}",
            "",
        )
    )


class SandboxRecordNarration:
    """Deterministic allowlist renderer with a fixed failure boundary."""

    __slots__ = ()

    @staticmethod
    def narrate(record: object) -> str:
        normalised = _normalise_record(record)
        if normalised is None:
            raise SandboxRecordError()
        return _render_narration(normalised)


class SandboxRecordPipelineResult(_StrictFrozenModel):
    """Complete output of the bounded L6 reference pipeline."""

    record: SandboxMedicalRecordData
    serialized_record: bytes = Field(max_length=_MAX_RECORD_BYTES)
    narration: str = Field(min_length=1, max_length=_MAX_RECORD_BYTES)

    @model_validator(mode="after")
    def result_is_consistent(self) -> SandboxRecordPipelineResult:
        normalised = _normalise_record(self.record)
        if (
            normalised is None
            or deserialize_record(self.serialized_record) != normalised
            or _render_narration(normalised) != self.narration
        ):
            raise ValueError("record pipeline result mismatch")
        return self


class SandboxRecordPipeline:
    """Sequence assemble -> verify -> store -> serialize -> narrate."""

    __slots__ = ("_assembler", "_recheck_coordinator", "_store", "_verifier")

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("sandbox record pipeline is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        recheck_coordinator: object,
        store: SandboxRecordStore | None = None,
    ) -> None:
        if (
            _trusted_snapshot(recheck_coordinator) is None
            or (store is not None and type(store) is not SandboxRecordStore)
        ):
            raise SandboxRecordError()
        self._recheck_coordinator = recheck_coordinator
        self._assembler = SandboxRecordAssembler()
        self._verifier = SandboxRecordConsistencyVerifier()
        self._store = (
            SandboxRecordStore()
            if store is None
            else store
        )

    def run(
        self,
        *,
        namespace: str,
        session_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> SandboxRecordPipelineResult:
        result: SandboxRecordPipelineResult | None = None
        try:
            if (
                type(self._assembler) is not SandboxRecordAssembler
                or type(self._verifier) is not SandboxRecordConsistencyVerifier
                or type(self._store) is not SandboxRecordStore
            ):
                raise SandboxRecordError()
            snapshot = _trusted_snapshot(self._recheck_coordinator)
            if snapshot is None:
                raise SandboxRecordError()
            record = SandboxRecordAssembler._build_from_snapshot(
                snapshot,
                namespace=namespace,
                session_id=session_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
            )
            if (
                record is None
                or not SandboxRecordConsistencyVerifier._verify_snapshot(
                    record,
                    snapshot,
                )
            ):
                raise SandboxRecordError()
            _put_record_with_snapshot(
                self._store,
                record,
                snapshot,
            )
            stored = SandboxRecordStore.get(self._store, record.record_id)
            serialized = serialize_record(stored)
            if deserialize_record(serialized) != stored:
                raise SandboxRecordError()
            narration = SandboxRecordNarration.narrate(stored)
            result = SandboxRecordPipelineResult(
                record=stored,
                serialized_record=serialized,
                narration=narration,
            )
        except Exception:
            result = None
        if result is None:
            raise SandboxRecordError()
        return result
