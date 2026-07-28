"""L7-SBX-FRESH Evidence/RAG offline reference composition.

This module implements a sandbox-only evidence and retrieval-augmented
generation (RAG) reference composition.  It is deliberately isolated from
the application runtime, HTTP, DB, models, and external services.

All data is fixed synthetic content — no real patient, clinical, or public
data is used or referenced.

Public contracts enumerated here must remain importable and independently
testable by ``tests/test_sandbox_evidence_l7.py``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from enum import Enum, StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Schema & resource constants
# ---------------------------------------------------------------------------

SANDBOX_EVIDENCE_SCHEMA_VERSION: Literal["sandbox-evidence.v1"] = "sandbox-evidence.v1"
SANDBOX_EVIDENCE_DISCLAIMER: Literal["sandbox_evidence_reference_only_not_a_production_source"] = (
    "sandbox_evidence_reference_only_not_a_production_source"
)

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class EvidencePacketLimit:
    """Resource limits for evidence packet collections."""

    MAX_ITEMS: int = 256
    MAX_SNIPPET_BYTES: int = 1024
    MAX_QUERY_BYTES: int = 8192


class EvidenceResultLimit:
    """Resource limits for single retrieval results."""

    MAX_ITEMS: int = 32


_CLAIM_LINK_LIMIT = 128
_MAX_SNAPSHOT_BYTES = 256 * 1024

# ---------------------------------------------------------------------------
# Enum types
# ---------------------------------------------------------------------------


class AgentKind(StrEnum):
    SYNDROME = "syndrome"
    FORMULA = "formula"


class SourceType(StrEnum):
    THEORY = "theory"
    CASE = "case"
    FORMULA = "formula"
    HERB = "herb"


class ClaimKind(StrEnum):
    SYNDROME_BASIS = "syndrome_basis"
    FORMULA_NAME = "formula_name"
    HERB = "herb"
    DOSAGE = "dosage"
    MODIFICATION_REASON = "modification_reason"


class FallbackPolicy(StrEnum):
    RAG_SUPPORTED = "rag_supported"
    MODEL_KNOWLEDGE_ONLY = "model_knowledge_only"
    HARD_BLOCK = "hard_block"


class ClaimVerifierResult(Enum):
    RAG_SUPPORTED = "rag_supported"
    MODEL_KNOWLEDGE_ONLY = "model_knowledge_only"
    HARD_BLOCK = "hard_block"


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class SandboxEvidenceError(ValueError):
    """A fixed, payload-free, chainless evidence-boundary failure.

    The message is the error code itself and must not contain query
    text, evidence content, claim text, exception payloads, keys,
    or signatures.
    """

    __slots__ = ()

    def __init__(self, code: str) -> None:
        super().__init__(code)


# ---------------------------------------------------------------------------
# Authority Protocols — injected live capabilities
# ---------------------------------------------------------------------------


@runtime_checkable
class SandboxEvidenceAuthorizer(Protocol):
    """Protocol for authorizer callback — injected into registry.

    Must return True to authorize an operation for the given bundle digest.
    """

    def authorize(self, *, bundle_digest: str) -> bool: ...


@runtime_checkable
class ClaimVerifierProtocol(Protocol):
    """Protocol for external claim verification — injected into pipeline.

    The module does NOT provide a default verifier that could be used as
    external authority.  Callers must inject their own verifier instance.
    """

    def verify(
        self,
        *,
        agent_kind: AgentKind,
        bundles: Mapping[str, SandboxEvidenceBundleV1] = {},
        claims: Sequence[SandboxEvidenceClaimV1] = (),
        links: Sequence[SandboxClaimEvidenceLinkV1] = (),
        fallback: FallbackPolicy = FallbackPolicy.RAG_SUPPORTED,
        allowed_retrieval_runs: tuple[str, ...] = (),
    ) -> ClaimVerifierResult: ...


# ---------------------------------------------------------------------------
# Strict frozen base model
# ---------------------------------------------------------------------------


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# DTOs — ordered by dependency
# ---------------------------------------------------------------------------


class SandboxRetrievalTraceV1(_StrictFrozenModel):
    """Deterministic retrieval trace tied to one graph run."""

    retrieval_run: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    graph_run: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    graph_trace: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)


class SandboxEvidencePacketV1(_StrictFrozenModel):
    """One frozen evidence packet with digest and trace."""

    evidence_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=256)
    chunk_id: str = Field(min_length=1, max_length=256)
    rank: int = Field(ge=1, le=EvidencePacketLimit.MAX_ITEMS)
    content_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    retrieval_trace: SandboxRetrievalTraceV1

    @model_validator(mode="after")
    def rank_is_exact(self) -> SandboxEvidencePacketV1:
        if int(self.rank) != self.rank:  # no fractional rank
            raise ValueError("rank must be an integer")
        return self


class SandboxEvidenceBundleV1(_StrictFrozenModel):
    """A sealed bundle of evidence packets with cumulative digests."""

    schema_version: Literal["sandbox-evidence.v1"]
    packets: tuple[SandboxEvidencePacketV1, ...] = Field(min_length=1, max_length=EvidencePacketLimit.MAX_ITEMS)
    bundle_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    content_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    graph_run: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    graph_trace: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    retrieval_run: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    node_name: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    disclaimer: Literal["sandbox_evidence_reference_only_not_a_production_source"] = SANDBOX_EVIDENCE_DISCLAIMER

    @model_validator(mode="after")
    def bundle_digest_is_derived(self) -> SandboxEvidenceBundleV1:
        expected = _derive_bundle_digest(
            content_digest=self.content_digest,
            retrieval_run=self.retrieval_run,
            node_name=self.node_name,
        )
        if self.bundle_digest != expected:
            raise ValueError("bundle_digest mismatch")
        return self

    @model_validator(mode="after")
    def content_digest_is_derived(self) -> SandboxEvidenceBundleV1:
        expected = _derive_content_digest(self.packets)
        if self.content_digest != expected:
            raise ValueError("content_digest mismatch")
        return self

    @model_validator(mode="after")
    def packets_have_matching_trace(self) -> SandboxEvidenceBundleV1:
        for p in self.packets:
            if p.retrieval_trace.retrieval_run != self.retrieval_run:
                raise ValueError("packet retrieval_run must match bundle")
        return self

    @model_validator(mode="after")
    def disclaimer_matches(self) -> SandboxEvidenceBundleV1:
        if self.disclaimer != SANDBOX_EVIDENCE_DISCLAIMER:
            raise ValueError("disclaimer mismatch")
        return self


class SandboxEvidenceClaimV1(_StrictFrozenModel):
    """One claim that may be linked to evidence."""

    claim_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    claim_kind: ClaimKind
    claim_text: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[str, ...] = Field(max_length=_CLAIM_LINK_LIMIT)


class SandboxClaimEvidenceLinkV1(_StrictFrozenModel):
    """Links one claim to one evidence packet in one bundle."""

    claim_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    evidence_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    bundle_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    content_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    retrieval_run: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)


class SandboxEvidenceResultV1(_StrictFrozenModel):
    """Result of one retrieval node call."""

    packets: tuple[SandboxEvidencePacketV1, ...] = Field(max_length=EvidenceResultLimit.MAX_ITEMS)
    bundles: tuple[SandboxEvidenceBundleV1, ...] = Field(max_length=EvidenceResultLimit.MAX_ITEMS)
    bundles_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)
    total: int = Field(ge=0)
    status: Literal["ok", "empty", "error"] = "ok"


class SandboxEvidenceContextV1(_StrictFrozenModel):
    """Context-data-tool projection for evidence — only the context layer."""

    evidence_packets: tuple[SandboxEvidencePacketV1, ...]
    bundles: tuple[SandboxEvidenceBundleV1, ...]
    bundle_digests: tuple[str, ...]
    context_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)


# ---------------------------------------------------------------------------
# EvidencePipeline result type
# ---------------------------------------------------------------------------


class EvidencePipelineResult:
    """Result of a full evidence pipeline run."""

    __slots__ = (
        "packets",
        "bundles",
        "result",
        "fallback",
        "context",
    )

    def __init__(
        self,
        *,
        packets: tuple[SandboxEvidencePacketV1, ...] = (),
        bundles: tuple[SandboxEvidenceBundleV1, ...] = (),
        result: ClaimVerifierResult = ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY,
        fallback: FallbackPolicy = FallbackPolicy.MODEL_KNOWLEDGE_ONLY,
        context: SandboxEvidenceContextV1 | None = None,
    ) -> None:
        self.packets = packets
        self.bundles = bundles
        self.result = result
        self.fallback = fallback
        self.context = context


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


class SandboxEvidenceStoreSnapshotV1(_StrictFrozenModel):
    """Canonical snapshot of an evidence store for deterministic replay."""

    data: str = Field(min_length=1)
    digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)


def _derive_content_digest(
    packets: tuple[SandboxEvidencePacketV1, ...],
) -> str:
    """Deterministic content digest from packet list."""
    raw = json.dumps(
        [p.model_dump(mode="json") for p in packets],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _derive_bundle_digest(
    *,
    content_digest: str,
    retrieval_run: str,
    node_name: str,
) -> str:
    return hashlib.sha256((content_digest + retrieval_run + node_name).encode("utf-8")).hexdigest()


def _canonical_store_bytes(value: object) -> bytes:
    """Encode to stable canonical JSON for store comparisons."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Source policy (task §6)
# ---------------------------------------------------------------------------


class SandboxSourcePolicy:
    """Closed-set source-type mappings per agent kind and claim kind."""

    _ALLOWED: dict[AgentKind, dict[ClaimKind, frozenset[SourceType]]] = {
        AgentKind.SYNDROME: {
            ClaimKind.SYNDROME_BASIS: frozenset({SourceType.THEORY, SourceType.CASE}),
        },
        AgentKind.FORMULA: {
            ClaimKind.FORMULA_NAME: frozenset({SourceType.FORMULA}),
            ClaimKind.HERB: frozenset({SourceType.HERB}),
            ClaimKind.DOSAGE: frozenset({SourceType.HERB}),
            ClaimKind.MODIFICATION_REASON: frozenset({SourceType.FORMULA, SourceType.HERB}),
        },
    }

    @classmethod
    def allowed_source_types(
        cls,
        agent_kind: AgentKind | str,
        claim_kind: ClaimKind | str,
    ) -> frozenset[SourceType]:
        """Return allowed source types for the given agent/claim kind.

        Raises SandboxEvidenceError for unknown agent or claim kind.
        """
        try:
            ak = agent_kind if isinstance(agent_kind, AgentKind) else AgentKind(agent_kind)
            ck = claim_kind if isinstance(claim_kind, ClaimKind) else ClaimKind(claim_kind)
        except (ValueError, LookupError):
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_SOURCE_POLICY_REJECTED") from None

        agent_map = cls._ALLOWED.get(ak)
        if agent_map is None:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_SOURCE_POLICY_REJECTED")
        allowed = agent_map.get(ck)
        if allowed is None:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_SOURCE_POLICY_REJECTED")
        return allowed

    @classmethod
    def is_source_allowed(
        cls,
        agent_kind: AgentKind,
        claim_kind: ClaimKind,
        source_type: SourceType,
    ) -> bool:
        """Check whether one source type is allowed for the given kind pair."""
        try:
            return source_type in cls.allowed_source_types(agent_kind, claim_kind)
        except SandboxEvidenceError:
            return False


# ---------------------------------------------------------------------------
# Fixed synthetic evidence bundles (offline, deterministic, byte-stable)
# ---------------------------------------------------------------------------

_SYNDROME_PACKETS: tuple[dict[str, Any], ...] = (
    {
        "evidence_id": "syn-ev-001",
        "source_type": "theory",
        "source_id": "theory-neijing-001",
        "chunk_id": "chunk-001",
        "content": "Deficiency of both qi and yin leads to lassitude, spontaneous sweating, and a weak pulse.",
    },
    {
        "evidence_id": "syn-ev-002",
        "source_type": "theory",
        "source_id": "theory-neijing-002",
        "chunk_id": "chunk-002",
        "content": "Spleen qi deficiency presents with fatigue, poor appetite, and loose stools.",
    },
    {
        "evidence_id": "syn-ev-003",
        "source_type": "case",
        "source_id": "case-jingui-001",
        "chunk_id": "chunk-003",
        "content": "Patient with liver depression and spleen deficiency responded to modified Xiao Yao San.",
    },
    {
        "evidence_id": "syn-ev-004",
        "source_type": "theory",
        "source_id": "theory-wenzhong-001",
        "chunk_id": "chunk-004",
        "content": "Kidney yin deficiency manifests as dry throat, night sweats, and a floating pulse.",
    },
    {
        "evidence_id": "syn-ev-005",
        "source_type": "theory",
        "source_id": "theory-dampness-001",
        "chunk_id": "chunk-005",
        "content": "Damp-heat in the liver channel causes bitter taste, "
        "rib-side distension, and greasy tongue coating.",
    },
)

_FORMULA_PACKETS: tuple[dict[str, Any], ...] = (
    {
        "evidence_id": "frm-ev-001",
        "source_type": "formula",
        "source_id": "formula-buzhong-yiqi",
        "chunk_id": "chunk-001",
        "content": "Bu Zhong Yi Qi Tang: Huang Qi, Ren Shen, Bai Zhu, Dang Gui, Chen Pi, Sheng Ma, Chai Hu, Gan Cao.",
    },
    {
        "evidence_id": "frm-ev-002",
        "source_type": "herb",
        "source_id": "herb-huangqi-001",
        "chunk_id": "chunk-002",
        "content": "Huang Qi (Astragalus membranaceus): Tonifies qi and raises yang. Typical dose 9-30g.",
    },
    {
        "evidence_id": "frm-ev-003",
        "source_type": "formula",
        "source_id": "formula-xiao-yao",
        "chunk_id": "chunk-003",
        "content": "Xiao Yao San: Chai Hu, Dang Gui, Bai Zhu, Fu Ling, Gan Cao, Bo He, Sheng Jiang.",
    },
    {
        "evidence_id": "frm-ev-004",
        "source_type": "herb",
        "source_id": "herb-danggui-001",
        "chunk_id": "chunk-004",
        "content": "Dang Gui (Angelica sinensis): Nourishes and activates blood. Typical dose 6-15g.",
    },
    {
        "evidence_id": "frm-ev-005",
        "source_type": "herb",
        "source_id": "herb-chenpi-001",
        "chunk_id": "chunk-005",
        "content": "Chen Pi (Citri Reticulatae Pericarpium): Regulates qi and dries dampness. Typical dose 3-9g.",
    },
)


def _build_syndrome_bundle(
    graph_run: str,
    graph_trace: str,
    retrieval_run: str,
    indices: Sequence[int] | None = None,
    max_items: int = EvidenceResultLimit.MAX_ITEMS,
) -> SandboxEvidenceBundleV1:
    """Build a deterministic Syndrome retrieval bundle."""
    if indices is None:
        indices = range(min(len(_SYNDROME_PACKETS), max_items))
    packets: list[SandboxEvidencePacketV1] = []
    for rank, idx in enumerate(indices, start=1):
        raw = _SYNDROME_PACKETS[idx]
        content_digest = hashlib.sha256(raw["content"].encode("utf-8")).hexdigest()
        packets.append(
            SandboxEvidencePacketV1(
                evidence_id=raw["evidence_id"],
                source_type=SourceType(raw["source_type"]),
                source_id=raw["source_id"],
                chunk_id=raw["chunk_id"],
                rank=rank,
                content_digest=content_digest,
                retrieval_trace=SandboxRetrievalTraceV1(
                    retrieval_run=retrieval_run,
                    graph_run=graph_run,
                    graph_trace=graph_trace,
                ),
            )
        )
    packet_tuple = tuple(packets)
    content_digest = _derive_content_digest(packet_tuple)
    node_name = "syndrome_retrieval"
    bundle_digest = _derive_bundle_digest(
        content_digest=content_digest,
        retrieval_run=retrieval_run,
        node_name=node_name,
    )
    return SandboxEvidenceBundleV1(
        schema_version=SANDBOX_EVIDENCE_SCHEMA_VERSION,
        packets=packet_tuple,
        bundle_digest=bundle_digest,
        content_digest=content_digest,
        graph_run=graph_run,
        graph_trace=graph_trace,
        retrieval_run=retrieval_run,
        node_name=node_name,
        disclaimer=SANDBOX_EVIDENCE_DISCLAIMER,
    )


def _build_formula_bundle(
    graph_run: str,
    graph_trace: str,
    retrieval_run: str,
    indices: Sequence[int] | None = None,
    max_items: int = EvidenceResultLimit.MAX_ITEMS,
) -> SandboxEvidenceBundleV1:
    """Build a deterministic Formula retrieval bundle."""
    if indices is None:
        indices = range(min(len(_FORMULA_PACKETS), max_items))
    packets: list[SandboxEvidencePacketV1] = []
    for rank, idx in enumerate(indices, start=1):
        raw = _FORMULA_PACKETS[idx]
        content_digest = hashlib.sha256(raw["content"].encode("utf-8")).hexdigest()
        packets.append(
            SandboxEvidencePacketV1(
                evidence_id=raw["evidence_id"],
                source_type=SourceType(raw["source_type"]),
                source_id=raw["source_id"],
                chunk_id=raw["chunk_id"],
                rank=rank,
                content_digest=content_digest,
                retrieval_trace=SandboxRetrievalTraceV1(
                    retrieval_run=retrieval_run,
                    graph_run=graph_run,
                    graph_trace=graph_trace,
                ),
            )
        )
    packet_tuple = tuple(packets)
    content_digest = _derive_content_digest(packet_tuple)
    node_name = "formula_retrieval"
    bundle_digest = _derive_bundle_digest(
        content_digest=content_digest,
        retrieval_run=retrieval_run,
        node_name=node_name,
    )
    return SandboxEvidenceBundleV1(
        schema_version=SANDBOX_EVIDENCE_SCHEMA_VERSION,
        packets=packet_tuple,
        bundle_digest=bundle_digest,
        content_digest=content_digest,
        graph_run=graph_run,
        graph_trace=graph_trace,
        retrieval_run=retrieval_run,
        node_name=node_name,
        disclaimer=SANDBOX_EVIDENCE_DISCLAIMER,
    )


# ---------------------------------------------------------------------------
# Offline retrieval nodes
# ---------------------------------------------------------------------------


class SyndromeRetrievalNode:
    """Offline syndrome retrieval node — deterministic, no model/network/DB."""

    def retrieve(
        self,
        *,
        query: str,
        graph_run: str,
        graph_trace: str,
        max_results: int = EvidenceResultLimit.MAX_ITEMS,
    ) -> SandboxEvidenceResultV1:
        """Retrieve syndrome evidence for the given query."""

        if len(query.encode("utf-8")) > EvidencePacketLimit.MAX_QUERY_BYTES:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_LIMIT_EXCEEDED")
        if not 1 <= max_results <= EvidenceResultLimit.MAX_ITEMS:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_LIMIT_EXCEEDED")

        retrieval_run = _derive_retrieval_run(graph_run, "syndrome_retrieval")
        count = min(len(_SYNDROME_PACKETS), max_results)
        bundle = _build_syndrome_bundle(
            graph_run=graph_run,
            graph_trace=graph_trace,
            retrieval_run=retrieval_run,
            indices=range(count),
            max_items=max_results,
        )
        packets = bundle.packets
        bundles_digest = hashlib.sha256(bundle.bundle_digest.encode("utf-8")).hexdigest()
        return SandboxEvidenceResultV1(
            packets=packets,
            bundles=(bundle,),
            bundles_digest=bundles_digest,
            total=len(packets),
            status="ok",
        )


class FormulaRetrievalNode:
    """Offline formula retrieval node — deterministic, no model/network/DB."""

    def retrieve(
        self,
        *,
        query: str,
        graph_run: str,
        graph_trace: str,
        max_results: int = EvidenceResultLimit.MAX_ITEMS,
    ) -> SandboxEvidenceResultV1:
        """Retrieve formula evidence for the given query."""

        if len(query.encode("utf-8")) > EvidencePacketLimit.MAX_QUERY_BYTES:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_LIMIT_EXCEEDED")
        if not 1 <= max_results <= EvidenceResultLimit.MAX_ITEMS:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_LIMIT_EXCEEDED")

        retrieval_run = _derive_retrieval_run(graph_run, "formula_retrieval")
        count = min(len(_FORMULA_PACKETS), max_results)
        bundle = _build_formula_bundle(
            graph_run=graph_run,
            graph_trace=graph_trace,
            retrieval_run=retrieval_run,
            indices=range(count),
            max_items=max_results,
        )
        packets = bundle.packets
        bundles_digest = hashlib.sha256(bundle.bundle_digest.encode("utf-8")).hexdigest()
        return SandboxEvidenceResultV1(
            packets=packets,
            bundles=(bundle,),
            bundles_digest=bundles_digest,
            total=len(packets),
            status="ok",
        )


def _derive_retrieval_run(graph_run: str, node_name: str) -> str:
    """Derive a deterministic retrieval run identity from graph run + node."""
    raw = f"{graph_run}:{node_name}"
    return "retrieval-run-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# SandboxEvidenceRegistry — live authority with monotonic epoch
# ---------------------------------------------------------------------------


class SandboxEvidenceRegistry:
    """Live bundle registry with monotonic epoch-based authorization.

    Tracks recognized bundle digests, supports revoke/reauthorize, and
    optionally accepts an injected SandboxEvidenceAuthorizer callback.
    The epoch increments on every state transition and cannot be reverted.
    """

    __slots__ = ("_lock", "_recognized", "_reauthorizable", "_epoch", "_authorizer", "_reentry_guard", "_poisoned")

    def __init__(self, authorizer: SandboxEvidenceAuthorizer) -> None:
        self._lock = threading.RLock()
        self._recognized: dict[str, int] = {}  # digest -> epoch when recognized
        self._reauthorizable: set[str] = set()  # digests ever recognized
        self._epoch = 0
        self._authorizer = authorizer
        self._reentry_guard = 0
        self._poisoned = False

    # -- Callback canonical state seal (R2: _StateSeal) ------------------------

    def _capture_callback_context(self) -> dict[str, Any]:
        """Capture identity/state snapshot before an untrusted authorizer callback."""
        seal = _StateSeal()
        seal.capture(
            _recognized=self._recognized,
            _reauthorizable=self._reauthorizable,
            _epoch=self._epoch,
            _reentry_guard=self._reentry_guard,
        )
        return {
            "_lock": self._lock,
            "_authorizer": self._authorizer,
            "_recognized": self._recognized,
            "_reauthorizable": self._reauthorizable,
            "_seal": seal,
        }

    def _verify_callback_context(self, ctx: dict[str, Any]) -> None:
        """Verify post-callback state matches pre-callback snapshot.

        On identity or state drift, restore safe invariants where possible,
        poison the instance, and raise fail-closed with ``SandboxEvidenceError``.
        """
        if self._lock is not ctx["_lock"]:
            self._lock = ctx["_lock"]
            self._poisoned = True
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._authorizer is not ctx["_authorizer"]:
            self._authorizer = ctx["_authorizer"]
            self._poisoned = True
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._recognized is not ctx["_recognized"]:
            self._recognized = ctx["_recognized"]
            self._poisoned = True
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._reauthorizable is not ctx["_reauthorizable"]:
            self._reauthorizable = ctx["_reauthorizable"]
            self._poisoned = True
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        # Canonical state seal — catches same-size mutations
        try:
            ctx["_seal"].verify(
                _recognized=self._recognized,
                _reauthorizable=self._reauthorizable,
                _epoch=self._epoch,
                _reentry_guard=self._reentry_guard,
            )
        except SandboxEvidenceError:
            ctx["_seal"].restore(self)
            raise

    @property
    def epoch(self) -> int:
        """Current monotonic epoch — increments on each state change."""
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            return self._epoch

    def recognize(self, bundle_digest: str) -> bool:
        """Check whether *bundle_digest* is currently recognized.

        The injected authorizer is always consulted.  If it returns False
        the digest is treated as unrecognised regardless of internal state.
        A reentrant call (detected by the caller's reentry guard) raises
        SandboxEvidenceError.
        """
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            ctx = self._capture_callback_context()
            try:
                auth_result = self._authorizer.authorize(bundle_digest=bundle_digest)
            except Exception:
                self._verify_callback_context(ctx)
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE") from None
            self._verify_callback_context(ctx)
            if not auth_result:
                return False
            return bundle_digest in self._recognized

    def add_recognized(self, bundle_digest: str) -> None:
        """Add *bundle_digest* to the recognized set after authorizer approval.

        The injected authorizer is always consulted.  If it returns False
        the operation is rejected with SandboxEvidenceError.
        The epoch is incremented on each successful state change.
        """
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            ctx = self._capture_callback_context()
            try:
                auth_ok = self._authorizer.authorize(bundle_digest=bundle_digest)
            except Exception:
                self._verify_callback_context(ctx)
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE") from None
            self._verify_callback_context(ctx)
            if not auth_ok:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")
            self._epoch += 1
            self._recognized[bundle_digest] = self._epoch
            self._reauthorizable.add(bundle_digest)

    def revoke(self, bundle_digest: str) -> None:
        """Remove *bundle_digest* from the recognized set.

        The digest is kept in the reauthorizable set so it can be
        restored with reauthorize().  No-op if not currently recognized.
        """
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            self._epoch += 1
            self._recognized.pop(bundle_digest, None)

    def reauthorize(self, bundle_digest: str) -> bool:
        """Re-authorize a previously recognized digest.

        The injected authorizer is always consulted again.  If it returns
        False the operation is rejected with SandboxEvidenceError.
        Returns True if the digest was revoked and is now re-recognized.
        Raises SandboxEvidenceError if the digest was never recognized
        or the authorizer denies.
        Returns False (without raising) if the digest is already recognized.
        """
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            if bundle_digest in self._recognized:
                return False
            if bundle_digest not in self._reauthorizable:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")
            ctx = self._capture_callback_context()
            try:
                auth_ok = self._authorizer.authorize(bundle_digest=bundle_digest)
            except Exception:
                self._verify_callback_context(ctx)
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE") from None
            self._verify_callback_context(ctx)
            if not auth_ok:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")
            self._epoch += 1
            self._recognized[bundle_digest] = self._epoch
            return True


# ---------------------------------------------------------------------------
# Citation Verifier (task §5 item 7)
# ---------------------------------------------------------------------------


class CitationVerifier:
    """Verifies evidence chains for citation integrity.

    Checks evidence ID existence, source type policy, current-run
    visibility, content/link digest consistency, and claim-link
    completeness.
    """

    __slots__ = ()

    def verify(
        self,
        *,
        agent_kind: AgentKind,
        bundles: Mapping[str, SandboxEvidenceBundleV1] = {},
        claims: Sequence[SandboxEvidenceClaimV1] = (),
        links: Sequence[SandboxClaimEvidenceLinkV1] = (),
        fallback: FallbackPolicy = FallbackPolicy.RAG_SUPPORTED,
        allowed_retrieval_runs: tuple[str, ...] = (),
    ) -> ClaimVerifierResult:
        """Verify citation integrity.

        Returns the verifier result indicating whether the evidence is
        supported, only model-knowledge, or hard-blocked.
        """
        if fallback is FallbackPolicy.HARD_BLOCK:
            return ClaimVerifierResult.HARD_BLOCK

        if fallback is FallbackPolicy.MODEL_KNOWLEDGE_ONLY:
            if bundles or claims or links:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_CLAIM_COMPLETENESS_REJECTED")
            return ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

        # RAG supported path — verify evidence integrity
        if not claims or not links:
            return ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY

        # Build a lookup of evidence_id -> packet for fast access
        evidence_map: dict[str, SandboxEvidencePacketV1] = {}
        for bundle in bundles.values():
            for packet in bundle.packets:
                evidence_map[packet.evidence_id] = packet

        # Build a lookup of claim_id -> claim
        claim_map: dict[str, SandboxEvidenceClaimV1] = {}
        for claim in claims:
            claim_map[claim.claim_id] = claim

        # Build a lookup of (claim_id, evidence_id) -> link
        link_map: dict[tuple[str, str], SandboxClaimEvidenceLinkV1] = {}
        for link in links:
            link_map[(link.claim_id, link.evidence_id)] = link

        # Check every claim has at least one valid link
        all_claims_linked = True
        for claim in claims:
            has_valid_link = False
            for ev_id in claim.evidence_ids:
                found_link = link_map.get((claim.claim_id, ev_id))
                if found_link is None:
                    continue
                found_packet = evidence_map.get(ev_id)
                if found_packet is None:
                    continue

                # Source type policy check
                if not SandboxSourcePolicy.is_source_allowed(agent_kind, claim.claim_kind, found_packet.source_type):
                    return ClaimVerifierResult.HARD_BLOCK

                # Run visibility check
                if allowed_retrieval_runs and found_link.retrieval_run not in allowed_retrieval_runs:
                    continue

                # Content digest check
                if found_link.content_digest != found_packet.content_digest:
                    continue

                # Bundle existence check
                found_bundle = bundles.get(found_link.bundle_digest)
                if found_bundle is None:
                    continue

                has_valid_link = True
                break

            if not has_valid_link:
                all_claims_linked = False
                break

        if all_claims_linked:
            return ClaimVerifierResult.RAG_SUPPORTED

        return ClaimVerifierResult.MODEL_KNOWLEDGE_ONLY


# ---------------------------------------------------------------------------
# SandboxEvidenceStore — append-only, idempotent, snapshot/restore
# ---------------------------------------------------------------------------


class SandboxEvidenceStore:
    """In-memory, append-only evidence store with canonical snapshot/restore.

    Key properties:
    - put/get by bundle_digest
    - Same key + same bytes: idempotent (silent no-op)
    - Same key + different bytes: rejected
    - Canonical snapshot/restore for deterministic replay
    - Graph-run-scoped visibility filtering
    - Optional live registry for authorization (when *registry* is provided)
    """

    __slots__ = ("_lock", "_bundles", "_sealed", "_registry", "_reentry_guard", "_poisoned")

    def __init__(self, registry: SandboxEvidenceRegistry) -> None:
        self._lock = threading.RLock()
        self._bundles: dict[str, bytes] = {}
        self._sealed = False
        self._registry = registry
        self._reentry_guard = 0
        self._poisoned = False

    @property
    def registry(self) -> SandboxEvidenceRegistry:
        """Expose the injected registry."""
        return self._registry

    # -- Callback canonical state seal (R2: _StateSeal) ------------------------

    def _capture_callback_context(self) -> dict[str, Any]:
        """Capture identity/state snapshot for pipeline-level callback sealing."""
        seal = _StateSeal()
        seal.capture(
            _bundles=self._bundles,
            _sealed=self._sealed,
            _reentry_guard=self._reentry_guard,
        )
        ctx: dict[str, Any] = {
            "_lock": self._lock,
            "_registry": self._registry,
            "_bundles": self._bundles,
            "_seal": seal,
        }
        if self._registry is not None:
            ctx["_registry_ctx"] = self._registry._capture_callback_context()
        return ctx

    def _verify_callback_context(self, ctx: dict[str, Any]) -> None:
        """Verify post-callback state matches pre-callback snapshot.

        On identity or state drift, restore safe invariants where possible,
        poison the instance, and raise fail-closed with ``SandboxEvidenceError``.
        """
        if self._lock is not ctx["_lock"]:
            self._lock = ctx["_lock"]
            self._poisoned = True
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._registry is not ctx["_registry"]:
            self._registry = ctx["_registry"]
            self._poisoned = True
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._bundles is not ctx["_bundles"]:
            self._bundles = ctx["_bundles"]
            self._poisoned = True
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        # Canonical state seal — catches same-size mutations
        try:
            ctx["_seal"].verify(
                _bundles=self._bundles,
                _sealed=self._sealed,
                _reentry_guard=self._reentry_guard,
            )
        except SandboxEvidenceError:
            ctx["_seal"].restore(self)
            raise
        if self._registry is not None:
            reg_ctx = ctx.get("_registry_ctx")
            if reg_ctx is not None:
                self._registry._verify_callback_context(reg_ctx)

    def put(self, bundle: SandboxEvidenceBundleV1) -> None:
        """Store a bundle idempotently.

        Raises SandboxEvidenceError if bundle_digest key exists with
        different content, or if the store is sealed.
        When a registry is configured, the bundle digest must be
        recognized before storing.
        """
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            if self._reentry_guard:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")
            if self._sealed:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            _check_exact_type(bundle, SandboxEvidenceBundleV1)

            self._reentry_guard += 1
            _store_seal = _StateSeal()
            _store_seal.capture(
                _bundles=self._bundles,
                _sealed=self._sealed,
                _reentry_guard=self._reentry_guard,
            )
            try:
                try:
                    _auth_result = self._registry.recognize(bundle.bundle_digest)
                except SandboxEvidenceError:
                    _store_seal_verify_or_restore(_store_seal, self)
                    raise
                except Exception:
                    _store_seal_verify_or_restore(_store_seal, self)
                    raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE") from None
                _store_seal_verify_or_restore(_store_seal, self)
                if not _auth_result:
                    raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")
            finally:
                self._reentry_guard -= 1

            key = bundle.bundle_digest
            raw = _canonical_store_bytes(bundle.model_dump(mode="json"))

            existing = self._bundles.get(key)
            if existing is not None:
                if existing != raw:
                    raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
                return  # idempotent silent no-op

            self._bundles[key] = raw

    def get(self, bundle_digest: str) -> SandboxEvidenceBundleV1 | None:
        """Retrieve a bundle by its digest, or None if missing/unauthorized."""
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            if self._reentry_guard:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")

            self._reentry_guard += 1
            _store_seal = _StateSeal()
            _store_seal.capture(
                _bundles=self._bundles,
                _sealed=self._sealed,
                _reentry_guard=self._reentry_guard,
            )
            try:
                try:
                    _recognized = self._registry.recognize(bundle_digest)
                except SandboxEvidenceError:
                    _store_seal_verify_or_restore(_store_seal, self)
                    raise
                except Exception:
                    _store_seal_verify_or_restore(_store_seal, self)
                    raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE") from None
                _store_seal_verify_or_restore(_store_seal, self)
                if not _recognized:
                    return None
            finally:
                self._reentry_guard -= 1

            raw = self._bundles.get(bundle_digest)
            if raw is None:
                return None
            try:
                return SandboxEvidenceBundleV1.model_validate_json(raw)
            except Exception:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE") from None

    def get_bundles_for_retrieval_run(self, retrieval_run: str) -> tuple[SandboxEvidenceBundleV1, ...]:
        """Return all bundles visible for the given retrieval run."""
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            result: list[SandboxEvidenceBundleV1] = []
            for raw in self._bundles.values():
                try:
                    bundle = SandboxEvidenceBundleV1.model_validate_json(raw)
                    if bundle.retrieval_run == retrieval_run:
                        self._reentry_guard += 1
                        _seal = _StateSeal()
                        _seal.capture(
                            _bundles=self._bundles,
                            _sealed=self._sealed,
                            _reentry_guard=self._reentry_guard,
                        )
                        try:
                            try:
                                _ok = self._registry.recognize(bundle.bundle_digest)
                            except SandboxEvidenceError:
                                _store_seal_verify_or_restore(_seal, self)
                                raise
                            except Exception:
                                _store_seal_verify_or_restore(_seal, self)
                                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE") from None
                            _store_seal_verify_or_restore(_seal, self)
                        finally:
                            self._reentry_guard -= 1
                        if _ok:
                            result.append(bundle)
                except SandboxEvidenceError as _se:
                    if _is_integrity_error(_se):
                        raise
                except Exception:
                    continue
            return tuple(result)

    def get_bundles_for_graph_run(self, graph_run: str) -> tuple[SandboxEvidenceBundleV1, ...]:
        """Return all bundles visible for the given graph run."""
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            result: list[SandboxEvidenceBundleV1] = []
            for raw in self._bundles.values():
                try:
                    bundle = SandboxEvidenceBundleV1.model_validate_json(raw)
                    if bundle.graph_run == graph_run:
                        self._reentry_guard += 1
                        _seal = _StateSeal()
                        _seal.capture(
                            _bundles=self._bundles,
                            _sealed=self._sealed,
                            _reentry_guard=self._reentry_guard,
                        )
                        try:
                            _ok = self._registry.recognize(bundle.bundle_digest)
                            _seal.verify(
                                _bundles=self._bundles,
                                _sealed=self._sealed,
                                _reentry_guard=self._reentry_guard,
                            )
                        except SandboxEvidenceError:
                            _seal.restore(self)
                            raise
                        finally:
                            self._reentry_guard -= 1
                        if _ok:
                            result.append(bundle)
                except SandboxEvidenceError as _se:
                    if "INTEGRITY_FAILURE" in str(_se):
                        raise
                except Exception:
                    continue
            return tuple(result)

    def snapshot(self) -> SandboxEvidenceStoreSnapshotV1:
        """Create a canonical snapshot of the current store state."""
        with self._lock:
            if self._poisoned:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
            if self._reentry_guard:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")
            raw = json.dumps(
                [json.loads(v) for v in self._bundles.values()],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(raw.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_LIMIT_EXCEEDED")
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            return SandboxEvidenceStoreSnapshotV1(data=raw, digest=digest)

    @classmethod
    def restore(
        cls,
        snapshot: SandboxEvidenceStoreSnapshotV1,
        *,
        registry: SandboxEvidenceRegistry,
    ) -> SandboxEvidenceStore:
        """Restore a store from a canonical snapshot.

        The snapshot digest is verified to ensure data integrity.
        The caller MUST provide a live registry.  The restored store
        does NOT auto-recognise any bundles — the caller is responsible
        for populating the registry with the appropriate digests.
        """
        _check_exact_type(snapshot, SandboxEvidenceStoreSnapshotV1)
        if snapshot.digest != hashlib.sha256(snapshot.data.encode("utf-8")).hexdigest():
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")

        store = cls(registry=registry)
        raw_bundles: list[dict[str, Any]] = json.loads(snapshot.data)
        for raw in raw_bundles:
            try:
                # Re-serialize and use model_validate_json to handle
                # strict StrEnum conversion from JSON strings
                raw_json = json.dumps(
                    raw,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                bundle = SandboxEvidenceBundleV1.model_validate_json(raw_json)
            except Exception:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE") from None
            store._bundles[bundle.bundle_digest] = _canonical_store_bytes(bundle.model_dump(mode="json"))
        return store


def _check_exact_type(value: object, expected: type) -> None:
    """Reject subclasses and non-exact type instances."""
    if type(value) is not expected:
        raise SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")


# ---------------------------------------------------------------------------
# _StateSeal — single shared canonical state seal for callback boundaries
# ---------------------------------------------------------------------------

# Schema version for canonical state domains — any change breaks all prior digests
_STATE_SEAL_SCHEMA: bytes = b"se.v3|"

# Sentinel for structured integrity-failure detection (no str()/repr())
_INTEGRITY_ERROR_CODE: str = "SANDBOX_EVIDENCE_INTEGRITY_FAILURE"


def _is_integrity_error(exc: SandboxEvidenceError) -> bool:
    """Check integrity failure via exact error-code comparison, no string conversion."""
    return bool(exc.args and exc.args[0] is _INTEGRITY_ERROR_CODE)


def _seal_hex(data: bytes) -> bytes:
    """Hex-encode bytes for injective canonical encoding (no delimiter ambiguity)."""
    return data.hex().encode("ascii")


def _canonical_encode_int(v: int) -> bytes:
    """Deterministic hex encoding of exact int (never bool) for digest.

    Safe for exact built-in int.  Never calls str(), repr(), or external
    methods.  Only called after type(v) is int and type(v) is not bool
    has been confirmed.
    """
    if v >= 0:
        return b"+" + _seal_hex(v.to_bytes(max((v.bit_length() + 7) // 8, 1), "big", signed=False))
    neg = -v
    return b"-" + _seal_hex(neg.to_bytes(max((neg.bit_length() + 7) // 8, 1), "big", signed=False))


def _encode_canonical_field(h: hashlib._Hash, name: str, value: object) -> None:
    """Append one field's canonical form with injective domain-separated encoding.

    All variable-length data uses hex encoding to prevent delimiter ambiguity.
    Exact built-in type validation happens before any iteration or sorting
    that could dispatch untrusted hooks.

    Only exact built-in types are accepted.  Never calls __str__, __repr__,
    __eq__, __hash__, __lt__, properties, Pydantic serializers/validators, or
    external callbacks.
    """
    _check_exact_type(name, str)
    h.update(name.encode("utf-8"))
    h.update(b"=")
    if type(value) is dict:
        # Must be dict[str, int] or dict[str, bytes]
        # Validate ALL keys/values as exact types BEFORE any sorting
        has_int_val = False
        has_bytes_val = False
        for k in value:
            _check_exact_type(k, str)
            v = value[k]
            if type(v) is int:
                has_int_val = True
            elif type(v) is bytes:
                has_bytes_val = True
            else:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")
            if has_int_val and has_bytes_val:
                raise SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")
        # Type tag for domain separation
        if has_bytes_val:
            h.update(b"SB:{")
            for k in sorted(value):
                v = value[k]
                h.update(_seal_hex(k.encode("utf-8")))
                h.update(b":")
                h.update(_seal_hex(v))
                h.update(b",")
        else:
            h.update(b"SI:{")
            for k in sorted(value):
                v = value[k]
                h.update(_seal_hex(k.encode("utf-8")))
                h.update(b":")
                h.update(_canonical_encode_int(v))
                h.update(b",")
        h.update(b"}")
    elif type(value) is set:
        # Must be set[str] — validate all items before sorting
        for item in value:
            _check_exact_type(item, str)
        h.update(b"SS:(")
        for item in sorted(value):
            h.update(_seal_hex(item.encode("utf-8")))
            h.update(b",")
        h.update(b")")
    elif type(value) is bool:
        h.update(b"B:")
        h.update(b"T" if value else b"F")
    elif type(value) is int:
        h.update(b"I:")
        h.update(_canonical_encode_int(value))
    else:
        raise SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")

def _store_seal_verify_or_restore(seal: _StateSeal, store: SandboxEvidenceStore) -> None:
    """Verify store-level seal and restore+poison on mismatch.

    Used as a finally-style guard after any registry callback that may have
    contaminated store state.  Not to be confused with _StateSeal.restore().
    """
    try:
        seal.verify(
            _bundles=store._bundles,
            _sealed=store._sealed,
            _reentry_guard=store._reentry_guard,
        )
    except SandboxEvidenceError:
        seal.restore(store)
        raise


class _StateSeal:
    """Single shared canonical state seal for callback boundary protection.

    Captures type-checked exact state, computes deterministic SHA-256 digest,
    and verifies no drift after untrusted callback.  On drift, restores saved
    pre-callback state and raises.

    This is THE shared primitive used uniformly by Registry, Store, and
    Pipeline.  No class maintains its own ad-hoc length/exception table.

    The seal operates only on exact built-in types (dict, set, int, bool, str,
    bytes).  Never calls __str__, __repr__, __eq__, __hash__, properties,
    Pydantic serializers/validators, or any authorizer/verifier method.
    """

    __slots__ = ("_pre", "_digest")

    def __init__(self) -> None:
        self._pre: dict[str, object] = {}
        self._digest: str = ""

    def capture(self, **fields: object) -> str:
        """Capture pre-callback state, save trusted copies, compute digest.

        Returns the hex digest.
        """
        h = hashlib.sha256(_STATE_SEAL_SCHEMA)
        self._pre = {}
        for name in sorted(fields):
            h.update(b"|")
            value = fields[name]
            if type(value) is dict:
                self._pre[str(name)] = dict(value)
            elif type(value) is set:
                self._pre[str(name)] = set(value)
            else:
                self._pre[str(name)] = value  # immutable
            _encode_canonical_field(h, name, value)
        self._digest = h.hexdigest()
        return self._digest

    def verify(self, **fields: object) -> str:
        """Verify current state matches captured digest.

        Returns current digest on match.
        Raises SandboxEvidenceError on mismatch.
        """
        h = hashlib.sha256(_STATE_SEAL_SCHEMA)
        for name in sorted(fields):
            h.update(b"|")
            _encode_canonical_field(h, name, fields[name])
        cur = h.hexdigest()
        if cur != self._digest:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        return cur

    def restore(self, target: object) -> None:
        """Restore pre-capture state copies on *target* and poison."""
        for name_str, pre_val in self._pre.items():
            if hasattr(target, name_str):
                setattr(target, name_str, pre_val)
        if hasattr(target, "_poisoned"):
            target._poisoned = True

    @property
    def digest(self) -> str:
        return self._digest


# ---------------------------------------------------------------------------
# EvidencePipeline — retrieve → store → verify → context projection
# ---------------------------------------------------------------------------


class EvidencePipeline:
    """Orchestrates the full evidence pipeline.

    retrieve → store → verify → context projection.

    The no-RAG branch skips retrieval and verification entirely.
    """

    __slots__ = (
        "_store",
        "_syndrome_node",
        "_formula_node",
        "_verifier",
        "_lock",
        "_registry",
        "_reentry_guard",
        "_poisoned",
    )

    def __init__(
        self,
        *,
        store: SandboxEvidenceStore,
        syndrome_node: SyndromeRetrievalNode,
        formula_node: FormulaRetrievalNode,
        verifier: ClaimVerifierProtocol,
        registry: SandboxEvidenceRegistry | None = None,
    ) -> None:
        _check_exact_type(store, SandboxEvidenceStore)
        _check_exact_type(syndrome_node, SyndromeRetrievalNode)
        _check_exact_type(formula_node, FormulaRetrievalNode)
        if not isinstance(verifier, ClaimVerifierProtocol):
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")
        self._store = store
        self._syndrome_node = syndrome_node
        self._formula_node = formula_node
        self._verifier = verifier
        self._lock = threading.RLock()
        self._registry = registry  # None = use store's registry (if any)
        self._reentry_guard = 0
        self._poisoned = False

    def _get_active_registry(self) -> SandboxEvidenceRegistry | None:
        """Resolve the effective registry — pipeline-level or store-level."""
        return self._registry if self._registry is not None else self._store.registry

    # -- Callback canonical state seal (R2: _StateSeal) ------------------------

    def _capture_callback_context(self) -> dict[str, Any]:
        """Capture identity/state snapshot before an untrusted verifier callback."""
        store_seal = _StateSeal()
        store_seal.capture(
            _bundles=self._store._bundles,
            _sealed=self._store._sealed,
            _reentry_guard=self._store._reentry_guard,
        )
        ctx: dict[str, Any] = {
            "_store": self._store,
            "_verifier": self._verifier,
            "_registry": self._registry,
            "_lock": self._lock,
            "_reentry_guard": self._reentry_guard,
            "_store_seal": store_seal,
        }
        # Capture registry internals for deep state-drift detection
        reg = self._get_active_registry()
        if reg is not None:
            ctx["_registry_ctx"] = reg._capture_callback_context()
        return ctx

    def _verify_callback_context(self, ctx: dict[str, Any]) -> None:
        """Verify post-callback state matches pre-callback snapshot.

        On identity or state mismatch, restore safe invariants where possible
        and raise fail-closed with ``SandboxEvidenceError``.
        """
        if self._store is not ctx["_store"]:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._verifier is not ctx["_verifier"]:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._registry is not ctx["_registry"]:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._lock is not ctx["_lock"]:
            self._lock = ctx["_lock"]
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        if self._reentry_guard != ctx["_reentry_guard"]:
            self._reentry_guard = ctx["_reentry_guard"]
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_INTEGRITY_FAILURE")
        # Store-level canonical state seal
        try:
            ctx["_store_seal"].verify(
                _bundles=self._store._bundles,
                _sealed=self._store._sealed,
                _reentry_guard=self._store._reentry_guard,
            )
        except SandboxEvidenceError:
            ctx["_store_seal"].restore(self._store)
            raise
        # Registry delegation
        reg_ctx = ctx.get("_registry_ctx")
        if reg_ctx is not None:
            reg = self._get_active_registry()
            if reg is not None:
                    try:
                        reg._verify_callback_context(reg_ctx)
                    except SandboxEvidenceError:
                        self._poisoned = True
                        raise

    def run(
        self,
        *,
        agent_kind: AgentKind,
        query: str,
        graph_run: str,
        graph_trace: str,
        fallback: FallbackPolicy = FallbackPolicy.RAG_SUPPORTED,
    ) -> EvidencePipelineResult:
        """Run the full evidence pipeline.

        For no-RAG and hard-block fallbacks, retrieval is skipped and
        references are empty.
        """
        if self._poisoned:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
        if self._reentry_guard:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")

        if fallback is FallbackPolicy.HARD_BLOCK:
            return EvidencePipelineResult(
                result=ClaimVerifierResult.HARD_BLOCK,
                fallback=fallback,
            )

        if fallback is FallbackPolicy.MODEL_KNOWLEDGE_ONLY:
            with self._lock:
                _ctx = self._capture_callback_context()
                result = self._verifier.verify(
                    agent_kind=agent_kind,
                    bundles={},
                    claims=(),
                    links=(),
                    fallback=fallback,
                )
                self._verify_callback_context(_ctx)
            return EvidencePipelineResult(
                result=result,
                fallback=fallback,
            )

        # RAG-supported path
        node: SyndromeRetrievalNode | FormulaRetrievalNode
        if agent_kind is AgentKind.SYNDROME:
            node = self._syndrome_node
        elif agent_kind is AgentKind.FORMULA:
            node = self._formula_node
        else:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_SCHEMA_INVALID")

        retrieval_result = node.retrieve(
            query=query,
            graph_run=graph_run,
            graph_trace=graph_trace,
        )

        # Store all retrieved bundles — pre-recognize in registry first
        registry = self._get_active_registry()
        all_packets: list[SandboxEvidencePacketV1] = []
        self._reentry_guard += 1
        try:
            for bundle in retrieval_result.bundles:
                if registry is not None:
                    registry.add_recognized(bundle.bundle_digest)
                self._store.put(bundle)
                all_packets.extend(bundle.packets)

            # Build the bundle map (inside reentry guard to prevent
            # verifier callback from replacing store references)
            bundle_map: dict[str, SandboxEvidenceBundleV1] = {b.bundle_digest: b for b in retrieval_result.bundles}

            # Capture pre-callback state for identity/state-drift sealing
            _ctx = self._capture_callback_context()
            # Verify — now under reentry guard so reentrant pipeline.run
            # during verifier callback is detected and rejected.
            verifier_result = self._verifier.verify(
                agent_kind=agent_kind,
                bundles=bundle_map,
                claims=(),
                links=(),
                fallback=fallback,
            )
            # Verify identity/state unchanged after untrusted callback
            self._verify_callback_context(_ctx)
        finally:
            self._reentry_guard -= 1

        # Build context projection
        all_bundles = tuple(retrieval_result.bundles)
        bundle_digests = tuple(b.bundle_digest for b in all_bundles)
        packet_tuple = tuple(all_packets)

        if verifier_result is ClaimVerifierResult.RAG_SUPPORTED and packet_tuple:
            context_digest = hashlib.sha256(
                json.dumps(
                    [p.model_dump(mode="json") for p in packet_tuple],
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            context = SandboxEvidenceContextV1(
                evidence_packets=packet_tuple,
                bundles=all_bundles,
                bundle_digests=bundle_digests,
                context_digest=context_digest,
            )
        else:
            context = None

        return EvidencePipelineResult(
            packets=packet_tuple,
            bundles=all_bundles,
            result=verifier_result,
            fallback=fallback,
            context=context,
        )

    def verify_claims(
        self,
        *,
        agent_kind: AgentKind,
        claims: Sequence[SandboxEvidenceClaimV1],
        links: Sequence[SandboxClaimEvidenceLinkV1],
        fallback: FallbackPolicy = FallbackPolicy.RAG_SUPPORTED,
    ) -> ClaimVerifierResult:
        """Verify claim-to-evidence links independently of retrieval."""
        if self._poisoned:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_UNAVAILABLE")
        if self._reentry_guard:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_AUTHORITY_REJECTED")
        if len(claims) > _CLAIM_LINK_LIMIT:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_LIMIT_EXCEEDED")
        if len(links) > _CLAIM_LINK_LIMIT:
            raise SandboxEvidenceError("SANDBOX_EVIDENCE_LIMIT_EXCEEDED")

        # Build bundle map from store
        with self._lock:
            bundle_map: dict[str, SandboxEvidenceBundleV1] = {}
            # Collect all bundle digests referenced in links
            for link in links:
                cached = self._store.get(link.bundle_digest)
                if cached is not None:
                    bundle_map[link.bundle_digest] = cached

        # Capture pre-callback state for identity/state-drift sealing
        _ctx = self._capture_callback_context()
        result = self._verifier.verify(
            agent_kind=agent_kind,
            bundles=bundle_map,
            claims=claims,
            links=links,
            fallback=fallback,
        )
        # Verify identity/state unchanged after untrusted callback
        self._verify_callback_context(_ctx)
        return result
