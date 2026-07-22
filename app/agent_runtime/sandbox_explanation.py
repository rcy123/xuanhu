"""Offline allowlist-verified explanations for accepted L5-1 sandbox results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_runtime.sandbox_safety import (
    SANDBOX_ADAPTER_VERSION,
    SANDBOX_RESULT_SCHEMA_VERSION,
    SandboxSafetyDecision,
    SandboxSafetyResultV1,
    SandboxSafetySeverity,
    canonical_result_bytes,
)

MAX_EXPLANATION_ISSUES = 64
MAX_EXPLANATION_BYTES = 8 * 1024
SANDBOX_EXPLANATION_DISCLAIMER: Literal[
    "sandbox_test_only_not_medical_advice"
] = "sandbox_test_only_not_medical_advice"

_MAX_ALLOWLIST_ENTRIES = 256
_UNVERIFIED_SOURCE_RESULT_DIGEST = "0" * 64
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SandboxExplanationStatus(StrEnum):
    ATTACHED = "attached"
    EXPLANATION_UNAVAILABLE = "explanation_unavailable"


class SandboxExplanationIssueRefV1(_StrictFrozenModel):
    issue_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    severity: SandboxSafetySeverity


class SandboxExplanationAllowlistEntryV1(_StrictFrozenModel):
    rule_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    text: str = Field(
        min_length=1,
        max_length=MAX_EXPLANATION_BYTES + 1,
        repr=False,
    )


class SandboxExplanationAllowlistBundleV1(_StrictFrozenModel):
    entries: tuple[SandboxExplanationAllowlistEntryV1, ...] = Field(
        max_length=_MAX_ALLOWLIST_ENTRIES
    )
    allowlist_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def entries_are_canonical_and_digest_bound(
        self,
    ) -> SandboxExplanationAllowlistBundleV1:
        rule_ids = tuple(entry.rule_id for entry in self.entries)
        if rule_ids != tuple(sorted(rule_ids)) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("allowlist entries must be unique and sorted")
        if self.allowlist_digest != _allowlist_digest(self.entries):
            raise ValueError("allowlist digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        entries: Sequence[SandboxExplanationAllowlistEntryV1],
    ) -> SandboxExplanationAllowlistBundleV1:
        canonical_entries = tuple(sorted(tuple(entries), key=lambda entry: entry.rule_id))
        return cls(
            entries=canonical_entries,
            allowlist_digest=_allowlist_digest(canonical_entries),
        )


class SandboxExplanationPortInputV1(_StrictFrozenModel):
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    decision: SandboxSafetyDecision
    issue_refs: tuple[SandboxExplanationIssueRefV1, ...] = Field(
        max_length=MAX_EXPLANATION_ISSUES
    )
    allowlist_entries: tuple[SandboxExplanationAllowlistEntryV1, ...] = Field(
        max_length=MAX_EXPLANATION_ISSUES
    )


class SandboxExplanationCandidateStatementV1(_StrictFrozenModel):
    issue_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    text: str = Field(
        min_length=1,
        max_length=MAX_EXPLANATION_BYTES + 1,
        repr=False,
    )


class SandboxExplanationCandidateV1(_StrictFrozenModel):
    statements: tuple[SandboxExplanationCandidateStatementV1, ...] = Field(
        max_length=MAX_EXPLANATION_ISSUES
    )


class SandboxExplanationStatementV1(_StrictFrozenModel):
    issue_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    text: str = Field(
        min_length=1,
        max_length=MAX_EXPLANATION_BYTES + 1,
        repr=False,
    )


class SandboxExplanationResultV1(_StrictFrozenModel):
    source_result_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: SandboxExplanationStatus
    statements: tuple[SandboxExplanationStatementV1, ...] = Field(
        max_length=MAX_EXPLANATION_ISSUES
    )
    disclaimer: Literal["sandbox_test_only_not_medical_advice"]
    explanation_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def result_is_digest_bound(self) -> SandboxExplanationResultV1:
        if self.status is SandboxExplanationStatus.EXPLANATION_UNAVAILABLE:
            if self.statements:
                raise ValueError("unavailable explanation cannot contain statements")
        elif not self.statements:
            raise ValueError("attached explanation requires statements")
        if self.explanation_digest != _explanation_digest(
            source_result_digest=self.source_result_digest,
            status=self.status,
            statements=self.statements,
            disclaimer=self.disclaimer,
        ):
            raise ValueError("explanation digest mismatch")
        return self


class SandboxExplanationPort(Protocol):
    """Minimal synchronous candidate-generation port; never a decision authority."""

    def generate(self, request: SandboxExplanationPortInputV1) -> object: ...


def canonical_explanation_bytes(value: object) -> bytes:
    """Serialize validated L5-2 values using a stable canonical JSON representation."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_explanation_bytes(value)).hexdigest()


def _allowlist_digest(
    entries: tuple[SandboxExplanationAllowlistEntryV1, ...],
) -> str:
    return _canonical_sha256({"entries": entries})


def _explanation_digest(
    *,
    source_result_digest: str,
    status: SandboxExplanationStatus,
    statements: tuple[SandboxExplanationStatementV1, ...],
    disclaimer: str,
) -> str:
    return _canonical_sha256(
        {
            "disclaimer": disclaimer,
            "source_result_digest": source_result_digest,
            "statements": statements,
            "status": status,
        }
    )


def _build_result(
    *,
    source_result_digest: str,
    status: SandboxExplanationStatus,
    statements: tuple[SandboxExplanationStatementV1, ...],
) -> SandboxExplanationResultV1:
    return SandboxExplanationResultV1(
        source_result_digest=source_result_digest,
        status=status,
        statements=statements,
        disclaimer=SANDBOX_EXPLANATION_DISCLAIMER,
        explanation_digest=_explanation_digest(
            source_result_digest=source_result_digest,
            status=status,
            statements=statements,
            disclaimer=SANDBOX_EXPLANATION_DISCLAIMER,
        ),
    )


def _unavailable(source_result_digest: str) -> SandboxExplanationResultV1:
    return _build_result(
        source_result_digest=source_result_digest,
        status=SandboxExplanationStatus.EXPLANATION_UNAVAILABLE,
        statements=(),
    )


def _parse_model[ModelT: BaseModel](
    model_type: type[ModelT],
    value: object,
) -> ModelT | None:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            return model_type.model_validate_json(value, strict=True)
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        if not isinstance(value, dict):
            return None
        return model_type.model_validate(value, strict=True)
    except Exception:
        return None


type _SourceInvariants = tuple[
    str,
    str,
    SandboxSafetyDecision,
    tuple[tuple[str, str, SandboxSafetySeverity, int], ...],
    str,
    str,
    str,
]
type _AllowlistInvariants = tuple[str, tuple[tuple[str, str], ...]]


def _source_invariants(source: SandboxSafetyResultV1) -> _SourceInvariants:
    return (
        source.sandbox_schema_version,
        source.adapter_version,
        source.decision,
        tuple(
            (
                issue.issue_id,
                issue.rule_id,
                issue.severity,
                issue.execution_order,
            )
            for issue in source.issues
        ),
        source.decision_subject_digest,
        source.run_envelope_digest,
        source.result_digest,
    )


def _allowlist_invariants(
    allowlist: SandboxExplanationAllowlistBundleV1,
) -> _AllowlistInvariants:
    return (
        allowlist.allowlist_digest,
        tuple((entry.rule_id, entry.text) for entry in allowlist.entries),
    )


def _source_is_unchanged(
    source_input: object,
    expected_bytes: bytes,
    expected_invariants: _SourceInvariants,
) -> bool:
    reparsed = _parse_model(SandboxSafetyResultV1, source_input)
    if reparsed is None:
        return False
    try:
        return (
            canonical_result_bytes(reparsed) == expected_bytes
            and _source_invariants(reparsed) == expected_invariants
        )
    except Exception:
        return False


def _allowlist_is_unchanged(
    allowlist_input: object,
    expected_bytes: bytes,
    expected_invariants: _AllowlistInvariants,
) -> bool:
    reparsed = _parse_model(
        SandboxExplanationAllowlistBundleV1,
        allowlist_input,
    )
    if reparsed is None:
        return False
    try:
        return (
            canonical_explanation_bytes(reparsed) == expected_bytes
            and _allowlist_invariants(reparsed) == expected_invariants
        )
    except Exception:
        return False


class SandboxSafetyExplanationAdapter:
    """Attach only locally verified, exact-allowlist explanations to L5-1 results."""

    __slots__ = ("_port",)

    def __init__(self, port: SandboxExplanationPort) -> None:
        self._port = port

    def explain(
        self,
        source_result: object,
        allowlist_bundle: object,
    ) -> SandboxExplanationResultV1:
        try:
            return self._explain(source_result, allowlist_bundle)
        except Exception:
            return _unavailable(_UNVERIFIED_SOURCE_RESULT_DIGEST)

    def _explain(
        self,
        source_input: object,
        allowlist_input: object,
    ) -> SandboxExplanationResultV1:
        source = _parse_model(SandboxSafetyResultV1, source_input)
        if source is None:
            return _unavailable(_UNVERIFIED_SOURCE_RESULT_DIGEST)
        source_digest = source.result_digest
        if (
            source.sandbox_schema_version != SANDBOX_RESULT_SCHEMA_VERSION
            or source.adapter_version != SANDBOX_ADAPTER_VERSION
        ):
            return _unavailable(source_digest)

        source_bytes = canonical_result_bytes(source)
        source_invariants = _source_invariants(source)
        if len(source.issues) > MAX_EXPLANATION_ISSUES or not source.issues:
            return _unavailable(source_digest)

        allowlist = _parse_model(SandboxExplanationAllowlistBundleV1, allowlist_input)
        if allowlist is None:
            return _unavailable(source_digest)
        allowlist_bytes = canonical_explanation_bytes(allowlist)
        allowlist_invariants = _allowlist_invariants(allowlist)
        verifier_text_by_rule = dict(allowlist_invariants[1])
        expected_rule_ids = frozenset(issue.rule_id for issue in source.issues)
        actual_rule_ids = frozenset(verifier_text_by_rule)
        if expected_rule_ids != actual_rule_ids:
            return _unavailable(source_digest)

        issue_rule_by_id = {
            issue.issue_id: issue.rule_id for issue in source.issues
        }
        source_order = {
            issue.issue_id: position for position, issue in enumerate(source.issues)
        }
        request = SandboxExplanationPortInputV1(
            result_digest=source.result_digest,
            decision=source.decision,
            issue_refs=tuple(
                SandboxExplanationIssueRefV1(
                    issue_id=issue.issue_id,
                    rule_id=issue.rule_id,
                    severity=issue.severity,
                )
                for issue in source.issues
            ),
            allowlist_entries=tuple(
                SandboxExplanationAllowlistEntryV1(
                    rule_id=rule_id,
                    text=text,
                )
                for rule_id, text in allowlist_invariants[1]
            ),
        )
        try:
            candidate_input = self._port.generate(request)
        except Exception:
            return _unavailable(source_digest)

        if not _source_is_unchanged(
            source_input,
            source_bytes,
            source_invariants,
        ) or not _allowlist_is_unchanged(
            allowlist_input,
            allowlist_bytes,
            allowlist_invariants,
        ):
            return _unavailable(source_digest)

        candidate = _parse_model(SandboxExplanationCandidateV1, candidate_input)
        if candidate is None or not candidate.statements:
            return _unavailable(source_digest)
        if len(canonical_explanation_bytes(candidate)) > MAX_EXPLANATION_BYTES:
            return _unavailable(source_digest)
        if len(candidate.statements) > len(source.issues):
            return _unavailable(source_digest)

        statement_issue_ids = tuple(
            statement.issue_id for statement in candidate.statements
        )
        if len(statement_issue_ids) != len(set(statement_issue_ids)):
            return _unavailable(source_digest)
        for statement in candidate.statements:
            source_rule_id = issue_rule_by_id.get(statement.issue_id)
            if (
                source_rule_id is None
                or statement.rule_id != source_rule_id
                or verifier_text_by_rule.get(statement.rule_id) != statement.text
            ):
                return _unavailable(source_digest)

        canonical_candidate = tuple(
            sorted(
                candidate.statements,
                key=lambda statement: source_order[statement.issue_id],
            )
        )
        statements = tuple(
            SandboxExplanationStatementV1(
                issue_id=statement.issue_id,
                rule_id=statement.rule_id,
                text=statement.text,
            )
            for statement in canonical_candidate
        )
        result = _build_result(
            source_result_digest=source_digest,
            status=SandboxExplanationStatus.ATTACHED,
            statements=statements,
        )
        if len(canonical_explanation_bytes(result)) > MAX_EXPLANATION_BYTES:
            return _unavailable(source_digest)
        if not _source_is_unchanged(
            source_input,
            source_bytes,
            source_invariants,
        ) or not _allowlist_is_unchanged(
            allowlist_input,
            allowlist_bytes,
            allowlist_invariants,
        ):
            return _unavailable(source_digest)
        return result


SandboxSafetyExplanationAgent = SandboxSafetyExplanationAdapter
