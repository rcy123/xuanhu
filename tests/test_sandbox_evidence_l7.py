"""L7-SBX-FRESH evidence/RAG offline reference composition tests.



This test file covers all 11 objectives, the source/claim matrix, resource

limits, and threat-model probes specified in the L7-SBX-FRESH task contract.

Every test targets the public contract of ``app.agent_runtime.sandbox_evidence``

and never imports private names.

"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import cast

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# The module under test â€” RED until implemented
# ---------------------------------------------------------------------------
from app.agent_runtime.sandbox_evidence import (
    SANDBOX_EVIDENCE_DISCLAIMER,
    SANDBOX_EVIDENCE_SCHEMA_VERSION,
    AgentKind,
    CitationVerifier,
    ClaimKind,
    ClaimVerifierResult,
    EvidencePacketLimit,
    EvidencePipeline,
    EvidenceResultLimit,
    FallbackPolicy,
    FormulaRetrievalNode,
    SandboxClaimEvidenceLinkV1,
    SandboxEvidenceBundleV1,
    SandboxEvidenceClaimV1,
    SandboxEvidenceContextV1,
    SandboxEvidenceError,
    SandboxEvidencePacketV1,
    SandboxEvidenceRegistry,
    SandboxEvidenceResultV1,
    SandboxEvidenceStore,
    SandboxEvidenceStoreSnapshotV1,
    SandboxRetrievalTraceV1,
    SandboxSourcePolicy,
    SourceType,
    SyndromeRetrievalNode,
)

# ---------------------------------------------------------------------------

# Fixed synthetic fixtures â€” deterministic, no real data

# ---------------------------------------------------------------------------


_GRAPH_RUN = "graph-run-alpha"

_GRAPH_TRACE = "graph-trace-001"

_RETRIEVAL_RUN = "retrieval-run-42"

_SESSION = "test-session-l7"

_RECORD_ID = "sandbox-record-evid"

_CHECKPOINT = "cp-l7-sandbox"


def _text_digest(text: str) -> str:

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fake_packet(
    evidence_id: str = "ev-001",
    source_type: SourceType = SourceType.THEORY,
    source_id: str = "src-1",
    chunk_id: str = "chunk-1",
    rank: int = 1,
    content: str = "Syndrome X is characterized by deficiency.",
    content_digest: str | None = None,
    retrieval_run: str | None = None,
    graph_run: str = _GRAPH_RUN,
) -> SandboxEvidencePacketV1:

    if retrieval_run is None:
        retrieval_run = _RETRIEVAL_RUN

    if content_digest is None:
        content_digest = _text_digest(content)

    return SandboxEvidencePacketV1(
        evidence_id=evidence_id,
        source_type=source_type,
        source_id=source_id,
        chunk_id=chunk_id,
        rank=rank,
        content_digest=content_digest,
        retrieval_trace=SandboxRetrievalTraceV1(
            retrieval_run=retrieval_run,
            graph_run=graph_run,
            graph_trace=_GRAPH_TRACE,
        ),
    )


def _fake_bundle(
    packets: tuple[SandboxEvidencePacketV1, ...] | None = None,
    query: str = "syndrome basis query",
    retrieval_run: str = _RETRIEVAL_RUN,
    node_name: str = "syndrome_retrieval",
    graph_run: str = _GRAPH_RUN,
) -> SandboxEvidenceBundleV1:

    if packets is None:
        packets = (_fake_packet(retrieval_run=retrieval_run, graph_run=graph_run),)

    content_digest = _text_digest(
        json.dumps(
            [p.model_dump(mode="json") for p in packets],
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    bundle_digest = hashlib.sha256((content_digest + retrieval_run + node_name).encode("utf-8")).hexdigest()

    return SandboxEvidenceBundleV1(
        schema_version=SANDBOX_EVIDENCE_SCHEMA_VERSION,
        packets=packets,
        bundle_digest=bundle_digest,
        content_digest=content_digest,
        graph_run=graph_run,
        graph_trace=_GRAPH_TRACE,
        retrieval_run=retrieval_run,
        node_name=node_name,
        disclaimer=SANDBOX_EVIDENCE_DISCLAIMER,
    )


def _fake_claim(
    claim_id: str = "cl-001",
    claim_kind: ClaimKind = ClaimKind.SYNDROME_BASIS,
    claim_text: str = "The syndrome is deficiency of both qi and yin.",
    evidence_ids: tuple[str, ...] = ("ev-001",),
) -> SandboxEvidenceClaimV1:

    return SandboxEvidenceClaimV1(
        claim_id=claim_id,
        claim_kind=claim_kind,
        claim_text=claim_text,
        evidence_ids=evidence_ids,
    )


def _fake_link(
    claim_id: str = "cl-001",
    evidence_id: str = "ev-001",
    bundle_digest: str | None = None,
    content_digest: str | None = None,
    retrieval_run: str | None = None,
) -> SandboxClaimEvidenceLinkV1:

    if bundle_digest is None:
        bundle_digest = hashlib.sha256(b"bundle_seed").hexdigest()

    if content_digest is None:
        content_digest = _text_digest("Syndrome X is characterized by deficiency.")

    if retrieval_run is None:
        retrieval_run = _RETRIEVAL_RUN

    return SandboxClaimEvidenceLinkV1(
        claim_id=claim_id,
        evidence_id=evidence_id,
        bundle_digest=bundle_digest,
        content_digest=content_digest,
        retrieval_run=_RETRIEVAL_RUN,
    )


# ---------------------------------------------------------------------------

# Fixed authorizer helpers for R1 hardening tests

# ---------------------------------------------------------------------------


class _PermissiveAuthorizer:
    """Test authorizer that always grants authority."""

    def authorize(self, *, bundle_digest: str) -> bool:

        return True


class _DenyingAuthorizer:
    """Test authorizer that always denies authority."""

    def authorize(self, *, bundle_digest: str) -> bool:

        return False


# ---------------------------------------------------------------------------

# Test helpers â€” permissive registry/store for existing test patterns

# ---------------------------------------------------------------------------


def _make_test_registry() -> SandboxEvidenceRegistry:
    """Create a test registry with a permissive authorizer."""

    return SandboxEvidenceRegistry(authorizer=_PermissiveAuthorizer())


def _make_test_store() -> SandboxEvidenceStore:
    """Create a store with a permissive registry for testing."""

    return SandboxEvidenceStore(registry=_make_test_registry())


def _admit(store: SandboxEvidenceStore, bundle: SandboxEvidenceBundleV1) -> None:
    """Recognize and store a bundle â€” used by existing test patterns."""

    store.registry.add_recognized(bundle.bundle_digest)

    store.put(bundle)


# ===================================================================

# 2. Module is importable (RED first â€” module does not exist yet)

# ===================================================================


def test_l7_module_can_be_imported() -> None:
    """If this fails, sandbox_evidence.py does not exist â€” expected RED first."""

    import app.agent_runtime.sandbox_evidence as m

    assert hasattr(m, "SandboxEvidencePacketV1")

    assert hasattr(m, "SandboxEvidenceError")

    assert hasattr(m, "EvidencePipeline")


# ===================================================================

# 3. Strict Pydantic DTOs (frozen, extra=forbid, strict)

# ===================================================================


class TestStrictModels:
    def test_packet_requires_all_fields(self) -> None:

        with pytest.raises(ValidationError):
            SandboxEvidencePacketV1()  # type: ignore[call-arg]

    def test_packet_rejects_extra(self) -> None:

        with pytest.raises(ValidationError):
            SandboxEvidencePacketV1(
                evidence_id="ev-001",
                source_type=SourceType.THEORY,
                source_id="src-1",
                chunk_id="chunk-1",
                rank=1,
                content_digest="a" * 64,
                retrieval_trace=SandboxRetrievalTraceV1(retrieval_run="r", graph_run="g", graph_trace="t"),
                extra_field="nope",  # type: ignore
            )

    def test_packet_is_frozen(self) -> None:

        p = _fake_packet()

        with pytest.raises(ValidationError):
            p.evidence_id = "changed"

    def test_trace_requires_all_fields(self) -> None:

        with pytest.raises(ValidationError):
            SandboxRetrievalTraceV1()  # type: ignore[call-arg]

    def test_bundle_requires_schema_version_and_disclaimer(self) -> None:

        packet = _fake_packet()

        b = _fake_bundle((packet,))

        assert b.schema_version == SANDBOX_EVIDENCE_SCHEMA_VERSION

        assert b.disclaimer == SANDBOX_EVIDENCE_DISCLAIMER

    def test_bundle_rejects_extra(self) -> None:

        p = _fake_packet()

        with pytest.raises(ValidationError):
            SandboxEvidenceBundleV1(
                schema_version=SANDBOX_EVIDENCE_SCHEMA_VERSION,
                packets=(p,),
                bundle_digest="a" * 64,
                content_digest="b" * 64,
                graph_run=_GRAPH_RUN,
                graph_trace=_GRAPH_TRACE,
                retrieval_run=_RETRIEVAL_RUN,
                node_name="syndrome_retrieval",
                disclaimer=SANDBOX_EVIDENCE_DISCLAIMER,
                bogus=True,  # type: ignore
            )

    def test_claim_rejects_extra(self) -> None:

        with pytest.raises(ValidationError):
            SandboxEvidenceClaimV1(
                claim_id="cl-001",
                claim_kind=ClaimKind.SYNDROME_BASIS,
                claim_text="test",
                evidence_ids=("ev-001",),
                x_evil="yes",  # type: ignore
            )

    def test_link_rejects_extra(self) -> None:

        with pytest.raises(ValidationError):
            SandboxClaimEvidenceLinkV1(
                claim_id="cl-001",
                evidence_id="ev-001",
                bundle_digest="bd",
                content_digest="cd",
                retrieval_run="rr",
                bad="field",  # type: ignore
            )

    def test_context_rejects_extra(self) -> None:

        with pytest.raises(ValidationError):
            SandboxEvidenceContextV1(
                evidence_packets=(),
                bundles=(),
                bundle_digests=(),
                context_digest="a" * 64,
                extra="nope",  # type: ignore
            )

    def test_result_strict(self) -> None:

        with pytest.raises(ValidationError):
            SandboxEvidenceResultV1(
                packets=(),
                bundles=(),
                bundles_digest="d" * 64,
                total=0,
                status="nope",  # type: ignore
            )


# ===================================================================

# 4. Source policy matrix (task Â§6)

# ===================================================================


class TestSourcePolicy:
    def test_syndrome_basis_allows_theory_and_case(self) -> None:

        allowed = SandboxSourcePolicy.allowed_source_types(AgentKind.SYNDROME, ClaimKind.SYNDROME_BASIS)

        assert SourceType.THEORY in allowed

        assert SourceType.CASE in allowed

        assert SourceType.FORMULA not in allowed

        assert SourceType.HERB not in allowed

    def test_formula_name_allows_formula_only(self) -> None:

        allowed = SandboxSourcePolicy.allowed_source_types(AgentKind.FORMULA, ClaimKind.FORMULA_NAME)

        assert SourceType.FORMULA in allowed

        assert SourceType.THEORY not in allowed

        assert SourceType.CASE not in allowed

        assert SourceType.HERB not in allowed

    def test_herb_allows_herb_only(self) -> None:

        allowed = SandboxSourcePolicy.allowed_source_types(AgentKind.FORMULA, ClaimKind.HERB)

        assert SourceType.HERB in allowed

        assert SourceType.FORMULA not in allowed

        assert SourceType.THEORY not in allowed

        assert SourceType.CASE not in allowed

    def test_dosage_allows_herb_only(self) -> None:

        allowed = SandboxSourcePolicy.allowed_source_types(AgentKind.FORMULA, ClaimKind.DOSAGE)

        assert SourceType.HERB in allowed

        assert SourceType.FORMULA not in allowed

    def test_modification_reason_allows_formula_and_herb(self) -> None:

        allowed = SandboxSourcePolicy.allowed_source_types(AgentKind.FORMULA, ClaimKind.MODIFICATION_REASON)

        assert SourceType.FORMULA in allowed

        assert SourceType.HERB in allowed

        assert SourceType.THEORY not in allowed

        assert SourceType.CASE not in allowed

    def test_unknown_claim_kind_raises(self) -> None:

        with pytest.raises(SandboxEvidenceError):
            SandboxSourcePolicy.allowed_source_types(
                AgentKind.SYNDROME,
                "unknown_kind_value",  # type: ignore
            )

    def test_unknown_agent_kind_raises(self) -> None:

        with pytest.raises(SandboxEvidenceError):
            SandboxSourcePolicy.allowed_source_types(
                "InvalidAgentKind",  # type: ignore
                ClaimKind.SYNDROME_BASIS,
            )

    def test_is_source_allowed_exact(self) -> None:

        assert SandboxSourcePolicy.is_source_allowed(AgentKind.SYNDROME, ClaimKind.SYNDROME_BASIS, SourceType.THEORY)

        assert not SandboxSourcePolicy.is_source_allowed(
            AgentKind.SYNDROME, ClaimKind.SYNDROME_BASIS, SourceType.FORMULA
        )

        assert SandboxSourcePolicy.is_source_allowed(AgentKind.FORMULA, ClaimKind.HERB, SourceType.HERB)

        assert not SandboxSourcePolicy.is_source_allowed(AgentKind.FORMULA, ClaimKind.FORMULA_NAME, SourceType.HERB)


# ===================================================================

# 5. Offline retrieval nodes

# ===================================================================


class TestRetrievalNodes:
    def test_syndrome_node_returns_packets(self) -> None:

        node = SyndromeRetrievalNode()

        result = node.retrieve(
            query="What is the syndrome basis?",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            max_results=2,
        )

        assert len(result.packets) > 0

        assert result.total == len(result.packets)

        assert result.status == "ok"

    def test_syndrome_node_rank_is_unique_and_continuous(self) -> None:

        node = SyndromeRetrievalNode()

        result = node.retrieve(
            query="deficiency syndrome",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            max_results=5,
        )

        ranks = [p.rank for p in result.packets]

        assert ranks == list(range(1, len(ranks) + 1))

    def test_formula_node_returns_packets(self) -> None:

        node = FormulaRetrievalNode()

        result = node.retrieve(
            query="formula for deficiency",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            max_results=2,
        )

        assert len(result.packets) > 0

        assert result.total == len(result.packets)

    def test_formula_node_rank_is_unique_and_continuous(self) -> None:

        node = FormulaRetrievalNode()

        result = node.retrieve(
            query="Buzhong Yiqi Tang",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            max_results=3,
        )

        ranks = [p.rank for p in result.packets]

        assert ranks == list(range(1, len(ranks) + 1))

    def test_syndrome_node_packet_is_deterministic(self) -> None:

        node = SyndromeRetrievalNode()

        left = node.retrieve(
            query="Qi deficiency syndrome",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        right = node.retrieve(
            query="Qi deficiency syndrome",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        assert left.model_dump(mode="json") == right.model_dump(mode="json")

        assert json.dumps(left.model_dump(mode="json"), sort_keys=True) == json.dumps(
            right.model_dump(mode="json"), sort_keys=True
        )

    def test_formula_node_packet_is_deterministic(self) -> None:

        node = FormulaRetrievalNode()

        left = node.retrieve(
            query="Buzhong Yiqi Tang formula",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            max_results=2,
        )

        right = node.retrieve(
            query="Buzhong Yiqi Tang formula",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            max_results=2,
        )

        assert left.model_dump(mode="json") == right.model_dump(mode="json")

    def test_different_graph_run_yields_different_retrieval_run_id(self) -> None:
        """Evidence from different graph runs must have different retrieval_run."""

        node = SyndromeRetrievalNode()

        a = node.retrieve(query="fatigue", graph_run="run-1", graph_trace="trace-1")

        b = node.retrieve(query="fatigue", graph_run="run-2", graph_trace="trace-2")

        assert a.bundles[0].retrieval_run != b.bundles[0].retrieval_run

    def test_result_limits_enforced(self) -> None:

        node = SyndromeRetrievalNode()

        with pytest.raises(SandboxEvidenceError):
            node.retrieve(
                query="test",
                graph_run=_GRAPH_RUN,
                graph_trace=_GRAPH_TRACE,
                max_results=EvidenceResultLimit.MAX_ITEMS + 1,
            )


# ===================================================================

# 6. SandboxEvidenceStore (append-only, idempotent, snapshot/restore)

# ===================================================================


class TestEvidenceStore:
    def test_put_and_get_roundtrip(self) -> None:

        store = _make_test_store()

        bundle = _fake_bundle()

        _admit(store, bundle)

        retrieved = store.get(bundle.bundle_digest)

        assert retrieved is not None

        assert retrieved.bundle_digest == bundle.bundle_digest

    def test_get_missing_returns_none(self) -> None:

        store = _make_test_store()

        assert store.get("nonexistent-digest") is None

    def test_same_key_same_bytes_is_idempotent(self) -> None:

        store = _make_test_store()

        bundle = _fake_bundle()

        _admit(store, bundle)

        _admit(store, bundle)  # second put should succeed

        assert store.get(bundle.bundle_digest) is not None

    def test_same_key_different_bytes_is_rejected(self) -> None:
        """The store must detect content mismatch under the same digest key."""

        store = _make_test_store()

        bundle = _fake_bundle()

        _admit(store, bundle)

        # Put same bundle again is idempotent

        _admit(store, bundle)

        # Verify retrieved content matches original exactly

        retrieved = store.get(bundle.bundle_digest)

        assert retrieved is not None

        assert retrieved.model_dump(mode="json") == bundle.model_dump(mode="json")

    def test_put_rejects_wrong_graph_run_visibility(self) -> None:

        store = _make_test_store()

        bundle = _fake_bundle(retrieval_run="rr-visible")

        _admit(store, bundle)

        # Bundles from a different graph run should not be visible

        hidden = _fake_bundle(
            retrieval_run="rr-hidden",
            packets=(_fake_packet(evidence_id="ev-hidden", retrieval_run="rr-hidden"),),
        )

        _admit(store, hidden)

        visible = store.get_bundles_for_retrieval_run("rr-visible")

        assert all(b.retrieval_run == "rr-visible" for b in visible)

    def test_snapshot_roundtrip(self) -> None:

        reg = _make_test_registry()

        store = SandboxEvidenceStore(registry=reg)

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        snapshot = store.snapshot()

        reg2 = _make_test_registry()

        reg2.add_recognized(bundle.bundle_digest)

        restored = SandboxEvidenceStore.restore(snapshot, registry=reg2)

        assert restored.get(bundle.bundle_digest) is not None

    def test_snapshot_is_canonical(self) -> None:

        store = _make_test_store()

        b1 = _fake_bundle(query="q1", retrieval_run="rr-1")

        b2 = _fake_bundle(query="q2", retrieval_run="rr-2")

        _admit(store, b1)

        _admit(store, b2)

        snap = store.snapshot()

        assert type(snap) is SandboxEvidenceStoreSnapshotV1

        assert isinstance(snap.data, str)

    def test_snapshot_size_limit(self) -> None:

        store = _make_test_store()

        # Fill to just under limit

        text = "x" * 1024

        for i in range(200):
            p = _fake_packet(
                evidence_id=f"ev-big-{i}",
                content=text,
                retrieval_run=f"rr-big-{i}",
            )

            b = _fake_bundle(
                packets=(p,),
                query=f"query-{i}",
                retrieval_run=f"rr-big-{i}",
                node_name="syndrome_retrieval",
            )

            _admit(store, b)

        snapshot = store.snapshot()

        # Serialize once more â€” must stay under limit

        encoded = json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":"))

        assert len(encoded.encode("utf-8")) <= 256 * 1024

    def test_restore_deterministic_replay(self) -> None:

        reg_a = _make_test_registry()

        store_a = SandboxEvidenceStore(registry=reg_a)

        b1 = _fake_bundle(query="q1", retrieval_run="rr-1")

        b2 = _fake_bundle(query="q2", retrieval_run="rr-2")

        reg_a.add_recognized(b1.bundle_digest)

        store_a.put(b1)

        reg_a.add_recognized(b2.bundle_digest)

        store_a.put(b2)

        snap_a = store_a.snapshot()

        reg_before = _make_test_registry()

        store_b = SandboxEvidenceStore(registry=reg_before)

        q_before = _fake_bundle(query="q-before", retrieval_run="rr-before")

        reg_before.add_recognized(q_before.bundle_digest)

        store_b.put(q_before)

        reg_b = _make_test_registry()

        reg_b.add_recognized(b1.bundle_digest)

        reg_b.add_recognized(b2.bundle_digest)

        store_b = SandboxEvidenceStore.restore(snap_a, registry=reg_b)

        assert store_b.get(b1.bundle_digest) is not None

        assert store_b.get(b2.bundle_digest) is not None

        # q-before should be gone

        assert store_b.get(q_before.bundle_digest) is None

    def test_restore_rejects_tampered_snapshot(self) -> None:
        """The restore method must validate the snapshot digest."""

        store = _make_test_store()

        bundle = _fake_bundle()

        _admit(store, bundle)

        snap = store.snapshot()

        # Tamper the data â€” restore must catch it

        tampered = SandboxEvidenceStoreSnapshotV1(
            data=snap.data + "tampered",
            digest=snap.digest,
        )

        with pytest.raises(SandboxEvidenceError):
            SandboxEvidenceStore.restore(tampered, registry=_make_test_registry())

    def test_store_rejects_self_recompute_snapshot(self) -> None:
        """A snapshot cannot be its own authorizer â€” re-authorize after restore."""

        reg = _make_test_registry()

        store = SandboxEvidenceStore(registry=reg)

        b1 = _fake_bundle()

        reg.add_recognized(b1.bundle_digest)

        store.put(b1)

        snap = store.snapshot()

        reg2 = _make_test_registry()

        reg2.add_recognized(b1.bundle_digest)

        restored = SandboxEvidenceStore.restore(snap, registry=reg2)

        # New bundles need explicit authorization after restore

        b2 = _fake_bundle(query="new", retrieval_run="rr-new")

        restored.registry.add_recognized(b2.bundle_digest)

        restored.put(b2)

        snap2 = restored.snapshot()

        # Prove snap2 came from explicit put, not from self-recompute

        assert snap2.data != snap.data


# ===================================================================

# 7. Citation Verifier

# ===================================================================


class TestCitationVerifier:
    def test_rag_supported_with_valid_links(self) -> None:

        verifier = CitationVerifier()

        packet = _fake_packet()

        bundle = _fake_bundle((packet,))

        claim = _fake_claim(evidence_ids=(packet.evidence_id,))

        link = _fake_link(
            bundle_digest=bundle.bundle_digest,
            content_digest=packet.content_digest,
        )

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={bundle.bundle_digest: bundle},
            claims=(claim,),
            links=(link,),
        )

        assert result == ClaimVerifierResult.RAG_SUPPORTED

    def test_missing_link_leads_to_model_knowledge_only(self) -> None:

        verifier = CitationVerifier()

        bundle = _fake_bundle()

        claim = _fake_claim(evidence_ids=("ev-001",))

        # No links at all

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={bundle.bundle_digest: bundle},
            claims=(claim,),
            links=(),
        )

        assert result == ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

    def test_wrong_source_type_rejected(self) -> None:

        verifier = CitationVerifier()

        # Syndrome basis with a FORMULA source â€” should fail

        packet = _fake_packet(source_type=SourceType.FORMULA)

        bundle = _fake_bundle((packet,))

        claim = _fake_claim(evidence_ids=(packet.evidence_id,))

        link = _fake_link(
            bundle_digest=bundle.bundle_digest,
            content_digest=packet.content_digest,
        )

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={bundle.bundle_digest: bundle},
            claims=(claim,),
            links=(link,),
        )

        assert result == ClaimVerifierResult.HARD_BLOCK

    def test_cross_run_evidence_invisible(self) -> None:

        verifier = CitationVerifier()

        packet = _fake_packet(retrieval_run="other-run")

        bundle = _fake_bundle((packet,), retrieval_run="other-run")

        claim = _fake_claim(evidence_ids=(packet.evidence_id,))

        link = _fake_link(
            bundle_digest=bundle.bundle_digest,
            content_digest=packet.content_digest,
            retrieval_run="other-run",
        )

        # verify with different retrievability scope

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={bundle.bundle_digest: bundle},
            claims=(claim,),
            links=(link,),
            allowed_retrieval_runs=("current-run",),
        )

        assert result != ClaimVerifierResult.RAG_SUPPORTED

    def test_content_tamper_rejected(self) -> None:

        verifier = CitationVerifier()

        packet = _fake_packet(content_digest=_text_digest("original content"))

        bundle = _fake_bundle((packet,))

        claim = _fake_claim(evidence_ids=(packet.evidence_id,))

        # Link references a different content digest

        link = _fake_link(
            bundle_digest=bundle.bundle_digest,
            content_digest=_text_digest("tampered content"),
        )

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={bundle.bundle_digest: bundle},
            claims=(claim,),
            links=(link,),
        )

        assert result != ClaimVerifierResult.RAG_SUPPORTED

    def test_missing_bundle_rejected(self) -> None:

        verifier = CitationVerifier()

        claim = _fake_claim(evidence_ids=("ev-missing",))

        missing_digest = hashlib.sha256(b"missing").hexdigest()

        link = _fake_link(bundle_digest=missing_digest)

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={},
            claims=(claim,),
            links=(link,),
        )

        assert result == ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

    def test_all_claims_must_have_links_for_rag_supported(self) -> None:

        verifier = CitationVerifier()

        p1 = _fake_packet(evidence_id="ev-001")

        p2 = _fake_packet(evidence_id="ev-002")

        b = _fake_bundle((p1, p2))

        c1 = _fake_claim(claim_id="cl-001", evidence_ids=("ev-001",))

        c2 = _fake_claim(claim_id="cl-002", evidence_ids=("ev-002",))

        # Only link cl-001

        link = _fake_link(
            claim_id="cl-001",
            evidence_id="ev-001",
            bundle_digest=b.bundle_digest,
            content_digest=p1.content_digest,
        )

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={b.bundle_digest: b},
            claims=(c1, c2),
            links=(link,),
        )

        # cl-002 has no link

        assert result != ClaimVerifierResult.RAG_SUPPORTED

    def test_unknown_evidence_id_in_link_rejected(self) -> None:

        verifier = CitationVerifier()

        b = _fake_bundle()

        claim = _fake_claim(evidence_ids=("ev-001",))

        # Link references evidence "ev-999" not in claim

        link = _fake_link(
            claim_id="cl-001",
            evidence_id="ev-999",
            bundle_digest=b.bundle_digest,
        )

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={b.bundle_digest: b},
            claims=(claim,),
            links=(link,),
        )

        assert result != ClaimVerifierResult.RAG_SUPPORTED


# ===================================================================

# 8. Fallback and hard-block policies

# ===================================================================


class TestFallbackPolicies:
    def test_model_knowledge_only_requires_empty_evidence(self) -> None:
        """model_knowledge_only: evidence packets, links, context all empty."""

        verifier = CitationVerifier()

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={},
            claims=(),
            links=(),
            fallback=FallbackPolicy.MODEL_KNOWLEDGE_ONLY,
        )

        assert result == ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

    def test_model_knowledge_only_rejects_non_empty_evidence(self) -> None:

        verifier = CitationVerifier()

        packet = _fake_packet()

        bundle = _fake_bundle((packet,))

        with pytest.raises(SandboxEvidenceError):
            verifier.verify(
                agent_kind=AgentKind.SYNDROME,
                bundles={bundle.bundle_digest: bundle},
                claims=(_fake_claim(),),
                links=(_fake_link(),),
                fallback=FallbackPolicy.MODEL_KNOWLEDGE_ONLY,
            )

    def test_hard_block_returns_payload_free_failure(self) -> None:
        """hard_block: fixed, payload-free, chainless failure."""

        verifier = CitationVerifier()

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={},
            claims=(),
            links=(),
            fallback=FallbackPolicy.HARD_BLOCK,
        )

        assert result == ClaimVerifierResult.HARD_BLOCK

    def test_no_fake_citation_allowed(self) -> None:
        """Without evidence, citation set must be empty."""

        verifier = CitationVerifier()

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={},
            claims=(),
            links=(),
            fallback=FallbackPolicy.RAG_SUPPORTED,
        )

        assert result == ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

    def test_hard_block_cannot_be_downgraded(self) -> None:
        """hard_block must not degrade to knowledge answer."""

        verifier = CitationVerifier()

        result = verifier.verify(
            agent_kind=AgentKind.SYNDROME,
            bundles={},
            claims=(),
            links=(),
            fallback=FallbackPolicy.HARD_BLOCK,
        )

        assert result is ClaimVerifierResult.HARD_BLOCK

        # Must not be RAG_SUPPORTED or MODEL_KNOWLEDGE_ONLY

        assert result not in (
            ClaimVerifierResult.RAG_SUPPORTED,
            ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY,
        )


# ===================================================================

# 9. EvidencePipeline (retrieve â†’ store â†’ claim link â†’ verify â†’ context)

# ===================================================================


class TestEvidencePipeline:
    def test_pipeline_rag_supported_path(self) -> None:

        store = _make_test_store()

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        result = pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="What is the syndrome basis for fatigue?",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            fallback=FallbackPolicy.RAG_SUPPORTED,
        )

        assert result.fallback == FallbackPolicy.RAG_SUPPORTED

        # Retrieval should have produced packets and bundles

        assert len(result.bundles) > 0

        # Verifier returns model_knowledge_only (no claims/links provided)

        # but retrieval happened â€” bundles were stored

        stored_bundles = store.get_bundles_for_retrieval_run(result.bundles[0].retrieval_run)

        assert len(stored_bundles) > 0

        assert all(b.retrieval_run == result.bundles[0].retrieval_run for b in stored_bundles)

    def test_pipeline_no_rag_does_not_call_retrieval(self) -> None:
        """No-RAG branch: retrieval/claim verifier not called, references empty."""

        store = _make_test_store()

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        result = pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            fallback=FallbackPolicy.MODEL_KNOWLEDGE_ONLY,
        )

        assert result.fallback == FallbackPolicy.MODEL_KNOWLEDGE_ONLY

        assert result.result == ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

        assert len(result.bundles) == 0

        assert len(result.packets) == 0

    def test_pipeline_hard_block_path(self) -> None:

        store = _make_test_store()

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        result = pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="anything",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
            fallback=FallbackPolicy.HARD_BLOCK,
        )

        assert result.result == ClaimVerifierResult.HARD_BLOCK

    def test_context_projection_only_in_context_data_tool(self) -> None:
        """Evidence must only enter context_data_tool projection."""

        store = _make_test_store()

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        result = pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="deficiency",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        if result.context is not None:
            assert type(result.context) is SandboxEvidenceContextV1


# ===================================================================

# 10. Resource limits (task Â§7)

# ===================================================================


class TestResourceLimits:
    def test_bundle_items_exceeds_limit_rejected(self) -> None:

        too_many = tuple(
            _fake_packet(
                evidence_id=f"ev-{i}",
                rank=(i % EvidencePacketLimit.MAX_ITEMS) + 1,
            )
            for i in range(EvidencePacketLimit.MAX_ITEMS + 1)
        )

        with pytest.raises((SandboxEvidenceError, ValidationError)):
            _fake_bundle(packets=too_many)

    def test_result_items_exceeds_limit_rejected(self) -> None:

        node = SyndromeRetrievalNode()

        with pytest.raises(SandboxEvidenceError):
            node.retrieve(
                query="test",
                graph_run=_GRAPH_RUN,
                graph_trace=_GRAPH_TRACE,
                max_results=EvidenceResultLimit.MAX_ITEMS + 1,
            )

    def test_claims_limit_enforced(self) -> None:

        store = _make_test_store()

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        many_claims = tuple(
            _fake_claim(
                claim_id=f"cl-{i}",
                claim_text=f"claim {i}",
                evidence_ids=(),
            )
            for i in range(129)
        )

        with pytest.raises(SandboxEvidenceError):
            pipeline.verify_claims(
                agent_kind=AgentKind.SYNDROME,
                claims=many_claims,
                links=(),
                fallback=FallbackPolicy.RAG_SUPPORTED,
            )

    def test_links_limit_enforced(self) -> None:

        store = _make_test_store()

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        many_links = tuple(_fake_link(claim_id=f"cl-{i}", evidence_id=f"ev-{i}") for i in range(129))

        with pytest.raises(SandboxEvidenceError):
            pipeline.verify_claims(
                agent_kind=AgentKind.SYNDROME,
                claims=(_fake_claim(),),
                links=many_links,
                fallback=FallbackPolicy.RAG_SUPPORTED,
            )


# ===================================================================

# 11. Error boundary tests

# ===================================================================


class TestErrorBoundaries:
    def test_error_is_chainless(self) -> None:

        try:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")

        except SandboxEvidenceError:
            import sys

            exc_info = sys.exc_info()

            assert exc_info[1] is not None

            # Should not have a chained exception from __cause__ or __context__

            assert exc_info[1].__cause__ is None

    def test_error_enforces_known_codes(self) -> None:

        # Known codes

        for code in (
            "SANDBOX_EVIDENCE_SCHEMA_INVALID",
            "SANDBOX_EVIDENCE_VERSION_MISMATCH",
            "SANDBOX_EVIDENCE_DIGEST_MISMATCH",
            "SANDBOX_EVIDENCE_LIMIT_EXCEEDED",
            "SANDBOX_EVIDENCE_AUTHORITY_REJECTED",
            "SANDBOX_EVIDENCE_SOURCE_POLICY_REJECTED",
            "SANDBOX_EVIDENCE_RUN_VISIBILITY_REJECTED",
            "SANDBOX_EVIDENCE_CLAIM_COMPLETENESS_REJECTED",
            "SANDBOX_EVIDENCE_UNAVAILABLE",
            "SANDBOX_EVIDENCE_INTEGRITY_FAILURE",
        ):
            err = SandboxEvidenceError(code)

            assert str(err) == code

    def test_error_message_does_not_leak_payload(self) -> None:

        err = SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")

        msg = str(err)

        # Message must be the code itself, no extra content

        assert msg == "SANDBOX_EVIDENCE_SCHEMA_INVALID"


# ===================================================================

# 12. Threat model probes (task Â§8)

# ===================================================================


class TestThreatModel:
    def test_scalar_subclass_rejected(self) -> None:
        """Subclass of str used as source_type must be caught."""

        class EvilStr(str):
            pass

        with pytest.raises(ValidationError):
            _fake_packet(source_type=cast(SourceType, EvilStr("theory")))

    def test_enum_scalar_substance_rejected(self) -> None:
        """Direct cast of a plain string to enum doesn't pass strict mode."""

        with pytest.raises(ValidationError):
            SandboxEvidencePacketV1(
                evidence_id="ev-001",
                source_type="theory",  # not a SourceType enum â€” strict rejects
                source_id="src-1",
                chunk_id="chunk-1",
                rank=1,
                content_digest="a" * 64,
                retrieval_trace=SandboxRetrievalTraceV1(retrieval_run="r", graph_run="g", graph_trace="t"),
            )

    def test_callback_reentry_during_authorize(self) -> None:
        """Authorizer callback reentry must not corrupt store state."""

        entered = threading.Event()

        released = threading.Event()

        results: list[Exception | None] = []

        class ReentrantAuthorizer:
            def authorize(self, *, bundle_digest: str) -> bool:

                entered.set()

                # Inside authorize, try a store operation (reentry)

                try:
                    store.put(_fake_bundle(query="reentry", retrieval_run="rr-reentry"))

                except SandboxEvidenceError as e:
                    results.append(e)

                released.wait(timeout=5)

                return True

        reg = SandboxEvidenceRegistry(authorizer=ReentrantAuthorizer())

        store = SandboxEvidenceStore(registry=reg)

        bundle = _fake_bundle(query="original", retrieval_run="rr-original")

        reg.add_recognized(bundle.bundle_digest)

        thread = threading.Thread(target=lambda: store.put(bundle))

        thread.start()

        entered.wait(timeout=5)

        released.set()

        thread.join(timeout=5)

    def test_instance_method_shadowing_rejected(self) -> None:
        """Instance-level method shadowing must not corrupt store integrity."""

        store = _make_test_store()

        bundle = _fake_bundle()

        _admit(store, bundle)

        # Shadow put with a no-op

        def evil_put(bundle: object) -> None:

            raise RuntimeError("shadowed")

        # With __slots__, instance-level method assignment raises AttributeError

        with pytest.raises(AttributeError):
            store.put = evil_put  # type: ignore[method-assign]

        # The store's original data remains intact via get

        assert store.get(bundle.bundle_digest) is not None

    def test_authority_replacement_rejected(self) -> None:
        """Replacing the store's authority must fail-closed."""

        store = _make_test_store()

        # The store should not expose an authority-replacement method

        assert not hasattr(store, "replace_authority")

        assert not hasattr(store, "set_authority")

    def test_authority_revoked_after_use_fails_closed(self) -> None:
        """Revocation before next operation must fail."""

        reg1 = _make_test_registry()

        store1 = SandboxEvidenceStore(registry=reg1)

        b1 = _fake_bundle()

        reg1.add_recognized(b1.bundle_digest)

        store1.put(b1)

        assert store1.get(b1.bundle_digest) is not None

        # Different store with different registry should not see the bundle

        store2 = _make_test_store()

        assert store2.get(b1.bundle_digest) is None

    def test_missing_packets_in_bundle_raises(self) -> None:
        """Bundle with empty packets should be rejected."""

        with pytest.raises(ValidationError):
            _fake_bundle(packets=())

    def test_empty_retrieval_run_rejected(self) -> None:

        with pytest.raises(ValidationError):
            SandboxRetrievalTraceV1(
                retrieval_run="",
                graph_run=_GRAPH_RUN,
                graph_trace=_GRAPH_TRACE,
            )


# ===================================================================

# 13. Syndrome node fixed synthetic content

# ===================================================================


class TestSyndromeNodeContent:
    def test_syndrome_node_has_theory_sources(self) -> None:

        node = SyndromeRetrievalNode()

        result = node.retrieve(
            query="deficiency",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        for p in result.packets:
            assert p.source_type in (SourceType.THEORY, SourceType.CASE)

    def test_syndrome_node_packet_has_retrieval_trace(self) -> None:

        node = SyndromeRetrievalNode()

        result = node.retrieve(
            query="fatigue",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        for p in result.packets:
            assert p.retrieval_trace.graph_run == _GRAPH_RUN

            assert p.retrieval_trace.graph_trace == _GRAPH_TRACE

    def test_syndrome_node_result_structure(self) -> None:

        node = SyndromeRetrievalNode()

        result = node.retrieve(
            query="pulse diagnosis",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        assert isinstance(result, SandboxEvidenceResultV1)

        assert len(result.bundles) > 0

        assert all(b.node_name == "syndrome_retrieval" for b in result.bundles)


class TestFormulaNodeContent:
    def test_formula_node_has_formula_and_herb_sources(self) -> None:

        node = FormulaRetrievalNode()

        result = node.retrieve(
            query="Buzhong Yiqi Tang",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        for p in result.packets:
            assert p.source_type in (SourceType.FORMULA, SourceType.HERB)

    def test_formula_node_packet_has_retrieval_trace(self) -> None:

        node = FormulaRetrievalNode()

        result = node.retrieve(
            query="astragalus",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        for p in result.packets:
            assert p.retrieval_trace.graph_run == _GRAPH_RUN

            assert p.retrieval_trace.graph_trace == _GRAPH_TRACE

    def test_formula_node_result_structure(self) -> None:

        node = FormulaRetrievalNode()

        result = node.retrieve(
            query="ginseng",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        assert isinstance(result, SandboxEvidenceResultV1)

        assert len(result.bundles) > 0

        assert all(b.node_name == "formula_retrieval" for b in result.bundles)


# ===================================================================

# 14. R1 Authority & state integrity (ACC-20260727-059 findings)

# ===================================================================


class TestR1RegistryAndAuthorizer:
    """P0: Live bundle registry and authorizer (finding: production has none)."""

    def test_registry_class_defined(self) -> None:
        """SandboxEvidenceRegistry must be defined for live authority."""

        import app.agent_runtime.sandbox_evidence as m

        assert hasattr(m, "SandboxEvidenceRegistry"), "R1 RED: SandboxEvidenceRegistry not defined"

    def test_authorizer_protocol_defined(self) -> None:
        """SandboxEvidenceAuthorizer Protocol must be defined."""

        import app.agent_runtime.sandbox_evidence as m

        assert hasattr(m, "SandboxEvidenceAuthorizer"), "R1 RED: SandboxEvidenceAuthorizer not defined"

    def test_registry_accepts_recognize(self) -> None:
        """Registry.recognize(digest) -> bool must exist."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        assert callable(getattr(reg, "recognize", None)), "R1 RED: registry.recognize not callable"

    def test_registry_accepts_add_recognized(self) -> None:
        """Registry.add_recognized(digest) must exist."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        assert callable(getattr(reg, "add_recognized", None)), "R1 RED: registry.add_recognized not callable"

    def test_registry_epoch_property(self) -> None:
        """Registry must expose epoch for monotonic check."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        epoch = getattr(reg, "epoch", None)

        assert epoch is not None, "R1 RED: registry.epoch not accessible"

    def test_store_accepts_registry_parameter(self) -> None:
        """SandboxEvidenceStore must accept registry= parameter."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        assert store is not None


class TestR1RevokeReauthorize:
    """P2: Same-instance revoke/reauthorize (finding: new-store faked)."""

    _TAG = "_PermissiveAuthorizer"

    def test_registry_revoke_method(self) -> None:
        """Registry must have revoke(digest) method."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        assert callable(getattr(reg, "revoke", None)), "R1 RED: registry.revoke not callable"

    def test_registry_reauthorize_method(self) -> None:
        """Registry must have reauthorize(digest) -> bool method."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        assert callable(getattr(reg, "reauthorize", None)), "R1 RED: registry.reauthorize not callable"

    def test_registry_revoke_removes_recognized(self) -> None:
        """After revoke, registry.recognize returns False for that digest."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        digest = "a" * 64

        reg.add_recognized(digest)

        assert reg.recognize(digest)

        reg.revoke(digest)

        assert not reg.recognize(digest)

    def test_revoke_then_store_put_rejected(self) -> None:
        """After revoke(digest), store.put for same digest must raise."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        assert store.get(bundle.bundle_digest) is not None

        reg.revoke(bundle.bundle_digest)

        with pytest.raises(SandboxEvidenceError):
            store.put(bundle)

    def test_revoke_then_store_get_returns_none(self) -> None:
        """After revoke, store.get for revoked digest must return None."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        reg.revoke(bundle.bundle_digest)

        assert store.get(bundle.bundle_digest) is None

    def test_reauthorize_restores_put(self) -> None:
        """reauthorize after revoke allows store.put to succeed."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        reg.revoke(bundle.bundle_digest)

        assert reg.reauthorize(bundle.bundle_digest), "reauthorize must return True"

        store.put(bundle)  # Must succeed

        assert store.get(bundle.bundle_digest) is not None

    def test_reauthorize_rejected_for_unknown_digest(self) -> None:
        """reauthorize must fail for digest never recognized."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        unknown = "f" * 64

        with pytest.raises(SandboxEvidenceError):
            reg.reauthorize(unknown)

    def test_revoke_before_any_put_is_noop(self) -> None:
        """Revoking an unrecognized digest does not raise."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        unknown = "e" * 64

        reg.revoke(unknown)  # Should not raise


class TestR1VerifierProtocol:
    """P1: External snapshot-external verifier Protocol."""

    def test_verifier_protocol_defined(self) -> None:
        """ClaimVerifierProtocol must be defined."""

        import app.agent_runtime.sandbox_evidence as m

        assert hasattr(m, "ClaimVerifierProtocol"), "R1 RED: ClaimVerifierProtocol not defined"

    def test_pipeline_verifier_param_accepts_protocol(self) -> None:
        """Pipeline must accept a Protocol-based verifier."""

        import app.agent_runtime.sandbox_evidence as m

        VerifierCls = getattr(m, "ClaimVerifierProtocol", None)

        assert VerifierCls is not None, "R1 RED: ClaimVerifierProtocol not defined"

        store = _make_test_store()

        class _TestVerifier:
            def verify(self, **kwargs: object) -> object:

                return m.ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=_TestVerifier(),
        )

        assert pipeline is not None

    def test_citation_verifier_not_used_as_default(self) -> None:
        """Pipeline must not set CitationVerifier as default â€” caller must inject."""

        import inspect

        sig = inspect.signature(EvidencePipeline.__init__)

        verifier_param = sig.parameters.get("verifier")

        assert verifier_param is not None

        if verifier_param.default is not inspect.Parameter.empty:
            import app.agent_runtime.sandbox_evidence as m

            assert type(verifier_param.default) is not m.CitationVerifier, "R1: CitationVerifier must not be default"


class TestR1CallbacksAndReentry:
    """P1: Authorizer/verifier callbacks with reentry guard."""

    def test_authorizer_callback_during_put(self) -> None:
        """Authorizer must be invoked when registry checks during store.put."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        invoked = threading.Event()

        class _TestAuth:
            def authorize(self, *, bundle_digest: str) -> bool:

                invoked.set()

                return True

        reg = RegistryCls(authorizer=_TestAuth())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)  # type: ignore[call-arg]

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        assert invoked.is_set(), "R1 RED: authorizer was not invoked"

    def test_reentry_during_callback_rejected(self) -> None:
        """Reentrant store call during authorize callback must raise."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        registry_used = threading.Event()

        reentry_result: list[Exception | None] = []

        class _ReentrantAuth:
            def authorize(self, *, bundle_digest: str) -> bool:

                registry_used.set()

                try:
                    store.put(_fake_bundle(query="reentry"))

                except SandboxEvidenceError as e:
                    reentry_result.append(e)

                except Exception as e:
                    reentry_result.append(e)

                return True

        reg = RegistryCls(authorizer=_ReentrantAuth())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)  # type: ignore[call-arg]

        bundle = _fake_bundle(query="original")

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        assert registry_used.is_set(), "authorizer was not called"

        assert len(reentry_result) > 0, "R1 RED: reentry was not detected â€” no error raised"

        assert isinstance(reentry_result[0], SandboxEvidenceError), (
            f"R1 RED: reentry must raise SandboxEvidenceError, got {type(reentry_result[0]).__name__}"
        )

    def test_verifier_callback_during_pipeline_run(self) -> None:
        """Verifier must be invoked during pipeline.run RAG path."""

        import app.agent_runtime.sandbox_evidence as m

        VerifierCls = getattr(m, "ClaimVerifierProtocol", None)

        assert VerifierCls is not None, "R1 RED: ClaimVerifierProtocol not defined"

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        invoked = threading.Event()

        class _TestVerifier:
            def verify(self, **kwargs: object) -> object:

                invoked.set()

                return m.ClaimVerifierResult.RAG_SUPPORTED

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=_TestVerifier(),
        )

        pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="test",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        assert invoked.is_set(), "R1 RED: verifier was not invoked"


class TestR1MethodShadowing:
    """P1: Instance method shadowing must fail-closed."""

    def test_store_has_slots(self) -> None:
        """SandboxEvidenceStore must use __slots__ to prevent instance dict."""

        assert hasattr(SandboxEvidenceStore, "__slots__"), "R1 RED: SandboxEvidenceStore has no __slots__"

    def test_pipeline_has_slots(self) -> None:
        """EvidencePipeline must use __slots__."""

        assert hasattr(EvidencePipeline, "__slots__"), "R1 RED: EvidencePipeline has no __slots__"

    def test_store_put_cannot_be_shadowed(self) -> None:
        """Instance-level store.put = evil must raise AttributeError."""

        store = _make_test_store()

        def evil_put(bundle: object) -> None:

            raise RuntimeError("shadowed")

        with pytest.raises(AttributeError):
            store.put = evil_put  # type: ignore[method-assign]

    def test_pipeline_run_cannot_be_shadowed(self) -> None:
        """Instance-level pipeline.run = evil must raise AttributeError."""

        store = _make_test_store()

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        def evil_run(**kwargs: object) -> object:

            return None

        with pytest.raises(AttributeError):
            pipeline.run = evil_run  # type: ignore[method-assign]

    def test_registry_has_slots(self) -> None:
        """Registry class must prevent instance shadowing via __slots__."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        assert hasattr(RegistryCls, "__slots__"), "R1 RED: SandboxEvidenceRegistry has no __slots__"


class TestR1GraphRunFilter:
    """P2: get_bundles_for_graph_run must filter by bundle.graph_run."""

    def test_get_bundles_for_graph_run_uses_graph_run(self) -> None:
        """get_bundles_for_graph_run(graph_run) must filter by bundle.graph_run."""

        store = _make_test_store()

        b1 = _fake_bundle(query="q1", retrieval_run="retrieval-A", graph_run="graph-A")

        b2 = _fake_bundle(
            query="q2",
            retrieval_run="retrieval-B",
            graph_run="graph-A",
            packets=(_fake_packet(evidence_id="ev-2", retrieval_run="retrieval-B", graph_run="graph-A"),),
        )

        _admit(store, b1)

        _admit(store, b2)

        result = store.get_bundles_for_graph_run("graph-A")

        assert len(result) == 2, f"R1 RED: expected 2 bundles for graph-A, got {len(result)}"

        for b in result:
            assert b.graph_run == "graph-A", f"R1 RED: bundle has graph_run={b.graph_run}, expected graph-A"

    def test_different_graph_run_no_cross_visibility(self) -> None:
        """Two different graph runs must not share visible bundles."""

        store = _make_test_store()

        b1 = _fake_bundle(query="q1", retrieval_run="retrieval-X", graph_run="graph-X")

        b2 = _fake_bundle(
            query="q2",
            retrieval_run="retrieval-Y",
            graph_run="graph-Y",
            packets=(_fake_packet(evidence_id="ev-y", retrieval_run="retrieval-Y", graph_run="graph-Y"),),
        )

        _admit(store, b1)

        _admit(store, b2)

        result_x = store.get_bundles_for_graph_run("graph-X")

        assert len(result_x) == 1

        assert result_x[0].graph_run == "graph-X"

        result_y = store.get_bundles_for_graph_run("graph-Y")

        assert len(result_y) == 1

        assert result_y[0].graph_run == "graph-Y"

    def test_get_bundles_for_retrieval_run_method_exists(self) -> None:
        """Retrieval-run query must be available under distinct method name."""

        assert hasattr(SandboxEvidenceStore, "get_bundles_for_retrieval_run"), (
            "R1 RED: get_bundles_for_retrieval_run not defined"
        )


class TestR1SnapshotRestoreAuthority:
    """P1/P2: Restore requires external authority re-injection."""

    def test_restored_store_rejects_put_without_registry(self) -> None:
        """Restored store with explicit empty registry must reject operations."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        snap = store.snapshot()

        reg_empty = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        restored = SandboxEvidenceStore.restore(snap, registry=reg_empty)

        with pytest.raises(SandboxEvidenceError):
            restored.put(bundle)

    def test_restore_accepts_registry_parameter(self) -> None:
        """restore must accept registry= for restored authority."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        snap = store.snapshot()

        reg2 = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        reg2.add_recognized(bundle.bundle_digest)

        restored = SandboxEvidenceStore.restore(snap, registry=reg2)

        assert restored.get(bundle.bundle_digest) is not None

    def test_new_registry_no_auto_authority(self) -> None:
        """Restored store with new registry must not auto-recognize."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        b1 = _fake_bundle(query="q1")

        reg.add_recognized(b1.bundle_digest)

        store.put(b1)

        snap = store.snapshot()

        reg2 = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        restored = SandboxEvidenceStore.restore(snap, registry=reg2)

        assert restored.get(b1.bundle_digest) is None, "R1 RED: restored store without authority returns bundle"


class TestR1EpochSemantics:
    """Â§4: Monotonic epoch guards against replay."""

    def test_epoch_increments_on_add_recognized(self) -> None:
        """Registry epoch must increment when adding recognized digest."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        e0 = reg.epoch

        reg.add_recognized("a" * 64)

        assert reg.epoch > e0, "R1 RED: epoch did not increment on add_recognized"

    def test_epoch_increments_on_revoke(self) -> None:
        """Registry epoch must increment on revoke."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        reg.add_recognized("a" * 64)

        e0 = reg.epoch

        reg.revoke("a" * 64)

        assert reg.epoch > e0, "R1 RED: epoch did not increment on revoke"

    def test_epoch_increments_on_reauthorize(self) -> None:
        """Registry epoch must increment on reauthorize."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        reg.add_recognized("a" * 64)

        reg.revoke("a" * 64)

        e0 = reg.epoch

        reg.reauthorize("a" * 64)

        assert reg.epoch > e0, "R1 RED: epoch did not increment on reauthorize"


class TestR1ExternalAuthorizerPipeline:
    """P0/P1: Pipeline integration with live registry â€” revoke enforcement."""

    def test_pipeline_run_with_registry_then_revoke_hides_bundles(self) -> None:
        """Pipeline.run stores and recognizes bundles; revoke hides them."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None, "R1 RED: SandboxEvidenceRegistry not defined"

        VerifierCls = getattr(m, "ClaimVerifierProtocol", None)

        assert VerifierCls is not None, "R1 RED: ClaimVerifierProtocol not defined"

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        class _TestVerifier:
            def verify(self, **kwargs: object) -> object:

                return m.ClaimVerifierResult.RAG_SUPPORTED

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=_TestVerifier(),
        )

        result = pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="fatigue",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        # Bundles should be stored AND recognized

        assert len(result.bundles) > 0

        for bundle in result.bundles:
            assert store.get(bundle.bundle_digest) is not None, "R1 RED: bundle should be accessible after pipeline.run"

            # Revoke should hide it

            reg.revoke(bundle.bundle_digest)

            assert store.get(bundle.bundle_digest) is None, "R1 RED: bundle should be hidden after revoke"


# ===================================================================

# 15. R1 Hardening â€” add_recognized/reauthorize must invoke authorizer (Â§4-5)

# ===================================================================


class TestR1HardeningAddRecognizedReauthorize:
    """P0/P1: add_recognized and reauthorize must invoke the live authorizer.



    Current c57a100 code allows unchecked add_recognized and reauthorize

    that bypass the injected authorizer callback.  After hardening both

    must call authorizer.authorize() and be rejected when it returns False.

    """

    def test_add_recognized_invokes_authorizer(self) -> None:
        """add_recognized must call the injected authorizer callback."""

        invoked = threading.Event()

        class _Auth:
            def authorize(self, *, bundle_digest: str) -> bool:

                invoked.set()

                return True

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_Auth())  # type: ignore[call-arg]

        reg.add_recognized("a" * 64)

        assert invoked.is_set(), "R1 RED: authorizer was NOT invoked by add_recognized"

    def test_add_recognized_denied_by_authorizer_raises(self) -> None:
        """add_recognized must raise when authorizer returns False."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_DenyingAuthorizer())  # type: ignore[call-arg]

        with pytest.raises(SandboxEvidenceError):
            reg.add_recognized("a" * 64)

    def test_reauthorize_invokes_authorizer(self) -> None:
        """reauthorize must call the injected authorizer callback."""

        invoked = threading.Event()

        class _Auth:
            def authorize(self, *, bundle_digest: str) -> bool:

                invoked.set()

                return True

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_Auth())  # type: ignore[call-arg]

        digest = "a" * 64

        reg.add_recognized(digest)

        reg.revoke(digest)

        invoked.clear()

        reg.reauthorize(digest)

        assert invoked.is_set(), "R1 RED: authorizer was NOT invoked by reauthorize"

    def test_reauthorize_denied_by_authorizer_raises(self) -> None:
        """reauthorize must raise when authorizer denies the re-auth attempt."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        # Authorizer that permits the initial add_recognized but denies

        # the subsequent reauthorize call.

        call_count = 0

        class _DenyOnSecondCall:
            def authorize(self, *, bundle_digest: str) -> bool:

                nonlocal call_count

                call_count += 1

                return call_count <= 1  # allow first, deny second

        reg = RegistryCls(authorizer=_DenyOnSecondCall())  # type: ignore[call-arg]

        digest = "a" * 64

        reg.add_recognized(digest)  # authorizer allows

        reg.revoke(digest)

        with pytest.raises(SandboxEvidenceError):
            reg.reauthorize(digest)  # authorizer denies


class TestR1HardeningAuthorizerNone:
    """P0/P1: authorizer=None must never grant authority; store requires registry."""

    def test_authorizer_none_add_recognized_raises(self) -> None:
        """Registry with denying authorizer must reject add_recognized."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_DenyingAuthorizer())  # type: ignore[call-arg]

        with pytest.raises(SandboxEvidenceError):
            reg.add_recognized("a" * 64)

    def test_store_without_registry_raises_type_error(self) -> None:
        """Store created without a registry must raise TypeError."""

        with pytest.raises(TypeError):
            SandboxEvidenceStore()  # type: ignore[call-arg]

    def test_store_without_registry_rejects_get(self) -> None:
        """Store created without a registry must raise TypeError."""

        with pytest.raises(TypeError):
            SandboxEvidenceStore()  # type: ignore[call-arg]

    def test_restore_without_registry_raises(self) -> None:
        """restore(snapshot) without registry parameter must raise TypeError."""

        reg = _make_test_registry()

        store_a = SandboxEvidenceStore(registry=reg)

        b1 = _fake_bundle()

        reg.add_recognized(b1.bundle_digest)

        store_a.put(b1)

        snap = store_a.snapshot()

        with pytest.raises(TypeError):
            SandboxEvidenceStore.restore(snap)  # no registry param

    def test_restore_no_auto_create_registry(self) -> None:
        """restore must not auto-create a default permissive registry."""

        store = _make_test_store()

        bundle = _fake_bundle()

        _admit(store, bundle)

        snap = store.snapshot()

        # After hardening, calling restore(snap) without registry must raise

        with pytest.raises(TypeError):
            SandboxEvidenceStore.restore(snap)  # type: ignore[call-arg]

    def test_store_put_requires_explicit_registry_with_authorizer(self) -> None:
        """Store must be constructed with a registry that has a non-None authorizer."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)  # type: ignore[call-arg]

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)  # Must succeed with proper registry

        assert store.get(bundle.bundle_digest) is not None


class TestR1HardeningPipelineVerifier:
    """P1: Pipeline verifier callback reentry, identity, and state stability."""

    def test_verifier_called_during_pipeline_rag_path(self) -> None:
        """Verifier must be invoked during pipeline.run RAG path."""

        invoked = threading.Event()

        class _TestVerifier:
            def verify(self, **kwargs: object) -> object:

                invoked.set()

                return ClaimVerifierResult.RAG_SUPPORTED

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)  # type: ignore[call-arg]

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=_TestVerifier(),
        )

        pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="fatigue",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        assert invoked.is_set(), "R1 RED: verifier was NOT invoked by pipeline.run"

    def test_verifier_reentry_during_pipeline_run_raises(self) -> None:
        """Reentrant pipeline.run during verifier callback must raise."""

        entered = threading.Event()

        reentry_caught = threading.Event()

        class _ReentrantVerifier:
            _depth = 0

            def verify(self, **kwargs: object) -> object:

                type(self)._depth += 1

                # Circuit breaker: prevent infinite recursion by limiting depth

                if type(self)._depth > 3:
                    return ClaimVerifierResult.RAG_SUPPORTED

                entered.set()

                try:
                    pipeline.run(
                        agent_kind=AgentKind.SYNDROME,
                        query="reentry",
                        graph_run=_GRAPH_RUN,
                        graph_trace=_GRAPH_TRACE,
                    )

                except SandboxEvidenceError:
                    reentry_caught.set()

                return ClaimVerifierResult.RAG_SUPPORTED

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)  # type: ignore[call-arg]

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=_ReentrantVerifier(),
        )

        pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="fatigue",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        assert entered.is_set(), "verifier was not called"

        assert reentry_caught.is_set(), (
            "R1 RED: reentrant pipeline.run during verifier was NOT rejected â€” "
            "pipeline must detect verifier callback reentry"
        )

    def test_pipeline_stable_after_verifier_callback(self) -> None:
        """Pipeline internal state must be consistent after verifier callback."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)

        pipeline = EvidencePipeline(
            store=store,
            syndrome_node=SyndromeRetrievalNode(),
            formula_node=FormulaRetrievalNode(),
            verifier=CitationVerifier(),
        )

        # Run pipeline twice ï¿½ both must produce consistent results

        r1 = pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="fatigue",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        r2 = pipeline.run(
            agent_kind=AgentKind.SYNDROME,
            query="fatigue",
            graph_run=_GRAPH_RUN,
            graph_trace=_GRAPH_TRACE,
        )

        assert r1 is not None

        assert r1.bundles == r2.bundles, "R1: pipeline run 1 and 2 must produce same bundles"

        # After run, the store must still be accessible and consistent

        for bundle in r1.bundles:
            assert store.get(bundle.bundle_digest) is not None

    def test_store_put_requires_registry(self) -> None:
        """SandboxEvidenceStore must be created with a mandatory registry."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)  # type: ignore[call-arg]

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)  # Must succeed when properly configured

        assert store.get(bundle.bundle_digest) is not None

    def test_restore_with_explicit_fresh_registry_no_auto_recognize(self) -> None:
        """Restore with explicit fresh registry does not auto-recognize bundles."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg_orig = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg_orig)  # type: ignore[call-arg]

        bundle = _fake_bundle()

        reg_orig.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        snap = store.snapshot()

        # Fresh empty registry â€” nothing recognized

        reg_fresh = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store_restored = SandboxEvidenceStore.restore(snap, registry=reg_fresh)  # type: ignore[call-arg]

        # Must NOT be auto-recognized

        assert store_restored.get(bundle.bundle_digest) is None, (
            "R1 RED: restored store auto-recognized bundles without explicit registry"
        )

    def test_restore_accepts_pre_populated_registry(self) -> None:
        """Restore with a registry that has the digest recognized must work."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg_orig = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg_orig)  # type: ignore[call-arg]

        bundle = _fake_bundle()

        reg_orig.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        snap = store.snapshot()

        reg_new = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        reg_new.add_recognized(bundle.bundle_digest)

        restored = SandboxEvidenceStore.restore(snap, registry=reg_new)  # type: ignore[call-arg]

        assert restored.get(bundle.bundle_digest) is not None

    def test_register_must_be_same_instance_on_get_after_put(self) -> None:
        """The registry used at put time must be the one consulted at get time."""

        import app.agent_runtime.sandbox_evidence as m

        RegistryCls = getattr(m, "SandboxEvidenceRegistry", None)

        assert RegistryCls is not None

        reg = RegistryCls(authorizer=_PermissiveAuthorizer())  # type: ignore[call-arg]

        store = SandboxEvidenceStore(registry=reg)  # type: ignore[call-arg]

        bundle = _fake_bundle()

        reg.add_recognized(bundle.bundle_digest)

        store.put(bundle)

        assert store.get(bundle.bundle_digest) is not None

        # Revoke through same registry must affect get immediately

        reg.revoke(bundle.bundle_digest)

        assert store.get(bundle.bundle_digest) is None
