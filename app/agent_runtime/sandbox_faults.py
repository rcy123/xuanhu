"""L8-3 沙盒故障注入、恢复、重试与幂等参考实现。

Copyright (c) 2026 xuanhu. All rights reserved.

本模块为 L8-SBX 子任务 L8-3 提供离线、确定性的故障处理组件：

- FaultKind：闭集覆盖 gateway timeout、RAG unavailable、PostgreSQL transient、
  Redis failure、checkpoint failure、duplicate resume、state conflict。
- FaultPlan：显式声明故障注入计划，默认关闭；每次 fault 可归因到
  node/model/tool/policy/verifier/persistence。
- RecoverySession：bounded retry、deadline、single-use resume、
  state-version precondition。
- IdempotentSideEffectLedger：幂等副作用账本，防止重复业务结果。
- CheckpointRestoreGuard：只接受 canonical snapshot，拒绝篡改。

所有数据均为固定合成内容，不涉及真实患者、临床或公开数据。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Schema & resource constants
# ---------------------------------------------------------------------------

SANDBOX_FAULT_PLAN_SCHEMA_VERSION: Literal["sandbox-fault-plan.v1"] = "sandbox-fault-plan.v1"
SANDBOX_RECOVERY_SESSION_SCHEMA_VERSION: Literal["sandbox-recovery-session.v1"] = "sandbox-recovery-session.v1"
SANDBOX_SIDE_EFFECT_LEDGER_SCHEMA_VERSION: Literal["sandbox-side-effect-ledger.v1"] = "sandbox-side-effect-ledger.v1"
SANDBOX_CHECKPOINT_RESTORE_GUARD_SCHEMA_VERSION: Literal["sandbox-checkpoint-restore-guard.v1"] = (
    "sandbox-checkpoint-restore-guard.v1"
)
SANDBOX_FAULT_RESULT_SCHEMA_VERSION: Literal["sandbox-fault-result.v1"] = "sandbox-fault-result.v1"
SANDBOX_FAULT_ADAPTER_VERSION: Literal["sandbox-fault-adapter.v1"] = "sandbox-fault-adapter.v1"

_MAX_RETRIES_PER_RECOVERY = 10
_MAX_RECOVERY_SESSIONS = 64
_MAX_SIDE_EFFECT_ENTRIES = 1024
_MAX_SNAPSHOT_BYTES = 512 * 1024
_MAX_FAULT_PLAN_ENTRIES = 64
_MAX_CANONICAL_BYTES = 256 * 1024

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class SandboxFaultError(ValueError):
    """A fixed, payload-free, fail-closed fault error."""

    __slots__ = ()

    def __init__(self, code: str) -> None:
        super().__init__(code)


class SandboxFaultFailureCode(StrEnum):
    FAULT_INJECTION_DISABLED = "SANDBOX_FAULT_INJECTION_DISABLED"
    FAULT_KIND_NOT_RECOGNIZED = "SANDBOX_FAULT_KIND_NOT_RECOGNIZED"
    FAULT_ATTRIBUTION_INVALID = "SANDBOX_FAULT_ATTRIBUTION_INVALID"
    RECOVERY_RETRIES_EXCEEDED = "SANDBOX_RECOVERY_RETRIES_EXCEEDED"
    RECOVERY_DEADLINE_EXCEEDED = "SANDBOX_RECOVERY_DEADLINE_EXCEEDED"
    RECOVERY_RESUME_ALREADY_USED = "SANDBOX_RECOVERY_RESUME_ALREADY_USED"
    RECOVERY_STATE_VERSION_CONFLICT = "SANDBOX_RECOVERY_STATE_VERSION_CONFLICT"
    SIDE_EFFECT_DUPLICATE = "SANDBOX_SIDE_EFFECT_DUPLICATE"
    CHECKPOINT_SNAPSHOT_INVALID = "SANDBOX_CHECKPOINT_SNAPSHOT_INVALID"
    CHECKPOINT_RESTORE_REJECTED = "SANDBOX_CHECKPOINT_RESTORE_REJECTED"
    INTERNAL_FAILURE = "SANDBOX_FAULT_INTERNAL_FAILURE"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# FaultKind — closed-set fault types
# ---------------------------------------------------------------------------


class FaultKind(StrEnum):
    """Closed set of recognized fault kinds for L8-3 sandbox."""

    GATEWAY_TIMEOUT = "gateway_timeout"
    RAG_UNAVAILABLE = "rag_unavailable"
    POSTGRESQL_TRANSIENT = "postgresql_transient"
    REDIS_FAILURE = "redis_failure"
    CHECKPOINT_FAILURE = "checkpoint_failure"
    DUPLICATE_RESUME = "duplicate_resume"
    STATE_CONFLICT = "state_conflict"


class FaultAttribution(StrEnum):
    """Attribution domain for injected faults."""

    NODE = "node"
    MODEL = "model"
    TOOL = "tool"
    POLICY = "policy"
    VERIFIER = "verifier"
    PERSISTENCE = "persistence"


# ---------------------------------------------------------------------------
# FaultPlan DTOs
# ---------------------------------------------------------------------------


class FaultEntryV1(_StrictFrozenModel):
    """A single fault injection entry in a FaultPlan."""

    fault_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    fault_kind: FaultKind
    attribution: FaultAttribution
    enabled: bool = False
    max_injections: int = Field(default=1, ge=1, le=100)
    injection_count: int = Field(default=0, ge=0)


class FaultPlanV1(_StrictFrozenModel):
    """Explicit fault injection plan.

    Default off (all entries disabled). Each fault is attributable
    to a specific domain.
    """

    schema_version: Literal["sandbox-fault-plan.v1"]
    entries: tuple[FaultEntryV1, ...] = Field(max_length=_MAX_FAULT_PLAN_ENTRIES)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def plan_digest_is_derived(self) -> FaultPlanV1:
        expected = _derive_plan_digest(self.entries)
        if self.plan_digest != expected:
            raise ValueError("plan_digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        entries: Sequence[FaultEntryV1],
    ) -> FaultPlanV1:
        entries_list = list(entries)
        if len(entries_list) > _MAX_FAULT_PLAN_ENTRIES:
            raise ValueError(f"too many entries: {len(entries_list)} > {_MAX_FAULT_PLAN_ENTRIES}")
        canonical = tuple(sorted(entries_list, key=lambda e: e.fault_id))
        plan_digest = _derive_plan_digest(canonical)
        return cls(
            schema_version="sandbox-fault-plan.v1",
            entries=canonical,
            plan_digest=plan_digest,
        )


# ---------------------------------------------------------------------------
# Recovery DTOs
# ---------------------------------------------------------------------------


class RecoveryStateV1(_StrictFrozenModel):
    """State of a recovery session."""

    session_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    resume_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    state_version: int = Field(ge=1)
    retry_count: int = Field(ge=0, le=_MAX_RETRIES_PER_RECOVERY)
    deadline_at: datetime
    used: bool = False
    succeeded: bool = False


class RecoverySessionV1(_StrictFrozenModel):
    """Stable record of a recovery session with bounded retry/deadline.

    Enforces:
    - single-use resume (each resume_id can be used at most once)
    - state-version precondition (state_version must match the current
      version being recovered; a conflict means another writer modified it)
    - bounded retry count
    - deadline expiry
    """

    schema_version: Literal["sandbox-recovery-session.v1"]
    sessions: tuple[RecoveryStateV1, ...] = Field(max_length=_MAX_RECOVERY_SESSIONS)
    session_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    side_effect_ledger_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def session_digest_is_derived(self) -> RecoverySessionV1:
        expected = _derive_session_digest(self.sessions)
        if self.session_digest != expected:
            raise ValueError("session_digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        sessions: Sequence[RecoveryStateV1],
    ) -> RecoverySessionV1:
        sessions_list = list(sessions)
        if len(sessions_list) > _MAX_RECOVERY_SESSIONS:
            raise ValueError(f"too many sessions: {len(sessions_list)} > {_MAX_RECOVERY_SESSIONS}")
        canonical = tuple(sorted(sessions_list, key=lambda s: s.session_id))
        session_digest = _derive_session_digest(canonical)
        return cls(
            schema_version="sandbox-recovery-session.v1",
            sessions=canonical,
            session_digest=session_digest,
            side_effect_ledger_digest=_canonical_sha256([]),
        )


# ---------------------------------------------------------------------------
# Idempotent Side Effect DTOs
# ---------------------------------------------------------------------------


class SideEffectEntryV1(_StrictFrozenModel):
    """One recorded side effect with idempotency tracking."""

    effect_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    idempotency_key: str = Field(min_length=1, max_length=256)
    effect_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    completed: bool = False


class SideEffectLedgerV1(_StrictFrozenModel):
    """Ledger of recorded side effects for idempotent replay protection.

    Same idempotency_key + same effect_digest -> silent no-op (idempotent).
    Same idempotency_key + different effect_digest -> rejected.
    """

    schema_version: Literal["sandbox-side-effect-ledger.v1"]
    entries: tuple[SideEffectEntryV1, ...] = Field(max_length=_MAX_SIDE_EFFECT_ENTRIES)
    ledger_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def ledger_digest_is_derived(self) -> SideEffectLedgerV1:
        expected = _derive_side_effect_ledger_digest(self.entries)
        if self.ledger_digest != expected:
            raise ValueError("ledger_digest mismatch")
        return self

    @classmethod
    def build(cls, entries: Sequence[SideEffectEntryV1]) -> SideEffectLedgerV1:
        entries_list = list(entries)
        if len(entries_list) > _MAX_SIDE_EFFECT_ENTRIES:
            raise ValueError(f"too many entries: {len(entries_list)} > {_MAX_SIDE_EFFECT_ENTRIES}")
        canonical = tuple(sorted(entries_list, key=lambda e: e.effect_id))
        ledger_digest = _derive_side_effect_ledger_digest(canonical)
        return cls(
            schema_version="sandbox-side-effect-ledger.v1",
            entries=canonical,
            ledger_digest=ledger_digest,
        )


# ---------------------------------------------------------------------------
# Checkpoint restore DTOs
# ---------------------------------------------------------------------------


class CheckpointSnapshotV1(_StrictFrozenModel):
    """A canonical snapshot for checkpoint restore.

    Restore must only accept canonical snapshots with verified digest.
    """

    schema_version: Literal["sandbox-checkpoint-restore-guard.v1"] = "sandbox-checkpoint-restore-guard.v1"
    data: str = Field(min_length=0)
    digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)


# ---------------------------------------------------------------------------
# Top-level fault result
# ---------------------------------------------------------------------------


class SandboxFaultResultV1(_StrictFrozenModel):
    """Composite result of a fault/recovery operation."""

    schema_version: Literal["sandbox-fault-result.v1"]
    adapter_version: Literal["sandbox-fault-adapter.v1"]
    fault_injected: bool = False
    fault_kind: FaultKind | None = None
    attribution: FaultAttribution | None = None
    recovery_needed: bool = False
    recovery_succeeded: bool = False
    side_effect_replayed: bool = False
    result_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def result_digest_is_derived(self) -> SandboxFaultResultV1:
        expected = _derive_fault_result_digest(self)
        if self.result_digest != expected:
            raise ValueError("result_digest mismatch")
        return self


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def canonical_json_bytes(value: object) -> bytes:
    """Serialize to stable canonical JSON."""
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return value


def _derive_plan_digest(entries: tuple[FaultEntryV1, ...]) -> str:
    return _canonical_sha256(
        {
            "entries": [
                {
                    "fault_id": e.fault_id,
                    "fault_kind": e.fault_kind,
                    "attribution": e.attribution,
                    "enabled": e.enabled,
                    "max_injections": e.max_injections,
                    "injection_count": e.injection_count,
                }
                for e in entries
            ],
        }
    )


def _derive_session_digest(sessions: tuple[RecoveryStateV1, ...]) -> str:
    return _canonical_sha256(
        {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "resume_id": s.resume_id,
                    "state_version": s.state_version,
                    "retry_count": s.retry_count,
                    "deadline_at": s.deadline_at.isoformat()
                    if hasattr(s.deadline_at, "isoformat")
                    else str(s.deadline_at),
                    "used": s.used,
                    "succeeded": s.succeeded,
                }
                for s in sessions
            ],
        }
    )


def _derive_side_effect_ledger_digest(entries: tuple[SideEffectEntryV1, ...]) -> str:
    return _canonical_sha256(
        {
            "entries": [
                {
                    "effect_id": e.effect_id,
                    "idempotency_key": e.idempotency_key,
                    "effect_digest": e.effect_digest,
                    "completed": e.completed,
                }
                for e in entries
            ],
        }
    )


def _derive_fault_result_digest(result: SandboxFaultResultV1) -> str:
    return _canonical_sha256(
        {
            "fault_injected": result.fault_injected,
            "fault_kind": result.fault_kind,
            "attribution": result.attribution,
            "recovery_needed": result.recovery_needed,
            "recovery_succeeded": result.recovery_succeeded,
            "side_effect_replayed": result.side_effect_replayed,
        }
    )


def _raise_error(code: SandboxFaultFailureCode) -> NoReturn:
    raise SandboxFaultError(code.value) from None


# ---------------------------------------------------------------------------
# FaultInjector — controlled fault injection
# ---------------------------------------------------------------------------


class FaultInjector:
    """Inject faults based on an explicit FaultPlan.

    Default off — no faults are injected unless the plan enables them.
    """

    __slots__ = ("_plan", "_lock")

    def __init__(self, plan: FaultPlanV1 | None = None) -> None:
        self._plan = plan or FaultPlanV1.build(entries=())
        self._lock = threading.RLock()

    @property
    def plan(self) -> FaultPlanV1:
        return self._plan

    def inject(self, fault_kind: FaultKind) -> SandboxFaultResultV1:
        """Attempt to inject a fault of the given kind.

        Fault only proceeds if the plan has a matching enabled entry
        with remaining injection capacity.
        """
        unexpected_failure = False
        try:
            return self._inject(fault_kind)
        except SandboxFaultError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxFaultFailureCode.INTERNAL_FAILURE)
        raise AssertionError("unreachable")

    def _inject(self, fault_kind: FaultKind) -> SandboxFaultResultV1:
        if not isinstance(fault_kind, FaultKind):
            try:
                fault_kind = FaultKind(fault_kind)
            except (ValueError, LookupError):
                _raise_error(SandboxFaultFailureCode.FAULT_KIND_NOT_RECOGNIZED)

        if fault_kind not in list(FaultKind):
            _raise_error(SandboxFaultFailureCode.FAULT_KIND_NOT_RECOGNIZED)

        with self._lock:
            for entry in self._plan.entries:
                if entry.fault_kind != fault_kind:
                    continue
                if not entry.enabled:
                    _raise_error(SandboxFaultFailureCode.FAULT_INJECTION_DISABLED)
                if entry.injection_count >= entry.max_injections:
                    _raise_error(SandboxFaultFailureCode.FAULT_INJECTION_DISABLED)
                # Fault matches and can be injected — rebuild plan with
                # incremented injection_count so that a new FaultInjector
                # constructed from self.plan cannot bypass max_injections.
                updated_entry = FaultEntryV1(
                    fault_id=entry.fault_id,
                    fault_kind=entry.fault_kind,
                    attribution=entry.attribution,
                    enabled=entry.enabled,
                    max_injections=entry.max_injections,
                    injection_count=entry.injection_count + 1,
                )
                new_entries = tuple(updated_entry if e.fault_id == entry.fault_id else e for e in self._plan.entries)
                self._plan = FaultPlanV1.build(entries=new_entries)
                digest = _canonical_sha256(
                    {
                        "fault_injected": True,
                        "fault_kind": fault_kind,
                        "attribution": entry.attribution,
                        "recovery_needed": True,
                        "recovery_succeeded": False,
                        "side_effect_replayed": False,
                    }
                )
                return SandboxFaultResultV1(
                    schema_version="sandbox-fault-result.v1",
                    adapter_version="sandbox-fault-adapter.v1",
                    fault_injected=True,
                    fault_kind=fault_kind,
                    attribution=entry.attribution,
                    recovery_needed=True,
                    result_digest=digest,
                )

            # No matching entry found → raise error
            _raise_error(SandboxFaultFailureCode.FAULT_INJECTION_DISABLED)
            raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# RecoverySession — bounded retry, single-use resume, state-version check
# ---------------------------------------------------------------------------


class RecoverySession:
    """Manages recovery with bounded retry, deadline, and state-version checks.

    Properties:
    - bounded retry: retry_count > max → RECOVERY_RETRIES_EXCEEDED
    - deadline: current time > deadline → RECOVERY_DEADLINE_EXCEEDED
    - single-use resume: each resume_id can be used at most once
    - state-version precondition: state_version must match expected value
    """

    __slots__ = ("_sessions", "_session_map", "_lock")

    def __init__(self, sessions: RecoverySessionV1 | None = None) -> None:
        self._sessions = sessions or RecoverySessionV1.build(sessions=())
        self._session_map: dict[str, RecoveryStateV1] = {state.session_id: state for state in self._sessions.sessions}
        self._lock = threading.RLock()

    @property
    def sessions(self) -> RecoverySessionV1:
        return self._sessions

    def resume(
        self,
        session_id: str,
        resume_id: str,
        *,
        expected_state_version: int,
        max_retries: int = _MAX_RETRIES_PER_RECOVERY,
    ) -> bool:
        """Resume a recovery session with preconditions.

        Args:
            session_id: The session to recover.
            resume_id: Single-use resume identifier.
            expected_state_version: Required state version (must match).
            max_retries: Maximum number of retries allowed.

        Returns True if recovery succeeds, raises SandboxFaultError on failure.
        """
        unexpected_failure = False
        try:
            return self._resume(
                session_id,
                resume_id,
                expected_state_version=expected_state_version,
                max_retries=max_retries,
            )
        except SandboxFaultError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxFaultFailureCode.INTERNAL_FAILURE)
        raise AssertionError("unreachable")

    def _resume(
        self,
        session_id: str,
        resume_id: str,
        *,
        expected_state_version: int,
        max_retries: int,
    ) -> bool:
        with self._lock:
            # Find existing session or create a new one
            session = self._find_session(session_id)
            if session is None:
                # First recovery — create session entry
                session = RecoveryStateV1(
                    session_id=session_id,
                    resume_id=resume_id,
                    state_version=expected_state_version,
                    retry_count=0,
                    deadline_at=datetime.now(UTC) + timedelta(seconds=60),
                    used=True,
                    succeeded=True,
                )
                self._session_map[session_id] = session
                self._sessions = RecoverySessionV1.build(sessions=tuple(self._session_map.values()))
                return True

            now = datetime.now(UTC)

            # Deadline check
            if now >= session.deadline_at:
                _raise_error(SandboxFaultFailureCode.RECOVERY_DEADLINE_EXCEEDED)

            # Single-use resume check
            if session.used:
                _raise_error(SandboxFaultFailureCode.RECOVERY_RESUME_ALREADY_USED)

            # State-version precondition
            if session.state_version != expected_state_version:
                _raise_error(SandboxFaultFailureCode.RECOVERY_STATE_VERSION_CONFLICT)

            # Bounded retry
            if session.retry_count >= max_retries:
                _raise_error(SandboxFaultFailureCode.RECOVERY_RETRIES_EXCEEDED)

            # Commit the transition atomically before returning.  The resume
            # token is single-use and the state cursor advances so a stale
            # caller cannot replay the same transition.
            updated = RecoveryStateV1(
                session_id=session.session_id,
                resume_id=resume_id,
                state_version=session.state_version + 1,
                retry_count=session.retry_count + 1,
                deadline_at=session.deadline_at,
                used=True,
                succeeded=True,
            )
            self._session_map[session_id] = updated
            self._sessions = RecoverySessionV1.build(sessions=tuple(self._session_map.values()))
            return True

    def _find_session(self, session_id: str) -> RecoveryStateV1 | None:
        return self._session_map.get(session_id)


# ---------------------------------------------------------------------------
# IdempotentSideEffectLedger — prevent duplicate business results
# ---------------------------------------------------------------------------


class IdempotentSideEffectLedger:
    """Ledger for idempotent side effect tracking.

    - Same idempotency_key + same effect_digest → idempotent (silent no-op).
    - Same idempotency_key + different effect_digest → rejected.
    - Supports replay detection without producing duplicate business results.
    """

    __slots__ = ("_ledger", "_lock", "_records")

    def __init__(self, ledger: SideEffectLedgerV1 | None = None) -> None:
        self._ledger = ledger or SideEffectLedgerV1.build(entries=())
        self._lock = threading.RLock()
        self._records: dict[str, SideEffectEntryV1] = {entry.idempotency_key: entry for entry in self._ledger.entries}

    @property
    def ledger(self) -> SideEffectLedgerV1:
        return self._ledger

    def record(
        self,
        effect_id: str,
        idempotency_key: str,
        effect_data: object,
    ) -> bool:
        """Record a side effect with idempotency protection.

        Returns True if recorded, False if idempotent no-op.
        Raises SandboxFaultError on idempotency conflict.
        """
        unexpected_failure = False
        try:
            return self._record(effect_id, idempotency_key, effect_data)
        except SandboxFaultError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxFaultFailureCode.INTERNAL_FAILURE)
        raise AssertionError("unreachable")

    def _record(self, effect_id: str, idempotency_key: str, effect_data: object) -> bool:
        effect_digest = _canonical_sha256(effect_data)

        with self._lock:
            # Check for existing entry with same idempotency_key
            entry = self._records.get(idempotency_key)
            if entry is not None:
                if entry.effect_digest == effect_digest:
                    return False
                _raise_error(SandboxFaultFailureCode.SIDE_EFFECT_DUPLICATE)

            entry = SideEffectEntryV1(
                effect_id=effect_id,
                idempotency_key=idempotency_key,
                effect_digest=effect_digest,
                completed=True,
            )
            self._records[idempotency_key] = entry
            # Rebuild the exposed immutable ledger so that
            # ledger.ledger reflects all recorded entries.
            self._ledger = SideEffectLedgerV1.build(entries=list(self._records.values()))
            return True

    def is_replayed(self, idempotency_key: str) -> bool:
        """Check if an idempotency key has been recorded (replayed)."""
        with self._lock:
            entry = self._records.get(idempotency_key)
            return entry is not None and entry.completed


# ---------------------------------------------------------------------------
# CheckpointRestoreGuard — only canonical snapshots
# ---------------------------------------------------------------------------


class CheckpointRestoreGuard:
    """Guard that only allows restore from canonical snapshots.

    Rejects:
    - tampered snapshots (digest mismatch)
    - non-canonical data
    - incomplete state

    Restore failure never produces partial success or duplicate business results.
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def validate(self, snapshot: CheckpointSnapshotV1) -> bool:
        """Validate a checkpoint snapshot before restore.

        Checks that the digest matches the data and that the data
        is well-formed canonical JSON.
        """
        unexpected_failure = False
        try:
            return self._validate(snapshot)
        except SandboxFaultError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxFaultFailureCode.INTERNAL_FAILURE)
        raise AssertionError("unreachable")

    def _validate(self, snapshot: CheckpointSnapshotV1) -> bool:
        # Data must be valid JSON
        try:
            parsed = json.loads(snapshot.data)
        except (json.JSONDecodeError, ValueError):
            _raise_error(SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID)

        # Must be a dict (JSON object) for checkpoint state
        if not isinstance(parsed, dict):
            _raise_error(SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID)

        # Must be canonical JSON (sorted keys, no whitespace, compact separators)
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if snapshot.data != canonical:
            _raise_error(SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID)

        # Expected digest must match
        expected_digest = hashlib.sha256(snapshot.data.encode("utf-8")).hexdigest()
        if snapshot.digest != expected_digest:
            _raise_error(SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID)

        return True

    def restore(self, snapshot: CheckpointSnapshotV1) -> dict[str, object]:
        """Validate and return the decoded checkpoint data.

        Never produces partial success — either fully succeeds or raises.
        """
        if not self.validate(snapshot):
            _raise_error(SandboxFaultFailureCode.CHECKPOINT_RESTORE_REJECTED)
        return cast(dict[str, object], json.loads(snapshot.data))


# ---------------------------------------------------------------------------
# SandboxFaultAdapter — composite fault/recovery adapter
# ---------------------------------------------------------------------------


class SandboxFaultAdapter:
    """Composite fault/recovery adapter for L8-3 checks."""

    __slots__ = ("_injector", "_recovery", "_ledger", "_checkpoint")

    def __init__(
        self,
        *,
        injector: FaultInjector,
        recovery: RecoverySession,
        ledger: IdempotentSideEffectLedger,
        checkpoint: CheckpointRestoreGuard,
    ) -> None:
        self._injector = injector
        self._recovery = recovery
        self._ledger = ledger
        self._checkpoint = checkpoint

    @property
    def injector(self) -> FaultInjector:
        return self._injector

    @property
    def recovery(self) -> RecoverySession:
        return self._recovery

    @property
    def ledger(self) -> IdempotentSideEffectLedger:
        return self._ledger

    @property
    def checkpoint(self) -> CheckpointRestoreGuard:
        return self._checkpoint
