"""Offline deterministic Evidence/RAG policy layer for the L7 sandbox.

This module defines the **data model and policy layer** for Evidence/RAG
augmentation in the L7 personal-learning sandbox.  It introduces:

* :class:`SandboxEvidencePacket` — a deterministic, immutable, pure-data
  evidence DTO.  No patients, no model output, no timestamps, no random
  identifiers.  ``evidence_id`` is derived from ``sha256(content +
  source_type + source_id)`` so the same triple always maps to the same id.
* :class:`EvidenceSourcePolicy` — a pure function mapping an agent context
  to the set of evidence source types it is allowed to cite.
* :class:`SandboxEvidenceScope` — a pure function deciding whether an
  evidence packet is visible to a given run context.
* :class:`RAGUnavailablePolicy` + ``decide_retrieval_behavior`` —
  enumerations and a pure-function dispatch describing what an agent
  should do when a real RAG backend is unavailable.
* :class:`SandboxEvidenceVerifier` — a deterministic citation → evidence
  validator returning a :class:`CitationVerdict`.

All helpers in this module are pure deterministic functions or
frozen pydantic models.  No I/O, no model calls, no randomness, no
timestamps, no network access.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Mapping
from typing import Final, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, model_validator

_EVIDENCE_ID_PREFIX: Final[str] = "sandbox-evidence-"
_EVIDENCE_ID_PATTERN: Final[str] = r"^sandbox-evidence-[0-9a-f]{64}$"
_DIGEST_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

# Conservative bounded content size; mirrors L5 sandbox caps.
_MAX_EVIDENCE_CONTENT_BYTES: Final[int] = 32 * 1024
_MAX_SOURCE_ID_LENGTH: Final[int] = 128
_MAX_TRACE_ID_LENGTH: Final[int] = 128

ALLOWED_SOURCE_TYPES: Final[frozenset[str]] = frozenset(
    {"theory", "case", "formula", "herb"}
)

EvidenceSourceType = Literal["theory", "case", "formula", "herb"]


class _LocalStrictFrozenModel(BaseModel):
    """Local re-export of the L6 strict-frozen base for readability."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SandboxEvidenceError(ValueError):
    """A fixed, payload-free, chainless evidence-boundary failure."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("SANDBOX_EVIDENCE_UNAVAILABLE")


# ────────────────────────────────────────────────────────────────────────
# Agent context enumeration
# ────────────────────────────────────────────────────────────────────────


class SandboxAgentContext(enum.StrEnum):
    """Closed enumeration of agent contexts that may cite Evidence.

    Every value is a fixed string; the enum is purely declarative and
    carries no runtime state.  New contexts must be added in a follow-up
    task — they are intentionally not user-extensible at runtime.
    """

    SYNDROME = "syndrome"
    FORMULA = "formula"
    MODIFICATION = "modification"
    INQUIRY = "inquiry"
    SUFFICIENCY = "sufficiency"


_SANDBOX_AGENT_CONTEXT_VALUES: Final[frozenset[str]] = frozenset(
    value.value for value in SandboxAgentContext
)
SANDBOX_AGENT_CONTEXT_VALUES: Final[frozenset[str]] = _SANDBOX_AGENT_CONTEXT_VALUES

# Frozen declarative mapping — every key is a SandboxAgentContext value
# and every value is the corresponding frozenset of allowed sources.
# Module-level (not class attribute) so pydantic does not treat it as a field.
_SANDBOX_SOURCE_POLICY_TABLE: Final[Mapping[str, frozenset[str]]] = {
    SandboxAgentContext.SYNDROME.value: frozenset({"theory", "case"}),
    SandboxAgentContext.FORMULA.value: frozenset({"formula", "herb"}),
    SandboxAgentContext.MODIFICATION.value: frozenset({"formula", "herb"}),
    SandboxAgentContext.INQUIRY.value: frozenset(),
    SandboxAgentContext.SUFFICIENCY.value: frozenset(),
}


# ────────────────────────────────────────────────────────────────────────
# Evidence source types enumeration (string-typed alias to Literal)
# ────────────────────────────────────────────────────────────────────────


class EvidenceSourceKind(enum.StrEnum):
    """Closed enumeration of evidence source kinds for cross-checking."""

    THEORY = "theory"
    CASE = "case"
    FORMULA = "formula"
    HERB = "herb"


# ────────────────────────────────────────────────────────────────────────
# Canonical JSON / digest helpers
# ────────────────────────────────────────────────────────────────────────


def _canonical_json_bytes(value: object) -> bytes:
    """Encode a value as deterministic, sorted-key JSON bytes."""

    def _json_ready(raw: object) -> object:
        if isinstance(raw, BaseModel):
            return raw.model_dump(mode="json")
        if isinstance(raw, Mapping):
            return {str(key): _json_ready(item) for key, item in raw.items()}
        if isinstance(raw, (list, tuple)):
            return [_json_ready(item) for item in raw]
        if isinstance(raw, bytes):
            return raw.hex()
        return raw

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_hex(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


# ────────────────────────────────────────────────────────────────────────
# Evidence DTO
# ────────────────────────────────────────────────────────────────────────


class SandboxEvidencePacket(_LocalStrictFrozenModel):
    """Immutable, deterministic evidence DTO — no patients, no models.

    ``evidence_id`` is **derived** from the canonical triple
    ``(content, source_type, source_id)``.  ``content_digest`` is the
    sha256 of ``content`` alone.  Together they make tampering with any
    field detectable while keeping the packet pure data.
    """

    evidence_id: str = Field(pattern=_EVIDENCE_ID_PATTERN)
    source_type: EvidenceSourceType
    source_id: str | None = Field(default=None, max_length=_MAX_SOURCE_ID_LENGTH)
    content: str = Field(min_length=1, max_length=_MAX_EVIDENCE_CONTENT_BYTES)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    retrieval_trace_id: str | None = Field(
        default=None, max_length=_MAX_TRACE_ID_LENGTH
    )

    @model_validator(mode="after")
    def _enforce_invariants(self) -> SandboxEvidencePacket:
        # content_digest must equal sha256(content).
        expected_content_digest = hashlib.sha256(
            self.content.encode("utf-8")
        ).hexdigest()
        if self.content_digest != expected_content_digest:
            raise ValueError("content_digest mismatch")

        # evidence_id must equal sha256(canonical(content, source_type, source_id)).
        expected_evidence_id = _derive_evidence_id(
            content=self.content,
            source_type=self.source_type,
            source_id=self.source_id,
        )
        if self.evidence_id != expected_evidence_id:
            raise ValueError("evidence_id mismatch")
        return self


def _derive_evidence_id(
    *,
    content: str,
    source_type: str,
    source_id: str | None,
) -> str:
    payload: dict[str, object] = {
        "content": content,
        "source_id": source_id,
        "source_type": source_type,
    }
    return _EVIDENCE_ID_PREFIX + _sha256_hex(payload)


def derive_content_digest(content: str) -> str:
    """Return the deterministic sha256 digest of ``content`` (utf-8)."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_evidence_packet(
    *,
    content: str,
    source_type: EvidenceSourceType,
    source_id: str | None = None,
    retrieval_trace_id: str | None = None,
) -> SandboxEvidencePacket:
    """Deterministically construct a :class:`SandboxEvidencePacket`.

    The constructor does not consult any external state and is therefore
    referentially transparent: equal inputs always yield equal packets,
    including the ``evidence_id`` and ``content_digest`` fields.
    """

    content_digest = derive_content_digest(content)
    evidence_id = _derive_evidence_id(
        content=content,
        source_type=source_type,
        source_id=source_id,
    )
    return SandboxEvidencePacket(
        content=content,
        content_digest=content_digest,
        evidence_id=evidence_id,
        retrieval_trace_id=retrieval_trace_id,
        source_id=source_id,
        source_type=source_type,
    )


# ────────────────────────────────────────────────────────────────────────
# Source-type policy
# ────────────────────────────────────────────────────────────────────────


class EvidenceSourcePolicy(_LocalStrictFrozenModel):
    """Pure-function policy: which sources may a context cite?

    The policy holds a fixed mapping from agent-context to the set of
    allowed evidence source types.  It is a frozen pydantic model so it
    carries no mutable runtime state, but every call to
    :meth:`allowed_sources_for` returns the same set for the same input.
    """

    @staticmethod
    def allowed_sources_for(context: str | SandboxAgentContext) -> frozenset[str]:
        """Return the set of allowed source types for ``context``.

        Accepts either a :class:`SandboxAgentContext` member or its raw
        string value.  Unknown contexts return an empty frozenset
        (fail-closed: explicit allow-list required for citation).
        """

        if isinstance(context, SandboxAgentContext):
            key = context.value
        elif isinstance(context, str):
            key = context
        else:
            return frozenset()
        return _SANDBOX_SOURCE_POLICY_TABLE.get(key, frozenset())


# ────────────────────────────────────────────────────────────────────────
# Scope / visibility
# ────────────────────────────────────────────────────────────────────────


class SandboxEvidenceRunContext(_LocalStrictFrozenModel):
    """A frozen, minimal run-context tuple used to gate evidence visibility.

    The struct carries only the data required for the scope check: the
    retrieval ``trace_id`` of the run that wants to consult the evidence,
    and the agent ``context`` that is making the request.  No sessions,
    no patients, no timestamps.
    """

    trace_id: str = Field(min_length=1, max_length=_MAX_TRACE_ID_LENGTH)
    context: str | SandboxAgentContext


class SandboxEvidenceScope(_LocalStrictFrozenModel):
    """Pure-function visibility check for evidence packets.

    Visibility requires all of:

    1. The packet's ``retrieval_trace_id`` matches the requesting run's
       ``trace_id``.  A packet carrying no ``retrieval_trace_id`` is not
       visible to any run (fail-closed).
    2. The packet's ``source_type`` is allowed for the requesting run's
       agent context (see :class:`EvidenceSourcePolicy`).
    """

    @staticmethod
    def is_visible(
        evidence: SandboxEvidencePacket,
        run_context: SandboxEvidenceRunContext,
        *,
        policy: EvidenceSourcePolicy | None = None,
    ) -> bool:
        """Return True iff ``evidence`` is visible to ``run_context``.

        Pure function; no side effects, no I/O, no model calls.
        """

        if not isinstance(evidence, SandboxEvidencePacket):
            return False
        if not isinstance(run_context, SandboxEvidenceRunContext):
            return False

        # Rule 1: trace_id must match, and evidence must declare one.
        if evidence.retrieval_trace_id is None:
            return False
        if run_context.trace_id != evidence.retrieval_trace_id:
            return False

        # Rule 2: source policy cross-check (fail-closed on unknown).
        source_policy = policy if policy is not None else EvidenceSourcePolicy()
        allowed = source_policy.allowed_sources_for(run_context.context)
        return evidence.source_type in allowed


# ────────────────────────────────────────────────────────────────────────
# RAG unavailable policy
# ────────────────────────────────────────────────────────────────────────


class RAGUnavailablePolicy(enum.StrEnum):
    """Closed enumeration of fallback behaviours when RAG is unavailable."""

    FALLBACK_TO_MODEL_KNOWLEDGE = "fallback_to_model_knowledge"
    HARD_BLOCK = "hard_block"


class RetrievalBehavior(enum.StrEnum):
    """Closed enumeration of retrieval modes after policy dispatch."""

    RETRIEVE = "retrieve"
    FALLBACK = "fallback"
    BLOCKED = "blocked"


def decide_retrieval_behavior(
    rag_available: bool,
    policy: RAGUnavailablePolicy,
) -> RetrievalBehavior:
    """Pure-function dispatch for RAG-availability × unavailable-policy.

    * ``rag_available=True`` → :attr:`RetrievalBehavior.RETRIEVE`
    * ``rag_available=False`` and policy
      :attr:`RAGUnavailablePolicy.HARD_BLOCK` →
      :attr:`RetrievalBehavior.BLOCKED`
    * ``rag_available=False`` and policy
      :attr:`RAGUnavailablePolicy.FALLBACK_TO_MODEL_KNOWLEDGE` →
      :attr:`RetrievalBehavior.FALLBACK`
    """

    if rag_available:
        return RetrievalBehavior.RETRIEVE
    if policy is RAGUnavailablePolicy.HARD_BLOCK:
        return RetrievalBehavior.BLOCKED
    return RetrievalBehavior.FALLBACK


# ────────────────────────────────────────────────────────────────────────
# Citation verifier
# ────────────────────────────────────────────────────────────────────────


class CitationVerdict(enum.StrEnum):
    """Closed enumeration of citation-verification outcomes."""

    PASS = "pass"
    INVALID_SOURCE_TYPE = "invalid_source_type"
    MISSING_EVIDENCE = "missing_evidence"
    TAMPERED_CONTENT = "tampered_content"


class SandboxEvidenceVerifier(_LocalStrictFrozenModel):
    """Deterministic citation → evidence verifier.

    The verifier is a pure four-way branch:

    * ``citation_source_type`` not in the policy allow-list for the
      calling context → :attr:`CitationVerdict.INVALID_SOURCE_TYPE`
    * ``evidence_packet is None`` (citation exists but no backing
      evidence) → :attr:`CitationVerdict.MISSING_EVIDENCE`
    * ``content_digest != sha256(content)`` →
      :attr:`CitationVerdict.TAMPERED_CONTENT`
    * otherwise → :attr:`CitationVerdict.PASS`
    """

    @staticmethod
    def verify_citation(
        *,
        citation_source_type: str,
        evidence_packet: SandboxEvidencePacket | None,
        policy: EvidenceSourcePolicy,
        context: str | SandboxAgentContext,
    ) -> CitationVerdict:
        """Verify a citation against the supplied evidence and policy."""

        allowed = policy.allowed_sources_for(context)
        if citation_source_type not in allowed:
            return CitationVerdict.INVALID_SOURCE_TYPE

        if evidence_packet is None:
            return CitationVerdict.MISSING_EVIDENCE

        # Cross-check source_type on the packet itself (defence in depth).
        if evidence_packet.source_type not in allowed:
            return CitationVerdict.INVALID_SOURCE_TYPE
        if evidence_packet.source_type != citation_source_type:
            return CitationVerdict.INVALID_SOURCE_TYPE

        # Tampering check: content digest must match sha256(content).
        expected = derive_content_digest(evidence_packet.content)
        if evidence_packet.content_digest != expected:
            return CitationVerdict.TAMPERED_CONTENT

        return CitationVerdict.PASS


# ────────────────────────────────────────────────────────────────────────
# Sandbox-boundary helpers (fixed failure semantics)
# ────────────────────────────────────────────────────────────────────────


def reject_evidence() -> NoReturn:
    """Raise the fixed, payload-free, chainless evidence error.

    Provided so future callers that need to fail closed can do so
    without depending on the closed Verifier's internals.
    """

    raise SandboxEvidenceError()


__all__ = [
    "ALLOWED_SOURCE_TYPES",
    "CitationVerdict",
    "EvidenceSourceKind",
    "EvidenceSourcePolicy",
    "EvidenceSourceType",
    "RAGUnavailablePolicy",
    "RetrievalBehavior",
    "SANDBOX_AGENT_CONTEXT_VALUES",
    "SandboxAgentContext",
    "SandboxEvidenceError",
    "SandboxEvidencePacket",
    "SandboxEvidenceRunContext",
    "SandboxEvidenceScope",
    "SandboxEvidenceVerifier",
    "build_evidence_packet",
    "decide_retrieval_behavior",
    "derive_content_digest",
    "reject_evidence",
]
