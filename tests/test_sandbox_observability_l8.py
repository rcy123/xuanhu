"""L8-1: 专项测试 — Episode Package、Metrics、业务事件和失败归因。

测试覆盖：
- 数据模型冻结、严格、闭集类型枚举
- append-only store 的 idempotency 规则
- canonical snapshot / restore 与篡改检测
- 固定名称/标签指标提取
- 敏感内容预防（exception stack、临床文本、身份字段）
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.agent_runtime.sandbox_observability import (
    METRIC_LABELS,
    METRIC_NAMES,
    OBSERVABILITY_SCHEMA_VERSION,
    BusinessEventV1,
    EpisodeNotFound,
    EpisodePackageV1,
    EpisodeStore,
    FailureAttributionV1,
    IdempotencyConflict,
    ModelUsageV1,
    NodeTrajectoryEventV1,
    TrajectoryEventType,
    _canonical_bytes,
    extract_episode_metrics,
)


@pytest.fixture(autouse=True)
def _allow_request_local_langgraph_test_runtime() -> None:
    """Override global conftest fixture to avoid importing missing langgraph."""
    return


# ============================================================================
# 1. 数据模型：冻结、严格、JSON-safe
# ============================================================================


class TestEpisodePackageV1:
    def test_default_construction(self) -> None:
        """Minimal construction produces a valid package with defaults."""
        pkg = EpisodePackageV1()
        assert pkg.schema_version == OBSERVABILITY_SCHEMA_VERSION
        assert pkg.episode_id and isinstance(pkg.episode_id, str)
        assert pkg.created_at and isinstance(pkg.created_at, str)
        assert pkg.trajectory == ()
        assert pkg.failure is None
        assert pkg.business_events == ()
        assert pkg.model_usage == ModelUsageV1()
        assert len(pkg.model_actual) == 0

    def test_full_construction(self) -> None:
        """Construction with all fields populated."""
        pkg = EpisodePackageV1(
            episode_id="ep-1",
            state_hash="abc123",
            graph_version="g1",
            agent_spec_version="as1",
            prompt_version="pv1",
            policy_version="pol1",
            model_actual="gpt-4",
            model_usage=ModelUsageV1(
                model_name="gpt-4",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                call_count=2,
            ),
            trajectory=(
                NodeTrajectoryEventV1(event_type=TrajectoryEventType.NODE_STARTED, node_name="a"),
                NodeTrajectoryEventV1(event_type=TrajectoryEventType.NODE_COMPLETED, node_name="a"),
            ),
            failure=FailureAttributionV1(
                failure_type="timeout",
                component="model",
                error_code="MODEL_TIMEOUT",
            ),
            business_events=(
                BusinessEventV1(
                    event_type="gate.passed",
                    reference_type="gate",
                    reference_id="g-1",
                ),
            ),
            evidence_refs=("ev-1",),
            verification_refs=("vf-1",),
            gate_refs=("gt-1",),
            intervention_refs=("hi-1",),
        )
        assert pkg.episode_id == "ep-1"
        assert pkg.state_hash == "abc123"
        assert len(pkg.trajectory) == 2
        assert pkg.failure is not None
        assert pkg.failure.error_code == "MODEL_TIMEOUT"
        assert pkg.evidence_refs == ("ev-1",)

    def test_frozen(self) -> None:
        """EpisodePackageV1 is immutable after construction."""
        pkg = EpisodePackageV1()
        with pytest.raises(ValidationError):
            pkg.episode_id = "changed"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        """Extraneous fields are rejected."""
        with pytest.raises(ValidationError):
            EpisodePackageV1(unknown_field="x")  # type: ignore[arg-type]

    def test_json_roundtrip(self) -> None:
        """EpisodePackageV1 survives JSON serialisation + deserialisation."""
        pkg = EpisodePackageV1(
            episode_id="rt-1",
            state_hash="xyz",
            trajectory=(
                NodeTrajectoryEventV1(
                    event_type=TrajectoryEventType.GRAPH_COMPLETED,
                    node_name="",
                ),
            ),
        )
        raw = _canonical_bytes(pkg)
        restored = EpisodePackageV1.model_validate_json(raw)
        assert restored.episode_id == pkg.episode_id
        assert restored.state_hash == pkg.state_hash
        assert len(restored.trajectory) == 1


# ============================================================================
# 2. 事件类型闭集
# ============================================================================


class TestTrajectoryEventType:
    def test_all_members_are_defined(self) -> None:
        """The closed set of event types matches spec §6."""
        expected = frozenset(
            {
                "node.started",
                "node.completed",
                "gate.failed",
                "interrupt.required",
                "graph.completed",
                "graph.failed",
            }
        )
        actual = frozenset(t.value for t in TrajectoryEventType)
        assert actual == expected

    def test_rejects_arbitrary_string(self) -> None:
        """Arbitrary event_type strings are rejected by the enum."""
        with pytest.raises(ValidationError):
            NodeTrajectoryEventV1(event_type="custom.event")  # type: ignore[arg-type]


class TestNodeTrajectoryEventV1:
    @pytest.mark.parametrize("etype", list(TrajectoryEventType))
    def test_all_types_construct(self, etype: TrajectoryEventType) -> None:
        """Every trajectory event type can be constructed."""
        evt = NodeTrajectoryEventV1(event_type=etype, node_name="test")
        assert evt.event_type == etype
        assert evt.event_id and isinstance(evt.event_id, str)

    def test_metadata_rejects_exception_stack(self) -> None:
        """metadata containing exception-stack patterns is rejected."""
        pattern = 'Traceback (most recent call last):\n  File "foo.py", line 10, in bar'
        with pytest.raises(ValidationError, match="exception stack"):
            NodeTrajectoryEventV1(
                event_type=TrajectoryEventType.NODE_STARTED,
                node_name="n",
                metadata={"error": pattern},
            )

    def test_node_name_rejects_clinical_text(self) -> None:
        """node_name containing clinical keywords is rejected."""
        with pytest.raises(ValidationError, match="clinical"):
            NodeTrajectoryEventV1(
                event_type=TrajectoryEventType.NODE_STARTED,
                node_name="diagnosis_analysis",
            )


# ============================================================================
# 3. ModelUsageV1
# ============================================================================


class TestModelUsageV1:
    def test_default_zero(self) -> None:
        usage = ModelUsageV1()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_tokens == 0
        assert usage.call_count == 0

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelUsageV1(input_tokens=-1)

    def test_frozen(self) -> None:
        usage = ModelUsageV1(input_tokens=10)
        with pytest.raises(ValidationError):
            usage.input_tokens = 20  # type: ignore[misc]


# ============================================================================
# 4. FailureAttributionV1
# ============================================================================


class TestFailureAttributionV1:
    def test_required_fields(self) -> None:
        f = FailureAttributionV1(failure_type="err", component="mod", error_code="E1")
        assert f.failure_type == "err"
        assert f.component == "mod"
        assert f.error_code == "E1"
        assert f.details == {}

    def test_empty_failure_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FailureAttributionV1(failure_type="", component="mod", error_code="E1")

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            FailureAttributionV1(
                failure_type="err",
                component="mod",
                error_code="E1",
                raw_stack="...",  # type: ignore[arg-type]
            )


# ============================================================================
# 5. BusinessEventV1
# ============================================================================


class TestBusinessEventV1:
    @pytest.mark.parametrize(
        "ref_type",
        [
            "evidence",
            "verification",
            "gate",
            "human-intervention",
        ],
    )
    def test_valid_reference_types(self, ref_type: str) -> None:
        evt = BusinessEventV1(
            event_type="some.event",
            reference_type=ref_type,
            reference_id="ref-1",
        )
        assert evt.reference_type == ref_type

    def test_invalid_reference_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BusinessEventV1(
                event_type="some.event",
                reference_type="unknown",
                reference_id="ref-1",
            )


# ============================================================================
# 6. EpisodeStore — 基础操作
# ============================================================================


class TestEpisodeStoreBasic:
    def test_store_and_retrieve(self) -> None:
        store = EpisodeStore()
        pkg = EpisodePackageV1(episode_id="e-1")
        store.put(pkg)
        assert store.get("e-1").episode_id == "e-1"
        assert len(store) == 1

    def test_get_missing_raises(self) -> None:
        store = EpisodeStore()
        with pytest.raises(EpisodeNotFound):
            store.get("nonexistent")

    def test_get_by_key(self) -> None:
        store = EpisodeStore()
        pkg = EpisodePackageV1(episode_id="e-1")
        store.put(pkg, storage_key="key-1")
        retrieved = store.get_by_key("key-1")
        assert retrieved.episode_id == "e-1"

    def test_get_by_key_missing_raises(self) -> None:
        store = EpisodeStore()
        with pytest.raises(EpisodeNotFound):
            store.get_by_key("unknown-key")

    def test_list_ids(self) -> None:
        store = EpisodeStore()
        store.put(EpisodePackageV1(episode_id="b"))
        store.put(EpisodePackageV1(episode_id="a"))
        assert store.list_ids() == ("a", "b")

    def test_contains(self) -> None:
        store = EpisodeStore()
        store.put(EpisodePackageV1(episode_id="e-1"))
        assert "e-1" in store
        assert "e-2" not in store


# ============================================================================
# 7. 幂等性规则
# ============================================================================


class TestEpisodeStoreIdempotency:
    def test_same_key_same_bytes_accepted(self) -> None:
        """Same key + same canonical bytes → no-op (no error)."""
        store = EpisodeStore()
        pkg = EpisodePackageV1(episode_id="e-1")
        store.put(pkg, storage_key="k")
        store.put(pkg, storage_key="k")  # should not raise
        assert len(store) == 1

    def test_same_key_diff_bytes_rejected(self) -> None:
        """Same key + different canonical bytes → IdempotencyConflict."""
        store = EpisodeStore()
        store.put(
            EpisodePackageV1(episode_id="e-1", state_hash="h1"),
            storage_key="k",
        )
        with pytest.raises(IdempotencyConflict):
            store.put(
                EpisodePackageV1(episode_id="e-1", state_hash="h2"),
                storage_key="k",
            )

    def test_no_key_no_idempotency(self) -> None:
        """Without storage_key, duplicate episode_id is fine (append-only)."""
        store = EpisodeStore()
        pkg1 = EpisodePackageV1(episode_id="e-1", state_hash="h1")
        pkg2 = EpisodePackageV1(episode_id="e-1", state_hash="h2")
        store.put(pkg1)
        store.put(pkg2)  # no key → no idempotency check; append-only
        assert len(store) == 2

    def test_different_keys_independent(self) -> None:
        """Different storage keys map to different episodes."""
        store = EpisodeStore()
        store.put(
            EpisodePackageV1(episode_id="e-1"),
            storage_key="k1",
        )
        store.put(
            EpisodePackageV1(episode_id="e-2"),
            storage_key="k2",
        )
        assert len(store) == 2
        assert store.get_by_key("k1").episode_id == "e-1"
        assert store.get_by_key("k2").episode_id == "e-2"


# ============================================================================
# 8. Snapshot / Restore — canonical、可重放、篡改拒否
# ============================================================================


class TestEpisodeStoreSnapshot:
    def test_snapshot_roundtrip(self) -> None:
        """Snapshot → restore reproduces the same store."""
        store = EpisodeStore()
        store.put(
            EpisodePackageV1(
                episode_id="e-1",
                state_hash="h1",
                trajectory=(
                    NodeTrajectoryEventV1(
                        event_type=TrajectoryEventType.NODE_STARTED,
                        node_name="a",
                    ),
                ),
            ),
        )
        store.put(EpisodePackageV1(episode_id="e-2", state_hash="h2"))

        snap = store.snapshot()
        restored = EpisodeStore()
        restored.restore(snap)

        assert len(restored) == 2
        assert restored.get("e-1").state_hash == "h1"
        assert restored.get("e-2").state_hash == "h2"

    def test_snapshot_replayable(self) -> None:
        """Same packages produce identical snapshot bytes."""
        s1 = EpisodeStore()
        s2 = EpisodeStore()
        pkg = EpisodePackageV1(episode_id="e-1", state_hash="x")
        s1.put(pkg)
        s2.put(pkg)
        assert s1.snapshot() == s2.snapshot()

    def test_restore_rejects_tampered_bytes(self) -> None:
        """Flipping a single byte in the snapshot triggers chain-hash mismatch."""
        store = EpisodeStore()
        store.put(EpisodePackageV1(episode_id="e-1"))
        snap = bytearray(store.snapshot())
        # Flip one bit in the payload area.
        snap[len(snap) // 2] ^= 0x01
        with pytest.raises(ValueError, match="chain hash mismatch|invalid"):
            store.restore(bytes(snap))

    def test_restore_rejects_modified_entry(self) -> None:
        """Modifying the canonical_hex of an entry triggers chain failure."""
        store = EpisodeStore()
        store.put(EpisodePackageV1(episode_id="e-1"))
        data = json.loads(store.snapshot().decode("utf-8"))
        # Flip one hex char in the first entry's canonical_hex.
        entry = data["entries"][0]
        old = entry["canonical_hex"]
        flipped = hex(int(old[0], 16) ^ 0xF)[2:] + old[1:]
        entry["canonical_hex"] = flipped
        tampered = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with pytest.raises(ValueError, match="chain hash mismatch"):
            store.restore(tampered)

    def test_restore_rejects_wrong_schema(self) -> None:
        """Snapshot with wrong schema version is rejected."""
        data = {
            "schema": "wrong-version",
            "entries": [],
            "chain_hash": "0" * 64,
        }
        snap = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with pytest.raises(ValueError, match="schema version mismatch"):
            EpisodeStore().restore(snap)

    def test_restore_rejects_malformed_json(self) -> None:
        """Non-JSON snapshot is rejected."""
        with pytest.raises(ValueError, match="invalid snapshot"):
            EpisodeStore().restore(b"not json")

    def test_restore_empties_store_then_replaces(self) -> None:
        """Restore into a non-empty store replaces all contents."""
        store = EpisodeStore()
        store.put(EpisodePackageV1(episode_id="old"))
        snap = EpisodeStore().snapshot()  # empty-store snapshot
        store.restore(snap)
        assert len(store) == 0

    def test_restore_rebuilds_storage_key_index(self) -> None:
        store = EpisodeStore()
        package = EpisodePackageV1(episode_id="keyed")
        store.put(package, storage_key="storage-key")
        restored = EpisodeStore()
        restored.restore(store.snapshot())
        assert restored.get_by_key("storage-key").episode_id == "keyed"

    def test_restore_validates_all_entries_before_mutation(self) -> None:
        """Corrupted data in entry N does not mutate prior entries."""
        store = EpisodeStore()
        store.put(EpisodePackageV1(episode_id="e-1"))

        snap_data = json.loads(store.snapshot().decode("utf-8"))
        # Append a malformed entry.
        snap_data["entries"].append(
            {
                "record_key": "bad",
                "episode_id": "bad",
                "canonical_hex": b"not-json".hex(),
            }
        )
        # Recompute chain with the bad entry.
        chain = b""
        for entry in snap_data["entries"]:
            raw = bytes.fromhex(entry["canonical_hex"])
            chain = hashlib.sha256(chain + raw).digest()
        snap_data["chain_hash"] = chain.hex()
        tampered = json.dumps(snap_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with pytest.raises(ValueError, match="payload is invalid"):
            store.restore(tampered)
        # Original store should be untouched.
        assert len(store) == 1


# ============================================================================
# 9. 指标 — 固定名称 / 固定标签 / 无动态 label
# ============================================================================


class TestEpisodeMetrics:
    def test_metric_names_are_fixed(self) -> None:
        """extract_episode_metrics only emits keys that belong to METRIC_NAMES
        or the fixed-label dimension keys."""
        pkg = EpisodePackageV1(
            model_usage=ModelUsageV1(input_tokens=10, output_tokens=5, total_tokens=15, call_count=1),
            trajectory=(NodeTrajectoryEventV1(event_type=TrajectoryEventType.NODE_STARTED, node_name="a"),),
            business_events=(BusinessEventV1(event_type="e", reference_type="gate", reference_id="g-1"),),
            failure=FailureAttributionV1(failure_type="err", component="mod", error_code="E1"),
            evidence_refs=("ev-1",),
            verification_refs=("vf-1",),
            gate_refs=("gt-1",),
            intervention_refs=("hi-1",),
        )
        metrics = extract_episode_metrics(pkg)

        # Every non-label key must be in METRIC_NAMES.
        for key, value in metrics.items():
            if "label_" not in key:
                assert key in METRIC_NAMES, f"{key} not in METRIC_NAMES"
            assert isinstance(value, int | float), f"{key} is not numeric"

        # Spot-check values.
        assert metrics["episode.total"] == 1
        assert metrics["episode.trajectory_events"] == 1
        assert metrics["episode.business_events"] == 1
        assert metrics["episode.model_input_tokens"] == 10
        assert metrics["episode.model_output_tokens"] == 5
        assert metrics["episode.model_total_tokens"] == 15
        assert metrics["episode.model_call_count"] == 1
        assert metrics["episode.failure_count"] == 1
        assert metrics["episode.evidence_refs"] == 1
        assert metrics["episode.verification_refs"] == 1
        assert metrics["episode.gate_refs"] == 1
        assert metrics["episode.intervention_refs"] == 1

    def test_metric_labels_are_fixed(self) -> None:
        """Label dimension keys only use METRIC_LABELS; no arbitrary labels."""
        pkg = EpisodePackageV1(
            graph_version="g1",
            agent_spec_version="as1",
        )
        metrics = extract_episode_metrics(pkg)
        label_keys = {k for k in metrics if "label_" in k}
        assert len(label_keys) == len(METRIC_LABELS)
        for lk in label_keys:
            # Extract label name from key like "episode.total:label_schema_version=..."
            label_name = lk.split("label_")[1].split("=")[0]
            assert label_name in METRIC_LABELS, f"{label_name} not in METRIC_LABELS"

    def test_metric_label_values_are_sanitized(self) -> None:
        pkg = EpisodePackageV1(
            graph_version="g1\nbad",
            agent_spec_version="as:1",
            prompt_version="pv=1",
            policy_version="pol\t1",
        )
        metrics = extract_episode_metrics(pkg)
        assert "episode.total:label_graph_version=g1_bad" in metrics
        assert "episode.total:label_agent_spec_version=as_1" in metrics
        assert "episode.total:label_prompt_version=pv_1" in metrics
        assert "episode.total:label_policy_version=pol_1" in metrics
        for key in metrics:
            assert "\n" not in key
            assert "\r" not in key

    def test_no_failure_metrics(self) -> None:
        """Package without failure yields failure_count=0 and has_failure=false."""
        pkg = EpisodePackageV1()
        metrics = extract_episode_metrics(pkg)
        assert metrics["episode.failure_count"] == 0
        assert metrics.get("episode.total:label_has_failure=false") == 1

    def test_failure_metrics(self) -> None:
        """Package with failure yields failure_count=1 and has_failure=true."""
        pkg = EpisodePackageV1(
            failure=FailureAttributionV1(
                failure_type="err",
                component="mod",
                error_code="E1",
            ),
        )
        metrics = extract_episode_metrics(pkg)
        assert metrics["episode.failure_count"] == 1
        assert metrics.get("episode.total:label_has_failure=true") == 1


# ============================================================================
# 10. 敏感内容预防
# ============================================================================


class TestSensitiveContentPrevention:
    def test_no_raw_prompt_in_package(self) -> None:
        """EpisodePackageV1 has no field for raw prompts."""
        pkg = EpisodePackageV1()
        assert not hasattr(pkg, "raw_prompt")
        assert not hasattr(pkg, "model_output")

    def test_no_clinical_identity_fields(self) -> None:
        """No field in the package carries clinical or identity data."""
        pkg = EpisodePackageV1()
        sensitive_terms = {"patient_id", "diagnosis", "treatment", "ssn", "name"}
        for field_name in pkg.model_dump():
            for term in sensitive_terms:
                assert term not in field_name.lower(), f"field '{field_name}' contains '{term}'"

    def test_no_exception_stack_in_trajectory(self) -> None:
        """model_validate rejects metadata that looks like a traceback."""
        with pytest.raises(ValidationError, match="exception stack"):
            NodeTrajectoryEventV1(
                event_type=TrajectoryEventType.GATE_FAILED,
                node_name="gate",
                metadata={"detail": 'Traceback (most recent call last):\n  File "x.py", line 1'},
            )

    def test_raw_prompt_and_identity_text_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="prohibited content"):
            NodeTrajectoryEventV1(
                event_type=TrajectoryEventType.NODE_COMPLETED,
                node_name="node",
                metadata={"raw_prompt": "ignore previous instructions"},
            )
        with pytest.raises(ValidationError, match="prohibited content"):
            FailureAttributionV1(
                failure_type="failure",
                component="model",
                error_code="E1",
                details={"patient_id": "synthetic-id"},
            )


# ============================================================================
# 11. 完整场景
# ============================================================================


class TestFullScenario:
    def test_end_to_end_episode_lifecycle(self) -> None:
        """Simulate a complete episode: events → store → snapshot → restore → metrics."""
        store = EpisodeStore()

        # Build episode.
        trajectory = (
            NodeTrajectoryEventV1(
                event_type=TrajectoryEventType.NODE_STARTED,
                node_name="intake",
            ),
            NodeTrajectoryEventV1(
                event_type=TrajectoryEventType.NODE_COMPLETED,
                node_name="intake",
            ),
            NodeTrajectoryEventV1(
                event_type=TrajectoryEventType.GRAPH_COMPLETED,
            ),
        )
        usage = ModelUsageV1(
            model_name="gpt-4",
            input_tokens=250,
            output_tokens=80,
            total_tokens=330,
            call_count=3,
        )
        business = (
            BusinessEventV1(
                event_type="evidence.collected",
                reference_type="evidence",
                reference_id="ev-abc",
            ),
        )
        pkg = EpisodePackageV1(
            episode_id="ep-full",
            state_hash="deadbeef",
            graph_version="v2.1",
            agent_spec_version="as-v3",
            prompt_version="prompt-v1",
            policy_version="pol-v2",
            model_actual="gpt-4-turbo",
            model_usage=usage,
            trajectory=trajectory,
            business_events=business,
            evidence_refs=("ev-abc",),
            verification_refs=("vf-xyz",),
        )

        # Store.
        store.put(pkg, storage_key="full-key")
        assert len(store) == 1

        # Retrieve.
        retrieved = store.get("ep-full")
        assert retrieved.model_usage.input_tokens == 250
        assert len(retrieved.trajectory) == 3

        # Snapshot → restore.
        snap = store.snapshot()
        restored_store = EpisodeStore()
        restored_store.restore(snap)
        assert restored_store.get("ep-full").state_hash == "deadbeef"

        # Metrics.
        metrics = extract_episode_metrics(retrieved)
        assert metrics["episode.total"] == 1
        assert metrics["episode.trajectory_events"] == 3
        assert metrics["episode.model_input_tokens"] == 250
        assert metrics["episode.model_total_tokens"] == 330
        assert metrics["episode.model_call_count"] == 3
        assert metrics["episode.evidence_refs"] == 1
        assert metrics["episode.verification_refs"] == 1
        assert metrics["episode.failure_count"] == 0

        # Idempotency: re-put same package with same key is a no-op.
        store.put(pkg, storage_key="full-key")
        assert len(store) == 1


# ============================================================================
# Helper: import ValidationError from pydantic
# ============================================================================

from pydantic import ValidationError  # noqa: E402 — imported at module level for fixtures
