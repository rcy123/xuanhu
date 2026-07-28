"""L8-3 sandbox fault injection, recovery, retry, idempotency tests.

Tests cover:
1. FaultKind — closed-set fault types
2. FaultPlan — explicit plan, default off, attributable
3. FaultInjector — controlled fault injection
4. RecoverySession — bounded retry, deadline, single-use resume, state-version
5. IdempotentSideEffectLedger — duplicate detection, idempotent replay
6. CheckpointRestoreGuard — canonical snapshot only
7. SandboxFaultAdapter — composite fault/recovery
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.agent_runtime.sandbox_faults import (
    CheckpointRestoreGuard,
    CheckpointSnapshotV1,
    FaultAttribution,
    FaultEntryV1,
    FaultInjector,
    FaultKind,
    FaultPlanV1,
    IdempotentSideEffectLedger,
    RecoverySession,
    RecoverySessionV1,
    RecoveryStateV1,
    SandboxFaultAdapter,
    SandboxFaultError,
    SandboxFaultFailureCode,
    SideEffectEntryV1,
    SideEffectLedgerV1,
    canonical_json_bytes,
)

# ===================================================================
# FaultKind + FaultPlan Tests
# ===================================================================


class TestFaultKind:
    def test_closed_set_size(self) -> None:
        assert len(FaultKind) == 7

    def test_all_kinds_present(self) -> None:
        kinds = {k.value for k in FaultKind}
        assert "gateway_timeout" in kinds
        assert "rag_unavailable" in kinds
        assert "postgresql_transient" in kinds
        assert "redis_failure" in kinds
        assert "checkpoint_failure" in kinds
        assert "duplicate_resume" in kinds
        assert "state_conflict" in kinds


class TestFaultEntry:
    def test_default_disabled(self) -> None:
        entry = FaultEntryV1(
            fault_id="fault-1",
            fault_kind=FaultKind.GATEWAY_TIMEOUT,
            attribution=FaultAttribution.MODEL,
        )
        assert not entry.enabled

    def test_minimal_entry(self) -> None:
        entry = FaultEntryV1(
            fault_id="fault-1",
            fault_kind=FaultKind.GATEWAY_TIMEOUT,
            attribution=FaultAttribution.MODEL,
            enabled=True,
        )
        assert entry.enabled
        assert entry.max_injections == 1
        assert entry.injection_count == 0


class TestFaultPlan:
    def test_default_plan_is_empty(self) -> None:
        plan = FaultPlanV1.build(entries=[])
        assert len(plan.entries) == 0

    def test_plan_with_entries(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.MODEL,
                enabled=True,
            ),
        ]
        plan = FaultPlanV1.build(entries=entries)
        assert len(plan.entries) == 1
        assert len(plan.plan_digest) == 64

    def test_plan_digest_stability(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.MODEL,
            ),
        ]
        plan1 = FaultPlanV1.build(entries=entries)
        plan2 = FaultPlanV1.build(entries=entries)
        assert plan1.plan_digest == plan2.plan_digest

    def test_plan_digest_validation(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.MODEL,
            ),
        ]
        plan = FaultPlanV1.build(entries=entries)
        # Tamper with digest
        with pytest.raises(ValueError, match="plan_digest mismatch"):
            FaultPlanV1(
                schema_version="sandbox-fault-plan.v1",
                entries=plan.entries,
                plan_digest="0" * 64,
            )


class TestBuilderMaxCapacity:
    """Builders must fail closed when inputs exceed their maximum."""

    def test_fault_plan_rejects_oversized(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id=f"fault-{i}",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.MODEL,
            )
            for i in range(65)  # 65 > 64
        ]
        with pytest.raises(ValueError, match="too many entries"):
            FaultPlanV1.build(entries=entries)

    def test_fault_plan_accepts_at_max(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id=f"fault-{i}",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.MODEL,
            )
            for i in range(64)
        ]
        plan = FaultPlanV1.build(entries=entries)
        assert len(plan.entries) == 64

    def test_recovery_session_rejects_oversized(self) -> None:
        sessions = [
            RecoveryStateV1(
                session_id=f"session-{i}",
                resume_id=f"resume-{i}",
                state_version=1,
                retry_count=0,
                deadline_at=datetime.now(UTC) + timedelta(hours=1),
                used=False,
                succeeded=False,
            )
            for i in range(65)
        ]
        with pytest.raises(ValueError, match="too many sessions"):
            RecoverySessionV1.build(sessions=sessions)

    def test_side_effect_ledger_rejects_oversized(self) -> None:
        entries = [
            SideEffectEntryV1(
                effect_id=f"effect-{i}",
                idempotency_key=f"key-{i}",
                effect_digest="a" * 64,
                completed=False,
            )
            for i in range(1025)  # 1025 > 1024
        ]
        with pytest.raises(ValueError, match="too many entries"):
            SideEffectLedgerV1.build(entries=entries)


# ===================================================================
# FaultInjector Tests
# ===================================================================


class TestFaultInjector:
    def test_default_disabled_no_fault(self) -> None:
        injector = FaultInjector()
        with pytest.raises(SandboxFaultError) as exc_info:
            injector.inject(FaultKind.GATEWAY_TIMEOUT)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.FAULT_INJECTION_DISABLED.value

    def test_enabled_fault_injects(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.MODEL,
                enabled=True,
            ),
        ]
        plan = FaultPlanV1.build(entries=entries)
        injector = FaultInjector(plan)
        result = injector.inject(FaultKind.GATEWAY_TIMEOUT)
        assert result.fault_injected
        assert result.fault_kind == FaultKind.GATEWAY_TIMEOUT
        assert result.attribution == FaultAttribution.MODEL
        assert result.recovery_needed

    def test_inject_unknown_kind_raises(self) -> None:
        injector = FaultInjector()
        with pytest.raises(SandboxFaultError) as exc_info:
            injector.inject(FaultKind.STATE_CONFLICT)  # no entry at all
        assert exc_info.value.args[0] == SandboxFaultFailureCode.FAULT_INJECTION_DISABLED.value

    def test_max_injections_enforced(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.REDIS_FAILURE,
                attribution=FaultAttribution.PERSISTENCE,
                enabled=True,
                max_injections=1,
            ),
        ]
        plan = FaultPlanV1.build(entries=entries)
        injector = FaultInjector(plan)

        # First injection succeeds
        result = injector.inject(FaultKind.REDIS_FAILURE)
        assert result.fault_injected

        # Second injection should be rejected (max_injections reached)
        with pytest.raises(SandboxFaultError) as exc_info:
            injector.inject(FaultKind.REDIS_FAILURE)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.FAULT_INJECTION_DISABLED.value

    def test_injection_count_persists_across_reconstruction(self) -> None:
        """Reconstructing FaultInjector from injector.plan preserves counts."""
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.REDIS_FAILURE,
                attribution=FaultAttribution.PERSISTENCE,
                enabled=True,
                max_injections=1,
            ),
        ]
        plan = FaultPlanV1.build(entries=entries)
        injector = FaultInjector(plan)

        # Inject once (uses the only allowed injection)
        result = injector.inject(FaultKind.REDIS_FAILURE)
        assert result.fault_injected
        assert injector.plan.entries[0].injection_count == 1

        # Reconstruct from plan — injection count should carry over
        injector2 = FaultInjector(injector.plan)
        with pytest.raises(SandboxFaultError) as exc_info:
            injector2.inject(FaultKind.REDIS_FAILURE)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.FAULT_INJECTION_DISABLED.value

    def test_multiple_injections_count_persists(self) -> None:
        """Multiple injections increment the plan's injection_count."""
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.NODE,
                enabled=True,
                max_injections=3,
            ),
        ]
        plan = FaultPlanV1.build(entries=entries)
        injector = FaultInjector(plan)

        for i in range(3):
            result = injector.inject(FaultKind.GATEWAY_TIMEOUT)
            assert result.fault_injected
            assert injector.plan.entries[0].injection_count == i + 1

        # Fourth attempt should fail
        with pytest.raises(SandboxFaultError):
            injector.inject(FaultKind.GATEWAY_TIMEOUT)

    def test_all_attributions_work(self) -> None:
        for attr in FaultAttribution:
            entries = [
                FaultEntryV1(
                    fault_id=f"fault-{attr.value}",
                    fault_kind=FaultKind.GATEWAY_TIMEOUT,
                    attribution=attr,
                    enabled=True,
                ),
            ]
            plan = FaultPlanV1.build(entries=entries)
            injector = FaultInjector(plan)
            result = injector.inject(FaultKind.GATEWAY_TIMEOUT)
            assert result.attribution == attr

    def test_nonexistent_fault_kind_as_string_raises(self) -> None:
        injector = FaultInjector()
        with pytest.raises(SandboxFaultError) as exc_info:
            # This should fail because the kind won't match any entry
            # and the fault_kind won't be in the plan
            injector.inject(FaultKind.POSTGRESQL_TRANSIENT)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.FAULT_INJECTION_DISABLED.value

    def test_fault_result_digest(self) -> None:
        entries = [
            FaultEntryV1(
                fault_id="fault-1",
                fault_kind=FaultKind.GATEWAY_TIMEOUT,
                attribution=FaultAttribution.MODEL,
                enabled=True,
            ),
        ]
        plan = FaultPlanV1.build(entries=entries)
        injector = FaultInjector(plan)
        result = injector.inject(FaultKind.GATEWAY_TIMEOUT)
        assert len(result.result_digest) == 64


# ===================================================================
# RecoverySession Tests
# ===================================================================


class TestRecoverySession:
    def test_resume_first_time_succeeds(self) -> None:
        session = RecoverySession()
        result = session.resume(
            "session-1",
            "resume-1",
            expected_state_version=1,
        )
        assert result

    def test_duplicate_resume_rejected(self) -> None:
        session_v1 = RecoverySessionV1.build(
            sessions=[
                RecoveryStateV1(
                    session_id="session-1",
                    resume_id="resume-1",
                    state_version=1,
                    retry_count=0,
                    deadline_at=datetime.now(UTC) + timedelta(hours=1),
                    used=True,
                    succeeded=False,
                ),
            ]
        )
        session = RecoverySession(session_v1)
        with pytest.raises(SandboxFaultError) as exc_info:
            session.resume(
                "session-1",
                "resume-1",
                expected_state_version=1,
            )
        assert exc_info.value.args[0] == SandboxFaultFailureCode.RECOVERY_RESUME_ALREADY_USED.value

    def test_state_version_conflict(self) -> None:
        session_v1 = RecoverySessionV1.build(
            sessions=[
                RecoveryStateV1(
                    session_id="session-1",
                    resume_id="resume-1",
                    state_version=2,
                    retry_count=0,
                    deadline_at=datetime.now(UTC) + timedelta(hours=1),
                    used=False,
                    succeeded=False,
                ),
            ]
        )
        session = RecoverySession(session_v1)
        with pytest.raises(SandboxFaultError) as exc_info:
            session.resume(
                "session-1",
                "resume-1",
                expected_state_version=1,  # mismatch with stored version=2
            )
        assert exc_info.value.args[0] == SandboxFaultFailureCode.RECOVERY_STATE_VERSION_CONFLICT.value

    def test_deadline_exceeded(self) -> None:
        session_v1 = RecoverySessionV1.build(
            sessions=[
                RecoveryStateV1(
                    session_id="session-1",
                    resume_id="resume-1",
                    state_version=1,
                    retry_count=0,
                    deadline_at=datetime.now(UTC) - timedelta(seconds=1),
                    used=False,
                    succeeded=False,
                ),
            ]
        )
        session = RecoverySession(session_v1)
        with pytest.raises(SandboxFaultError) as exc_info:
            session.resume(
                "session-1",
                "resume-1",
                expected_state_version=1,
            )
        assert exc_info.value.args[0] == SandboxFaultFailureCode.RECOVERY_DEADLINE_EXCEEDED.value

    def test_retries_exceeded(self) -> None:
        session_v1 = RecoverySessionV1.build(
            sessions=[
                RecoveryStateV1(
                    session_id="session-1",
                    resume_id="resume-1",
                    state_version=1,
                    retry_count=10,
                    deadline_at=datetime.now(UTC) + timedelta(hours=1),
                    used=False,
                    succeeded=False,
                ),
            ]
        )
        session = RecoverySession(session_v1)
        with pytest.raises(SandboxFaultError) as exc_info:
            session.resume(
                "session-1",
                "resume-1",
                expected_state_version=1,
                max_retries=5,
            )
        assert exc_info.value.args[0] == SandboxFaultFailureCode.RECOVERY_RETRIES_EXCEEDED.value

    def test_no_existing_session_creates_one(self) -> None:
        session = RecoverySession()
        # First call has no state — should succeed
        result = session.resume("session-new", "resume-new", expected_state_version=1)
        assert result
        assert session.sessions.sessions[0].used
        with pytest.raises(SandboxFaultError) as exc_info:
            session.resume("session-new", "resume-new", expected_state_version=1)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.RECOVERY_RESUME_ALREADY_USED.value

    def test_recovery_session_digest(self) -> None:
        states = [
            RecoveryStateV1(
                session_id="session-1",
                resume_id="resume-1",
                state_version=1,
                retry_count=0,
                deadline_at=datetime.now(UTC) + timedelta(hours=1),
                used=True,
                succeeded=True,
            ),
        ]
        session_v1 = RecoverySessionV1.build(sessions=states)
        assert len(session_v1.session_digest) == 64


# ===================================================================
# IdempotentSideEffectLedger Tests
# ===================================================================


class TestIdempotentSideEffectLedger:
    def test_record_new_effect_succeeds(self) -> None:
        ledger = IdempotentSideEffectLedger()
        result = ledger.record("effect-1", "key-1", {"action": "test"})
        assert result  # True means recorded

    def test_same_key_same_data_is_idempotent(self) -> None:
        ledger = IdempotentSideEffectLedger()
        ledger.record("effect-1", "key-1", {"action": "test"})
        result = ledger.record("effect-2", "key-1", {"action": "test"})
        # Same key + same data should be idempotent (returns False = no-op)
        assert not result

    def test_same_key_different_data_raises(self) -> None:
        ledger = IdempotentSideEffectLedger()
        ledger.record("effect-1", "key-1", {"action": "test"})
        with pytest.raises(SandboxFaultError) as exc_info:
            ledger.record("effect-2", "key-1", {"action": "different"})
        assert exc_info.value.args[0] == SandboxFaultFailureCode.SIDE_EFFECT_DUPLICATE.value

    def test_is_replayed_returns_false_for_new_key(self) -> None:
        ledger = IdempotentSideEffectLedger()
        assert not ledger.is_replayed("nonexistent-key")

    def test_is_replayed_returns_true_after_record(self) -> None:
        ledger = IdempotentSideEffectLedger()
        ledger.record("effect-1", "key-1", {"action": "test"})
        assert ledger.is_replayed("key-1")

    def test_multiple_independent_keys(self) -> None:
        ledger = IdempotentSideEffectLedger()
        assert ledger.record("effect-1", "key-1", {"action": "a"})
        assert ledger.record("effect-2", "key-2", {"action": "b"})
        assert not ledger.record("effect-3", "key-1", {"action": "a"})

    def test_ledger_property_updated_after_record(self) -> None:
        """ledger.ledger reflects entries after record()."""
        ledger = IdempotentSideEffectLedger()
        assert len(ledger.ledger.entries) == 0

        ledger.record("effect-1", "key-1", {"action": "test"})
        assert len(ledger.ledger.entries) == 1
        assert ledger.ledger.entries[0].effect_id == "effect-1"
        assert ledger.ledger.entries[0].completed

        ledger.record("effect-2", "key-2", {"action": "other"})
        assert len(ledger.ledger.entries) == 2

    def test_ledger_reconstruction_preserves_entries(self) -> None:
        """Reconstructing from ledger.ledger preserves duplicate protection."""
        ledger = IdempotentSideEffectLedger()
        ledger.record("effect-1", "key-1", {"action": "test"})

        # Reconstruct from exposed ledger
        ledger2 = IdempotentSideEffectLedger(ledger.ledger)
        assert ledger2.is_replayed("key-1")

        # Same key + same data should still be idempotent
        assert not ledger2.record("effect-2", "key-1", {"action": "test"})

    def test_ledger_reconstruction_rejects_duplicate(self) -> None:
        """Reconstructing from ledger.ledger preserves conflict detection."""
        ledger = IdempotentSideEffectLedger()
        ledger.record("effect-1", "key-1", {"action": "test"})

        ledger2 = IdempotentSideEffectLedger(ledger.ledger)
        with pytest.raises(SandboxFaultError) as exc_info:
            ledger2.record("effect-2", "key-1", {"action": "different"})
        assert exc_info.value.args[0] == SandboxFaultFailureCode.SIDE_EFFECT_DUPLICATE.value


# ===================================================================
# CheckpointRestoreGuard Tests
# ===================================================================


class TestCheckpointRestoreGuard:
    def test_valid_snapshot_passes(self) -> None:
        guard = CheckpointRestoreGuard()
        data = json.dumps({"state": "valid", "version": 1}, separators=(",", ":"))
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        snapshot = CheckpointSnapshotV1(data=data, digest=digest)
        assert guard.validate(snapshot)

    def test_tampered_snapshot_fails(self) -> None:
        guard = CheckpointRestoreGuard()
        data = json.dumps({"state": "valid"}, separators=(",", ":"))
        snapshot = CheckpointSnapshotV1(data=data, digest="0" * 64)
        with pytest.raises(SandboxFaultError) as exc_info:
            guard.validate(snapshot)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID.value

    def test_restore_returns_data(self) -> None:
        guard = CheckpointRestoreGuard()
        data = json.dumps({"state": "valid", "version": 1}, separators=(",", ":"))
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        snapshot = CheckpointSnapshotV1(data=data, digest=digest)
        result = guard.restore(snapshot)
        assert result == {"state": "valid", "version": 1}

    def test_restore_tampered_raises(self) -> None:
        guard = CheckpointRestoreGuard()
        snapshot = CheckpointSnapshotV1(data="not json", digest="0" * 64)
        with pytest.raises(SandboxFaultError) as exc_info:
            guard.restore(snapshot)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID.value

    def test_non_dict_json_fails(self) -> None:
        guard = CheckpointRestoreGuard()
        data = json.dumps(["list", "not", "dict"], separators=(",", ":"))
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        snapshot = CheckpointSnapshotV1(data=data, digest=digest)
        with pytest.raises(SandboxFaultError) as exc_info:
            guard.validate(snapshot)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID.value

    def test_empty_data_fails(self) -> None:
        guard = CheckpointRestoreGuard()
        data = ""
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        snapshot = CheckpointSnapshotV1(data=data, digest=digest)
        with pytest.raises(SandboxFaultError):
            guard.restore(snapshot)

    def test_schema_version(self) -> None:
        snapshot = CheckpointSnapshotV1(
            data="{}",
            digest=hashlib.sha256(b"{}").hexdigest(),
        )
        assert snapshot.schema_version == "sandbox-checkpoint-restore-guard.v1"

    def test_rejects_non_canonical_whitespace(self) -> None:
        """JSON with whitespace must be rejected even if digest matches."""
        guard = CheckpointRestoreGuard()
        data = '{"state": "valid", "version": 1}'
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        snapshot = CheckpointSnapshotV1(data=data, digest=digest)
        with pytest.raises(SandboxFaultError) as exc_info:
            guard.validate(snapshot)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID.value

    def test_rejects_reordered_keys(self) -> None:
        """Reordered keys must be rejected — canonical requires sort_keys=True."""
        guard = CheckpointRestoreGuard()
        data = '{"version":1,"state":"valid"}'
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        snapshot = CheckpointSnapshotV1(data=data, digest=digest)
        with pytest.raises(SandboxFaultError) as exc_info:
            guard.validate(snapshot)
        assert exc_info.value.args[0] == SandboxFaultFailureCode.CHECKPOINT_SNAPSHOT_INVALID.value

    def test_accepts_canonical_json(self) -> None:
        """Properly canonical JSON with sorted keys and compact separators passes."""
        guard = CheckpointRestoreGuard()
        parsed = {"state": "valid", "version": 1}
        data = json.dumps(parsed, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        snapshot = CheckpointSnapshotV1(data=data, digest=digest)
        assert guard.validate(snapshot)


# ===================================================================
# RecoverySession Persistence Tests
# ===================================================================


class TestRecoverySessionPersistence:
    """RecoverySession must persist state after first resume and
    consume/version-increment existing sessions."""

    def test_state_persisted_after_first_resume(self) -> None:
        session = RecoverySession()
        session.resume("session-p", "resume-1", expected_state_version=1)
        assert len(session.sessions.sessions) == 1
        stored = session.sessions.sessions[0]
        assert stored.session_id == "session-p"
        assert stored.used
        assert stored.state_version == 1
        assert stored.resume_id == "resume-1"

    def test_state_version_incremented_on_resume(self) -> None:
        """Resuming an existing session with used=False increments version."""
        initial = RecoveryStateV1(
            session_id="session-v",
            resume_id="resume-1",
            state_version=1,
            retry_count=0,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            used=False,
            succeeded=False,
        )
        session = RecoverySession(RecoverySessionV1.build(sessions=[initial]))

        result = session.resume(
            "session-v",
            "resume-2",
            expected_state_version=1,
        )
        assert result
        stored = session.sessions.sessions[0]
        assert stored.state_version == 2
        assert stored.retry_count == 1
        assert stored.used

    def test_session_digest_changes_after_resume(self) -> None:
        """Session digest must differ after state change."""
        initial = RecoveryStateV1(
            session_id="session-d",
            resume_id="resume-1",
            state_version=1,
            retry_count=0,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            used=False,
            succeeded=False,
        )
        session = RecoverySession(RecoverySessionV1.build(sessions=[initial]))
        original_digest = session.sessions.session_digest

        session.resume("session-d", "resume-2", expected_state_version=1)
        assert session.sessions.session_digest != original_digest

    def test_reconstruction_preserves_session_state(self) -> None:
        """RecoverySession reconstructed from V1 preserves consumed state."""
        initial = RecoveryStateV1(
            session_id="session-r",
            resume_id="resume-1",
            state_version=1,
            retry_count=0,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            used=False,
            succeeded=False,
        )
        v1 = RecoverySessionV1.build(sessions=[initial])
        session = RecoverySession(v1)
        session.resume("session-r", "resume-2", expected_state_version=1)

        # Reconstruct from sessions property
        session2 = RecoverySession(session.sessions)
        stored = session2.sessions.sessions[0]
        assert stored.state_version == 2
        assert stored.used
        assert stored.retry_count == 1


# ===================================================================
# SandboxFaultAdapter Tests
# ===================================================================


class TestSandboxFaultAdapter:
    def test_adapter_holds_components(self) -> None:
        injector = FaultInjector()
        recovery = RecoverySession()
        ledger = IdempotentSideEffectLedger()
        checkpoint = CheckpointRestoreGuard()

        adapter = SandboxFaultAdapter(
            injector=injector,
            recovery=recovery,
            ledger=ledger,
            checkpoint=checkpoint,
        )
        assert adapter.injector is injector
        assert adapter.recovery is recovery
        assert adapter.ledger is ledger
        assert adapter.checkpoint is checkpoint


# ===================================================================
# Schema / Contract Tests
# ===================================================================


class TestSchemaContract:
    def test_canonical_json_is_stable(self) -> None:
        data = {"z": 3, "a": 1, "m": 2}
        encoded = canonical_json_bytes(data)
        assert encoded == b'{"a":1,"m":2,"z":3}'

    def test_error_code_values_are_stable(self) -> None:
        codes = {c.value for c in SandboxFaultFailureCode}
        assert "SANDBOX_FAULT_INJECTION_DISABLED" in codes
        assert "SANDBOX_FAULT_KIND_NOT_RECOGNIZED" in codes
        assert "SANDBOX_RECOVERY_RETRIES_EXCEEDED" in codes
        assert "SANDBOX_RECOVERY_STATE_VERSION_CONFLICT" in codes
        assert "SANDBOX_SIDE_EFFECT_DUPLICATE" in codes
        assert "SANDBOX_CHECKPOINT_SNAPSHOT_INVALID" in codes

    def test_fault_kind_all_covered(self) -> None:
        """All seven required fault kinds are present."""
        kinds = list(FaultKind)
        expected_kinds = [
            "gateway_timeout",
            "rag_unavailable",
            "postgresql_transient",
            "redis_failure",
            "checkpoint_failure",
            "duplicate_resume",
            "state_conflict",
        ]
        for ek in expected_kinds:
            assert any(k.value == ek for k in kinds)

    def test_fault_attribution_all_covered(self) -> None:
        """All six attribution domains are present."""
        attributions = list(FaultAttribution)
        expected = ["node", "model", "tool", "policy", "verifier", "persistence"]
        for ea in expected:
            assert any(a.value == ea for a in attributions)

    def test_schema_versions_match(self) -> None:
        from app.agent_runtime.sandbox_faults import (
            SANDBOX_CHECKPOINT_RESTORE_GUARD_SCHEMA_VERSION,
            SANDBOX_FAULT_PLAN_SCHEMA_VERSION,
            SANDBOX_FAULT_RESULT_SCHEMA_VERSION,
            SANDBOX_RECOVERY_SESSION_SCHEMA_VERSION,
            SANDBOX_SIDE_EFFECT_LEDGER_SCHEMA_VERSION,
        )

        assert SANDBOX_FAULT_PLAN_SCHEMA_VERSION == "sandbox-fault-plan.v1"
        assert SANDBOX_RECOVERY_SESSION_SCHEMA_VERSION == "sandbox-recovery-session.v1"
        assert SANDBOX_SIDE_EFFECT_LEDGER_SCHEMA_VERSION == "sandbox-side-effect-ledger.v1"
        assert SANDBOX_CHECKPOINT_RESTORE_GUARD_SCHEMA_VERSION == "sandbox-checkpoint-restore-guard.v1"
        assert SANDBOX_FAULT_RESULT_SCHEMA_VERSION == "sandbox-fault-result.v1"
