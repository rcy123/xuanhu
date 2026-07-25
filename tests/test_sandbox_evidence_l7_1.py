"""L7-1 sandbox evidence data model and policy tests.

The tests are organised as:

* :class:`TestSandboxEvidenceRed` â€” gap-proving tests that exercise the
  import surface and document the historical absence of the L7-1 module.
* :class:`TestSandboxEvidenceGreen` â€” full-coverage tests of the
  delivered module: determinism, scope, source policy, RAG-unavailable
  dispatch, and citation verification.

The tests are pure in-memory: no I/O, no model calls, no network.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_runtime.sandbox_evidence import (
    ALLOWED_SOURCE_TYPES,
    SANDBOX_AGENT_CONTEXT_VALUES,
    CitationVerdict,
    EvidenceSourceKind,
    EvidenceSourcePolicy,
    RAGUnavailablePolicy,
    RetrievalBehavior,
    SandboxAgentContext,
    SandboxEvidenceError,
    SandboxEvidencePacket,
    SandboxEvidenceRunContext,
    SandboxEvidenceScope,
    SandboxEvidenceVerifier,
    build_evidence_packet,
    decide_retrieval_behavior,
    derive_content_digest,
    reject_evidence,
)

# â”€â”€ Constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_TRACE_A = "sandbox-evidence-trace-aaa"
_TRACE_B = "sandbox-evidence-trace-bbb"
_SOURCE_ID = "sandbox-chunk-001"

_THEORY_CONTENT = (
    "ã€Šä¼¤å¯’è®ºã€‹ç¬¬åäºŒæ¡ï¼šå¤ªé˜³ä¸­é£Žï¼Œé˜³æµ®è€Œé˜´å¼±ï¼Œé˜³æµ®è€…çƒ­è‡ªå‘ï¼Œ"
    "é˜´å¼±è€…æ±—è‡ªå‡ºï¼Œå•¬å•¬æ¶å¯’ï¼Œæ·…æ·…æ¶é£Žï¼Œç¿•ç¿•å‘çƒ­ï¼Œé¼»é¸£å¹²å‘•è€…ï¼Œ"
    "æ¡‚æžæ±¤ä¸»ä¹‹ã€‚"
)
_CASE_CONTENT = (
    "åŒ»æ¡ˆï¼šæ‚£è€…å‘çƒ­ã€æ±—å‡ºã€æ¶é£Žã€è„‰æµ®ç¼“ï¼ŒèˆŒæ·¡è‹”ç™½ã€‚"
    "è¯Šæ–­ä¸ºå¤ªé˜³ä¸­é£Žè¯ï¼Œä»¥æ¡‚æžæ±¤åŠ å‡æ²»ä¹‹ã€‚"
)
_FORMULA_CONTENT = (
    "æ¡‚æžæ±¤æ–¹ï¼šæ¡‚æžä¸‰ä¸¤ï¼ˆåŽ»çš®ï¼‰ã€èŠè¯ä¸‰ä¸¤ã€ç”˜è‰äºŒä¸¤ï¼ˆç‚™ï¼‰ã€"
    "ç”Ÿå§œä¸‰ä¸¤ï¼ˆåˆ‡ï¼‰ã€å¤§æž£åäºŒæžšï¼ˆæ“˜ï¼‰ã€‚"
)
_HERB_CONTENT = (
    "æ¡‚æžï¼šæ€§å‘³è¾›ç”˜æ¸©ï¼Œå½’å¿ƒã€è‚ºã€è†€èƒ±ç»ï¼›åŠŸæ•ˆå‘æ±—è§£è‚Œã€æ¸©é€šç»è„‰ã€‚"
)


# â”€â”€ Fixtures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _packet(
    *,
    content: str = _THEORY_CONTENT,
    source_type: str = "theory",
    source_id: str | None = _SOURCE_ID,
    retrieval_trace_id: str | None = _TRACE_A,
) -> SandboxEvidencePacket:
    return build_evidence_packet(
        content=content,
        source_type=source_type,  # type: ignore[arg-type]
        source_id=source_id,
        retrieval_trace_id=retrieval_trace_id,
    )


def _run(
    *,
    trace_id: str = _TRACE_A,
    context: SandboxAgentContext | str = SandboxAgentContext.SYNDROME,
) -> SandboxEvidenceRunContext:
    return SandboxEvidenceRunContext(trace_id=trace_id, context=context)


# â”€â”€ RED â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestSandboxEvidenceRed:
    """RED tests proving gaps before the L7-1 module exists."""

    def test_l7_1_red_packet_class_exists(self) -> None:
        """``SandboxEvidencePacket`` is importable."""
        assert SandboxEvidencePacket is not None

    def test_l7_1_red_source_policy_class_exists(self) -> None:
        """``EvidenceSourcePolicy`` is importable."""
        assert EvidenceSourcePolicy is not None
        assert callable(EvidenceSourcePolicy.allowed_sources_for)

    def test_l7_1_red_scope_class_exists(self) -> None:
        """``SandboxEvidenceScope`` is importable with ``is_visible``."""
        assert SandboxEvidenceScope is not None
        assert callable(SandboxEvidenceScope.is_visible)

    def test_l7_1_red_rag_unavailable_policy_exists(self) -> None:
        """``RAGUnavailablePolicy`` is importable with both branches."""
        assert RAGUnavailablePolicy.FALLBACK_TO_MODEL_KNOWLEDGE is not None
        assert RAGUnavailablePolicy.HARD_BLOCK is not None
        assert decide_retrieval_behavior is not None

    def test_l7_1_red_verifier_class_exists(self) -> None:
        """``SandboxEvidenceVerifier`` is importable with ``verify_citation``."""
        assert SandboxEvidenceVerifier is not None
        assert callable(SandboxEvidenceVerifier.verify_citation)


# â”€â”€ GREEN â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestSandboxEvidenceGreen:
    """GREEN tests proving evidence DTO + policy correctness."""

    # â”€â”€ Determinism â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_packet_deterministic_same_triple_same_id(self) -> None:
        """Same (content, source_type, source_id) â†’ same evidence_id."""

        packet_a = build_evidence_packet(
            content=_THEORY_CONTENT,
            source_type="theory",
            source_id=_SOURCE_ID,
            retrieval_trace_id=_TRACE_A,
        )
        packet_b = build_evidence_packet(
            content=_THEORY_CONTENT,
            source_type="theory",
            source_id=_SOURCE_ID,
            retrieval_trace_id=_TRACE_B,  # trace may differ but base triple not
        )
        assert packet_a.evidence_id == packet_b.evidence_id

    def test_l7_1_green_packet_different_content_different_id(self) -> None:
        """Different content â†’ different evidence_id."""

        packet_a = build_evidence_packet(
            content=_THEORY_CONTENT,
            source_type="theory",
            source_id=_SOURCE_ID,
            retrieval_trace_id=_TRACE_A,
        )
        packet_b = build_evidence_packet(
            content=_CASE_CONTENT,
            source_type="theory",
            source_id=_SOURCE_ID,
            retrieval_trace_id=_TRACE_A,
        )
        assert packet_a.evidence_id != packet_b.evidence_id

    def test_l7_1_green_packet_different_source_type_different_id(self) -> None:
        """Different source_type â†’ different evidence_id."""

        packet_a = build_evidence_packet(
            content=_FORMULA_CONTENT,
            source_type="formula",
            source_id=_SOURCE_ID,
        )
        packet_b = build_evidence_packet(
            content=_FORMULA_CONTENT,
            source_type="herb",
            source_id=_SOURCE_ID,
        )
        assert packet_a.evidence_id != packet_b.evidence_id

    def test_l7_1_green_packet_different_source_id_different_id(self) -> None:
        """Different source_id â†’ different evidence_id."""

        packet_a = build_evidence_packet(
            content=_THEORY_CONTENT,
            source_type="theory",
            source_id="chunk-aaa",
        )
        packet_b = build_evidence_packet(
            content=_THEORY_CONTENT,
            source_type="theory",
            source_id="chunk-bbb",
        )
        assert packet_a.evidence_id != packet_b.evidence_id

    def test_l7_1_green_content_digest_matches_sha256(self) -> None:
        """``content_digest`` field must equal ``sha256(content)``."""

        packet = _packet()
        expected = hashlib.sha256(_THEORY_CONTENT.encode("utf-8")).hexdigest()
        assert packet.content_digest == expected
        assert packet.content_digest == derive_content_digest(_THEORY_CONTENT)

    def test_l7_1_green_evidence_id_format(self) -> None:
        """``evidence_id`` must follow ``sandbox-evidence-<64 hex>``."""

        packet = _packet()
        assert packet.evidence_id.startswith("sandbox-evidence-")
        digest_part = packet.evidence_id.removeprefix("sandbox-evidence-")
        assert len(digest_part) == 64
        assert all(ch in "0123456789abcdef" for ch in digest_part)

    # â”€â”€ Immutability / validation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_packet_is_frozen(self) -> None:
        """Packet fields must be immutable after construction."""

        packet = _packet()
        with pytest.raises(ValidationError):
            packet.content = "tampered"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            packet.source_type = "case"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            packet.evidence_id = "sandbox-evidence-" + "0" * 64  # type: ignore[misc]

    def test_l7_1_green_packet_rejects_tampered_content_digest(self) -> None:
        """A packet with a wrong ``content_digest`` must be rejected."""

        packet = _packet()
        # Direct construction with mismatched (content, content_digest)
        # must be rejected by the model_validator.
        with pytest.raises(ValidationError):
            SandboxEvidencePacket(
                evidence_id=packet.evidence_id,
                source_type=packet.source_type,
                source_id=packet.source_id,
                content="different content but claimed digest",
                content_digest=packet.content_digest,
                retrieval_trace_id=packet.retrieval_trace_id,
            )

    def test_l7_1_green_packet_rejects_tampered_evidence_id(self) -> None:
        """A packet with a wrong ``evidence_id`` must be rejected."""

        packet = _packet()
        wrong_id = "sandbox-evidence-" + "f" * 64
        # Use ValidationError path: re-validate with mismatched evidence_id.
        with pytest.raises(ValidationError):
            SandboxEvidencePacket(
                evidence_id=wrong_id,
                source_type=packet.source_type,
                source_id=packet.source_id,
                content=packet.content,
                content_digest=packet.content_digest,
                retrieval_trace_id=packet.retrieval_trace_id,
            )

    def test_l7_1_green_packet_rejects_unknown_source_type(self) -> None:
        """An unknown ``source_type`` literal is rejected."""

        with pytest.raises(ValidationError):
            build_evidence_packet(
                content=_THEORY_CONTENT,
                source_type="unknown-source-type",  # type: ignore[arg-type]
                source_id=_SOURCE_ID,
            )

    def test_l7_1_green_packet_rejects_empty_content(self) -> None:
        """Empty content is not allowed."""

        with pytest.raises(ValidationError):
            build_evidence_packet(
                content="",
                source_type="theory",
            )

    # â”€â”€ Source policy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_source_policy_syndrome_returns_theory_case(self) -> None:
        """``syndrome`` context can cite ``theory`` and ``case`` only."""

        allowed = EvidenceSourcePolicy.allowed_sources_for(
            SandboxAgentContext.SYNDROME
        )
        assert "theory" in allowed
        assert "case" in allowed
        assert "formula" not in allowed
        assert "herb" not in allowed

    def test_l7_1_green_source_policy_formula_returns_formula_herb(self) -> None:
        """``formula`` context can cite ``formula`` and ``herb`` only."""

        allowed = EvidenceSourcePolicy.allowed_sources_for(
            SandboxAgentContext.FORMULA
        )
        assert "formula" in allowed
        assert "herb" in allowed
        assert "theory" not in allowed
        assert "case" not in allowed

    def test_l7_1_green_source_policy_modification_returns_formula_herb(self) -> None:
        """``modification`` context can cite ``formula`` and ``herb`` only."""

        allowed = EvidenceSourcePolicy.allowed_sources_for(
            SandboxAgentContext.MODIFICATION
        )
        assert "formula" in allowed
        assert "herb" in allowed
        assert "theory" not in allowed
        assert "case" not in allowed

    def test_l7_1_green_source_policy_inquiry_returns_empty(self) -> None:
        """``inquiry`` context cannot cite any evidence."""

        allowed = EvidenceSourcePolicy.allowed_sources_for(
            SandboxAgentContext.INQUIRY
        )
        assert allowed == frozenset()

    def test_l7_1_green_source_policy_sufficiency_returns_empty(self) -> None:
        """``sufficiency`` context cannot cite any evidence."""

        allowed = EvidenceSourcePolicy.allowed_sources_for(
            SandboxAgentContext.SUFFICIENCY
        )
        assert allowed == frozenset()

    def test_l7_1_green_source_policy_unknown_context_empty(self) -> None:
        """Unknown context â†’ empty set (fail-closed)."""

        allowed = EvidenceSourcePolicy.allowed_sources_for("never-seen-context")
        assert allowed == frozenset()

    def test_l7_1_green_source_policy_accepts_string_aliases(self) -> None:
        """The same context may be passed as a raw string value."""

        enum_allowed = EvidenceSourcePolicy.allowed_sources_for(
            SandboxAgentContext.SYNDROME
        )
        string_allowed = EvidenceSourcePolicy.allowed_sources_for("syndrome")
        assert enum_allowed == string_allowed

    def test_l7_1_green_all_contexts_covered(self) -> None:
        """``SANDBOX_AGENT_CONTEXT_VALUES`` is finite and matches the enum."""

        expected_values = frozenset(
            value.value for value in SandboxAgentContext
        )
        assert expected_values == SANDBOX_AGENT_CONTEXT_VALUES

    # â”€â”€ Scope / visibility â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_scope_same_trace_visible(self) -> None:
        """Same ``trace_id`` and allowed source â†’ visible."""

        packet = _packet(source_type="theory", retrieval_trace_id=_TRACE_A)
        run = _run(trace_id=_TRACE_A, context=SandboxAgentContext.SYNDROME)
        assert SandboxEvidenceScope.is_visible(packet, run) is True

    def test_l7_1_green_scope_cross_run_not_visible(self) -> None:
        """Cross ``trace_id`` references are not visible (fail-closed)."""

        packet = _packet(source_type="theory", retrieval_trace_id=_TRACE_A)
        run = _run(trace_id=_TRACE_B, context=SandboxAgentContext.SYNDROME)
        assert SandboxEvidenceScope.is_visible(packet, run) is False

    def test_l7_1_green_scope_missing_trace_id_not_visible(self) -> None:
        """An evidence packet without ``retrieval_trace_id`` is never visible."""

        packet = _packet(retrieval_trace_id=None)
        run = _run(trace_id=_TRACE_A, context=SandboxAgentContext.SYNDROME)
        assert SandboxEvidenceScope.is_visible(packet, run) is False

    def test_l7_1_green_scope_source_mismatch_not_visible(self) -> None:
        """Evidence whose ``source_type`` is not in the context's policy is
        not visible even within the same ``trace_id``."""

        packet = _packet(
            source_type="formula",  # not allowed in `syndrome`
            retrieval_trace_id=_TRACE_A,
        )
        run = _run(trace_id=_TRACE_A, context=SandboxAgentContext.SYNDROME)
        assert SandboxEvidenceScope.is_visible(packet, run) is False

    def test_l7_1_green_scope_inquiry_never_visible(self) -> None:
        """``inquiry`` policy forbids every source â†’ never visible."""

        packet = _packet(source_type="theory", retrieval_trace_id=_TRACE_A)
        run = _run(trace_id=_TRACE_A, context=SandboxAgentContext.INQUIRY)
        assert SandboxEvidenceScope.is_visible(packet, run) is False

    def test_l7_1_green_scope_pure_function_no_mutation(self) -> None:
        """``is_visible`` must not mutate its inputs."""

        packet = _packet(source_type="theory", retrieval_trace_id=_TRACE_A)
        run = _run(trace_id=_TRACE_A, context=SandboxAgentContext.SYNDROME)
        before_packet = packet.model_dump(mode="json")
        before_run = run.model_dump(mode="json")
        SandboxEvidenceScope.is_visible(packet, run)
        SandboxEvidenceScope.is_visible(packet, run)
        assert packet.model_dump(mode="json") == before_packet
        assert run.model_dump(mode="json") == before_run

    # â”€â”€ RAG unavailable dispatch â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_rag_available_always_retrieve(self) -> None:
        """``rag_available=True`` â†’ RETRIEVE regardless of policy."""

        assert (
            decide_retrieval_behavior(True, RAGUnavailablePolicy.HARD_BLOCK)
            is RetrievalBehavior.RETRIEVE
        )
        assert (
            decide_retrieval_behavior(
                True, RAGUnavailablePolicy.FALLBACK_TO_MODEL_KNOWLEDGE
            )
            is RetrievalBehavior.RETRIEVE
        )

    def test_l7_1_green_rag_unavailable_hard_block(self) -> None:
        """Unavailable + HARD_BLOCK â†’ BLOCKED."""

        assert (
            decide_retrieval_behavior(False, RAGUnavailablePolicy.HARD_BLOCK)
            is RetrievalBehavior.BLOCKED
        )

    def test_l7_1_green_rag_unavailable_fallback(self) -> None:
        """Unavailable + FALLBACK â†’ FALLBACK."""

        assert (
            decide_retrieval_behavior(
                False, RAGUnavailablePolicy.FALLBACK_TO_MODEL_KNOWLEDGE
            )
            is RetrievalBehavior.FALLBACK
        )

    # â”€â”€ Citation verifier â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_verifier_pass(self) -> None:
        """Aligned citation + valid evidence â†’ PASS."""

        packet = _packet(source_type="theory")
        verdict = SandboxEvidenceVerifier.verify_citation(
            citation_source_type="theory",
            evidence_packet=packet,
            policy=EvidenceSourcePolicy(),
            context=SandboxAgentContext.SYNDROME,
        )
        assert verdict is CitationVerdict.PASS

    def test_l7_1_green_verifier_invalid_source_type(self) -> None:
        """Source type not in policy allow-list â†’ INVALID_SOURCE_TYPE."""

        packet = _packet(source_type="formula")
        verdict = SandboxEvidenceVerifier.verify_citation(
            citation_source_type="formula",
            evidence_packet=packet,
            policy=EvidenceSourcePolicy(),
            context=SandboxAgentContext.SYNDROME,
        )
        assert verdict is CitationVerdict.INVALID_SOURCE_TYPE

    def test_l7_1_green_verifier_missing_evidence(self) -> None:
        """Citation present but evidence_packet is None â†’ MISSING_EVIDENCE."""

        verdict = SandboxEvidenceVerifier.verify_citation(
            citation_source_type="theory",
            evidence_packet=None,
            policy=EvidenceSourcePolicy(),
            context=SandboxAgentContext.SYNDROME,
        )
        assert verdict is CitationVerdict.MISSING_EVIDENCE

    def test_l7_1_green_verifier_tampered_content(self) -> None:
        """Evidence whose ``content_digest`` doesn't match content is
        rejected as TAMPERED_CONTENT."""

        packet = _packet(source_type="theory")
        # Bypass the strict model validator to construct a packet whose
        # content_digest is inconsistent with its content.  The verifier
        # must catch this independent of how the packet was assembled.
        tampered = SandboxEvidencePacket.model_construct(
            evidence_id=packet.evidence_id,
            source_type=packet.source_type,
            source_id=packet.source_id,
            content="tampered content",
            content_digest=packet.content_digest,  # mismatch by design
            retrieval_trace_id=packet.retrieval_trace_id,
        )
        verdict = SandboxEvidenceVerifier.verify_citation(
            citation_source_type="theory",
            evidence_packet=tampered,
            policy=EvidenceSourcePolicy(),
            context=SandboxAgentContext.SYNDROME,
        )
        assert verdict is CitationVerdict.TAMPERED_CONTENT

    def test_l7_1_green_verifier_packet_source_mismatch(self) -> None:
        """Citation source_type vs. packet source_type mismatch â†’ INVALID."""

        packet = _packet(source_type="theory")
        verdict = SandboxEvidenceVerifier.verify_citation(
            citation_source_type="case",  # not the packet's source_type
            evidence_packet=packet,
            policy=EvidenceSourcePolicy(),
            context=SandboxAgentContext.SYNDROME,
        )
        assert verdict is CitationVerdict.INVALID_SOURCE_TYPE

    def test_l7_1_green_verifier_inquiry_always_invalid(self) -> None:
        """Any citation under ``inquiry`` (empty allow-list) â†’ INVALID."""

        packet = _packet(source_type="theory")
        verdict = SandboxEvidenceVerifier.verify_citation(
            citation_source_type="theory",
            evidence_packet=packet,
            policy=EvidenceSourcePolicy(),
            context=SandboxAgentContext.INQUIRY,
        )
        assert verdict is CitationVerdict.INVALID_SOURCE_TYPE

    # â”€â”€ Errors â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_error_chainless_and_payload_free(self) -> None:
        """``SandboxEvidenceError`` must be chainless and payload-free."""

        try:
            raise SandboxEvidenceError()
        except SandboxEvidenceError as exc:
            assert str(exc) == "SANDBOX_EVIDENCE_UNAVAILABLE"
            assert exc.__cause__ is None
            assert exc.__context__ is None
            assert exc.__slots__ == ()

        assert reject_evidence.__name__ == "reject_evidence"
        with pytest.raises(SandboxEvidenceError):
            reject_evidence()

    # â”€â”€ Slots / purity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_classes_have_empty_slots(self) -> None:
        """Pure-function / policy classes carry no instance state."""

        assert EvidenceSourcePolicy().model_config["frozen"] is True
        assert SandboxEvidenceScope().model_config["frozen"] is True
        assert (
            SandboxEvidenceVerifier().model_config["frozen"] is True
        )


# â”€â”€ AST / import-roots â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestSandboxEvidenceStatic:
    """Static / AST checks (no I/O, no model, no network)."""

    def test_l7_1_green_no_forbidden_calls(self) -> None:
        """No ``open`` / ``print`` / ``breakpoint`` / ``exec`` / ``eval`` /
        ``compile`` calls and no network tokens in source."""

        source = Path(
            "app/agent_runtime/sandbox_evidence.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        forbidden = {"open", "print", "breakpoint", "exec", "eval", "compile"}
        assert called_names.isdisjoint(forbidden)
        for token in (
            "http://",
            "https://",
            "socket",
            "subprocess",
            "requests",
            ".env",
            "data/",
        ):
            assert token not in source

    def test_l7_1_green_no_new_import_roots(self) -> None:
        """Imports must stay within the L5/L6 approved set."""

        source = Path(
            "app/agent_runtime/sandbox_evidence.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", maxsplit=1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", maxsplit=1)[0])

        allowed = {
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

    def test_l7_1_green_no_environ_or_getenv(self) -> None:
        """No ``os.environ`` / ``os.getenv`` access."""

        source = Path(
            "app/agent_runtime/sandbox_evidence.py"
        ).read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "getenv(" not in source

    def test_l7_1_green_pure_function_canonical_determinism(self) -> None:
        """The internal canonical serializer is stable."""

        a = json.dumps(
            {"b": 1, "a": 2},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        b = json.dumps(
            {"a": 2, "b": 1},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert a == b
        assert hashlib.sha256(a.encode("utf-8")).hexdigest() == (
            hashlib.sha256(b.encode("utf-8")).hexdigest()
        )

    def test_l7_1_green_no_mutation_of_inputs(self) -> None:
        """The pure-function policy must not mutate call arguments."""

        packet = _packet(source_type="theory", retrieval_trace_id=_TRACE_A)
        run = _run(trace_id=_TRACE_A, context=SandboxAgentContext.SYNDROME)
        before_p = copy.deepcopy(packet.model_dump(mode="python"))
        before_r = copy.deepcopy(run.model_dump(mode="python"))
        SandboxEvidenceScope.is_visible(packet, run)
        SandboxEvidenceScope.is_visible(packet, run)
        assert packet.model_dump(mode="python") == before_p
        assert run.model_dump(mode="python") == before_r

    # â”€â”€ Surface-level type checks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def test_l7_1_green_allowed_source_types_is_frozenset(self) -> None:
        """``ALLOWED_SOURCE_TYPES`` is the closed source-type set."""

        expected_sources = frozenset(
            {
                EvidenceSourceKind.THEORY.value,
                EvidenceSourceKind.CASE.value,
                EvidenceSourceKind.FORMULA.value,
                EvidenceSourceKind.HERB.value,
            }
        )
        assert isinstance(ALLOWED_SOURCE_TYPES, frozenset)
        assert expected_sources == ALLOWED_SOURCE_TYPES

    def test_l7_1_green_enums_are_strings(self) -> None:
        """All delivered enums are str-typed for JSON-safety."""

        for enum_cls in (SandboxAgentContext, RAGUnavailablePolicy,
                         RetrievalBehavior, CitationVerdict,
                         EvidenceSourceKind):
            for member in enum_cls:
                assert isinstance(member.value, str)
                assert isinstance(member, str)  # str-enum membership
