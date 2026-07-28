"""L8-2 沙盒安全/隐私/预算/Prompt Injection 参考实现。

Copyright (c) 2026 xuanhu. All rights reserved.

本模块为 L8-SBX 子任务 L8-2 提供离线、确定性的安全边界组件：

- PrivacyPolicy：写入前的有限 redaction，不确定时 fail-closed。
- CapabilityScope：操作为闭集 allowlist，未授权能力必须拒绝。
- BudgetLedger：原子 reserve/consume/release，限制 model calls/tokens/
  deadline/retries；同一幂等键不可重复扣费。
- PromptInjectionGuard：对 untrusted text 做有限分类，禁止把 untrusted 指令
  提升为 system/policy；命中高风险时返回固定无 payload 错误。

所有数据均为固定合成内容，不涉及真实患者、临床或公开数据。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Schema & resource constants
# ---------------------------------------------------------------------------

SANDBOX_PRIVACY_POLICY_SCHEMA_VERSION: Literal["sandbox-privacy-policy.v1"] = "sandbox-privacy-policy.v1"
SANDBOX_CAPABILITY_SCOPE_SCHEMA_VERSION: Literal["sandbox-capability-scope.v1"] = "sandbox-capability-scope.v1"
SANDBOX_BUDGET_LEDGER_SCHEMA_VERSION: Literal["sandbox-budget-ledger.v1"] = "sandbox-budget-ledger.v1"
SANDBOX_PROMPT_INJECTION_GUARD_SCHEMA_VERSION: Literal["sandbox-prompt-injection-guard.v1"] = (
    "sandbox-prompt-injection-guard.v1"
)
SANDBOX_SECURITY_RESULT_SCHEMA_VERSION: Literal["sandbox-security-result.v1"] = "sandbox-security-result.v1"
SANDBOX_SECURITY_ADAPTER_VERSION: Literal["sandbox-security-adapter.v1"] = "sandbox-security-adapter.v1"

_MAX_REDACTION_FIELDS = 128
_MAX_BUDGET_SLOTS = 128
_MAX_BUDGET_DEADLINE_SECONDS = 86400
_MAX_RETRIES = 100
_MAX_IDEMPOTENCY_ENTRIES = 1024
_MAX_UNTRUSTED_TEXT_BYTES = 65536
_MAX_CANONICAL_BYTES = 256 * 1024

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

# Keywords indicating prompt injection attempt
_HIGH_RISK_PATTERNS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all instructions",
    "ignore all previous",
    "ignore prior instructions",
    "ignore prior",
    "forget all instructions",
    "forget previous instructions",
    "override system prompt",
    "override your instructions",
    "you are now",
    "act as if",
    "pretend to be",
    "from now on you are",
    "new instructions",
    "system override",
    "you must follow these instructions",
    "disregard previous",
    "do not follow",
    "you are not",
    "you should not",
    "say you can't",
    "respond as",
)

# Sensitive field name markers for redaction
_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "apikey",
        "api_key",
        "api-key",
        "auth",
        "authorization",
        "credential",
        "private_key",
        "private-key",
        "access_key",
        "access-key",
        "session_token",
        "session-key",
        "certificate",
        "cert",
        "key",
    }
)


class SandboxSecurityError(ValueError):
    """A fixed, payload-free, fail-closed security error."""

    __slots__ = ()

    def __init__(self, code: str) -> None:
        super().__init__(code)


class SandboxSecurityFailureCode(StrEnum):
    PRIVACY_REDACTION_FAILED = "SANDBOX_SECURITY_PRIVACY_REDACTION_FAILED"
    PRIVACY_UNCERTAIN = "SANDBOX_SECURITY_PRIVACY_UNCERTAIN"
    PRIVACY_LIMIT_EXCEEDED = "SANDBOX_SECURITY_PRIVACY_LIMIT_EXCEEDED"
    CAPABILITY_UNAUTHORIZED = "SANDBOX_SECURITY_CAPABILITY_UNAUTHORIZED"
    CAPABILITY_DRIFT_DETECTED = "SANDBOX_SECURITY_CAPABILITY_DRIFT_DETECTED"
    BUDGET_INSUFFICIENT = "SANDBOX_SECURITY_BUDGET_INSUFFICIENT"
    BUDGET_IDEMPOTENCY_CONFLICT = "SANDBOX_SECURITY_BUDGET_IDEMPOTENCY_CONFLICT"
    BUDGET_DEADLINE_EXCEEDED = "SANDBOX_SECURITY_BUDGET_DEADLINE_EXCEEDED"
    BUDGET_RETRIES_EXCEEDED = "SANDBOX_SECURITY_BUDGET_RETRIES_EXCEEDED"
    PROMPT_INJECTION_HIGH_RISK = "SANDBOX_SECURITY_PROMPT_INJECTION_HIGH_RISK"
    PROMPT_INJECTION_LIMIT_EXCEEDED = "SANDBOX_SECURITY_PROMPT_INJECTION_LIMIT_EXCEEDED"
    INTERNAL_FAILURE = "SANDBOX_SECURITY_INTERNAL_FAILURE"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# Enum types for capability closed-set
# ---------------------------------------------------------------------------


class CapabilityOp(StrEnum):
    """Closed set of allowed capabilities."""

    READ_STATE = "read_state"
    READ_EVIDENCE = "read_evidence"
    WRITE_STATE = "write_state"
    WRITE_DATABASE = "write_database"
    TRANSITION_STAGE = "transition_stage"
    INVOKE_MODEL = "invoke_model"
    INVOKE_TOOL = "invoke_tool"
    ACCESS_STORE = "access_store"


class InjectionRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Privacy-related DTOs
# ---------------------------------------------------------------------------


class PrivacyRedactionFieldV1(_StrictFrozenModel):
    """A single redaction rule: field path pattern and action."""

    field_pattern: str = Field(min_length=1, max_length=256)
    redact_with: str = Field(default="[REDACTED]", min_length=1, max_length=64)


class PrivacyPolicyV1(_StrictFrozenModel):
    """Privacy policy: pre-write redaction rules, fail-closed when uncertain.

    Schema versioning ensures forward compatibility.
    """

    schema_version: Literal["sandbox-privacy-policy.v1"]
    redaction_fields: tuple[PrivacyRedactionFieldV1, ...] = Field(
        max_length=_MAX_REDACTION_FIELDS,
    )
    fail_closed_on_uncertain: bool = True
    policy_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def policy_digest_is_derived(self) -> PrivacyPolicyV1:
        expected = _derive_privacy_policy_digest(
            redaction_fields=self.redaction_fields,
            fail_closed_on_uncertain=self.fail_closed_on_uncertain,
        )
        if self.policy_digest != expected:
            raise ValueError("policy_digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        redaction_fields: Sequence[PrivacyRedactionFieldV1],
        fail_closed_on_uncertain: bool = True,
    ) -> PrivacyPolicyV1:
        if len(redaction_fields) > _MAX_REDACTION_FIELDS:
            raise ValueError("privacy redaction field limit exceeded") from None
        canonical_fields = tuple(
            sorted(
                tuple(redaction_fields),
                key=lambda f: f.field_pattern,
            )
        )
        policy_digest = _derive_privacy_policy_digest(
            redaction_fields=canonical_fields,
            fail_closed_on_uncertain=fail_closed_on_uncertain,
        )
        return cls(
            schema_version="sandbox-privacy-policy.v1",
            redaction_fields=canonical_fields,
            fail_closed_on_uncertain=fail_closed_on_uncertain,
            policy_digest=policy_digest,
        )


class PrivacyRedactionResultV1(_StrictFrozenModel):
    """Result of a redaction pass."""

    redacted: bool
    fields_redacted: tuple[str, ...] = ()
    uncertain: bool = False
    result_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def result_digest_is_derived(self) -> PrivacyRedactionResultV1:
        expected = _canonical_sha256(
            {
                "redacted": self.redacted,
                "fields_redacted": list(self.fields_redacted),
                "uncertain": self.uncertain,
            }
        )
        if self.result_digest != expected:
            raise ValueError("result_digest mismatch")
        return self


# ---------------------------------------------------------------------------
# Capability-related DTOs
# ---------------------------------------------------------------------------


class CapabilityScopeV1(_StrictFrozenModel):
    """Closed-set allowlist for capabilities.

    Each operation must be explicitly allowed. Undefined capabilities
    and drift detection are rejected.
    """

    schema_version: Literal["sandbox-capability-scope.v1"]
    allowed_ops: frozenset[CapabilityOp] = frozenset()
    scope_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def scope_digest_is_derived(self) -> CapabilityScopeV1:
        expected = _canonical_sha256(sorted(str(op) for op in self.allowed_ops))
        if self.scope_digest != expected:
            raise ValueError("scope_digest mismatch")
        return self

    @classmethod
    def build(cls, *, allowed_ops: frozenset[CapabilityOp]) -> CapabilityScopeV1:
        scope_digest = _canonical_sha256(sorted(str(op) for op in allowed_ops))
        return cls(
            schema_version="sandbox-capability-scope.v1",
            allowed_ops=allowed_ops,
            scope_digest=scope_digest,
        )


# ---------------------------------------------------------------------------
# Budget-related DTOs
# ---------------------------------------------------------------------------


class BudgetSlotV1(_StrictFrozenModel):
    """A single budget allocation slot."""

    slot_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    max_model_calls: int = Field(ge=0, le=10000)
    max_total_tokens: int = Field(ge=0, le=10_000_000)
    max_retries: int = Field(ge=0, le=_MAX_RETRIES)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        """Reject ambiguous local-time deadlines before comparisons occur."""
        if value.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value


class BudgetStateV1(_StrictFrozenModel):
    """Snapshot of the current budget consumption state."""

    slot_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    model_calls_used: int = Field(ge=0)
    total_tokens_used: int = Field(ge=0)
    retries_used: int = Field(ge=0)
    is_exhausted: bool = False


class BudgetConsumptionV1(_StrictFrozenModel):
    """Durable idempotent usage record carried by a ledger snapshot."""

    idempotency_key: str = Field(min_length=1, max_length=256)
    slot_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    model_calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    retries: int = Field(ge=0)


class BudgetLedgerV1(_StrictFrozenModel):
    """Stable ledger of budget allocation and consumption.

    Supports reserve/consume/release with idempotent-key dedup.
    """

    schema_version: Literal["sandbox-budget-ledger.v1"]
    slots: tuple[BudgetSlotV1, ...] = Field(max_length=_MAX_BUDGET_SLOTS)
    consumed: tuple[BudgetConsumptionV1, ...] = Field(
        default=(),
        max_length=_MAX_IDEMPOTENCY_ENTRIES,
    )
    idempotency_used: frozenset[str] = frozenset()
    ledger_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def ledger_digest_is_derived(self) -> BudgetLedgerV1:
        expected = _derive_ledger_digest(self.slots, self.consumed, self.idempotency_used)
        if self.ledger_digest != expected:
            raise ValueError("ledger_digest mismatch")
        if self.idempotency_used != frozenset(item.idempotency_key for item in self.consumed):
            raise ValueError("idempotency_used mismatch")
        if len({slot.slot_id for slot in self.slots}) != len(self.slots):
            raise ValueError("budget slot ids must be unique")
        if len({item.idempotency_key for item in self.consumed}) != len(self.consumed):
            raise ValueError("budget idempotency keys must be unique")
        slots = {slot.slot_id: slot for slot in self.slots}
        usage: dict[str, tuple[int, int, int]] = {}
        for item in self.consumed:
            slot = slots.get(item.slot_id)
            if slot is None:
                raise ValueError("budget consumption references unknown slot")
            calls, tokens, retries = usage.get(item.slot_id, (0, 0, 0))
            usage[item.slot_id] = (
                calls + item.model_calls,
                tokens + item.tokens,
                retries + item.retries,
            )
        for slot_id, (calls, tokens, retries) in usage.items():
            slot = slots[slot_id]
            if calls > slot.max_model_calls:
                raise ValueError("budget model-call limit exceeded")
            if tokens > slot.max_total_tokens:
                raise ValueError("budget token limit exceeded")
            if retries > slot.max_retries:
                raise ValueError("budget retry limit exceeded")
        return self

    @classmethod
    def build(
        cls,
        slots: Sequence[BudgetSlotV1],
        consumed: Sequence[BudgetConsumptionV1] = (),
    ) -> BudgetLedgerV1:
        if len(slots) > _MAX_BUDGET_SLOTS or len(consumed) > _MAX_IDEMPOTENCY_ENTRIES:
            raise ValueError("budget ledger limit exceeded") from None
        canonical_slots = tuple(
            sorted(
                tuple(slots),
                key=lambda s: s.slot_id,
            )
        )
        canonical_consumed = tuple(sorted(tuple(consumed), key=lambda item: item.idempotency_key))
        used = frozenset(item.idempotency_key for item in canonical_consumed)
        ledger_digest = _derive_ledger_digest(canonical_slots, canonical_consumed, used)
        return cls(
            schema_version="sandbox-budget-ledger.v1",
            slots=canonical_slots,
            consumed=canonical_consumed,
            idempotency_used=used,
            ledger_digest=ledger_digest,
        )


# ---------------------------------------------------------------------------
# Prompt injection DTOs
# ---------------------------------------------------------------------------


class PromptInjectionClassificationV1(_StrictFrozenModel):
    """Classification of untrusted text for injection risk."""

    risk_level: InjectionRiskLevel
    matched_patterns: tuple[str, ...] = ()
    classification_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_is_derived(self) -> PromptInjectionClassificationV1:
        expected = _canonical_sha256(
            {
                "risk_level": self.risk_level,
                "matched_patterns": list(self.matched_patterns),
            }
        )
        if self.classification_digest != expected:
            raise ValueError("classification_digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        risk_level: InjectionRiskLevel,
        matched_patterns: Sequence[str] = (),
    ) -> PromptInjectionClassificationV1:
        canonical_patterns = tuple(sorted(set(matched_patterns)))
        digest = _canonical_sha256(
            {
                "risk_level": risk_level,
                "matched_patterns": list(canonical_patterns),
            }
        )
        return cls(
            risk_level=risk_level,
            matched_patterns=canonical_patterns,
            classification_digest=digest,
        )


class PromptInjectionGuardV1(_StrictFrozenModel):
    """Guard result for prompt injection detection."""

    schema_version: Literal["sandbox-prompt-injection-guard.v1"]
    allowed: bool
    classification: PromptInjectionClassificationV1
    guard_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def guard_digest_is_derived(self) -> PromptInjectionGuardV1:
        expected = _canonical_sha256(
            {
                "allowed": self.allowed,
                "classification": self.classification.model_dump(mode="json"),
            }
        )
        if self.guard_digest != expected:
            raise ValueError("guard_digest mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        allowed: bool,
        classification: PromptInjectionClassificationV1,
    ) -> PromptInjectionGuardV1:
        guard_digest = _canonical_sha256(
            {
                "allowed": allowed,
                "classification": classification.model_dump(mode="json"),
            }
        )
        return cls(
            schema_version="sandbox-prompt-injection-guard.v1",
            allowed=allowed,
            classification=classification,
            guard_digest=guard_digest,
        )


# ---------------------------------------------------------------------------
# Top-level security result
# ---------------------------------------------------------------------------


class SandboxSecurityResultV1(_StrictFrozenModel):
    """Composite result of all L8-2 security checks."""

    schema_version: Literal["sandbox-security-result.v1"]
    adapter_version: Literal["sandbox-security-adapter.v1"]
    privacy_result: PrivacyRedactionResultV1 | None = None
    capability_allowed: bool = False
    budget_allowed: bool = False
    injection_guard: PromptInjectionGuardV1 | None = None
    overall_allowed: bool = False
    result_digest: str = Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def result_digest_is_derived(self) -> SandboxSecurityResultV1:
        expected = _derive_security_result_digest(self)
        if self.result_digest != expected:
            raise ValueError("result_digest mismatch")
        return self


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def canonical_json_bytes(value: object) -> bytes:
    """Serialize to stable canonical JSON without Nan or Python extras."""
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


def _derive_privacy_policy_digest(
    redaction_fields: tuple[PrivacyRedactionFieldV1, ...],
    fail_closed_on_uncertain: bool,
) -> str:
    return _canonical_sha256(
        {
            "redaction_fields": redaction_fields,
            "fail_closed_on_uncertain": fail_closed_on_uncertain,
        }
    )


def _derive_ledger_digest(
    slots: tuple[BudgetSlotV1, ...],
    consumed: tuple[BudgetConsumptionV1, ...],
    idempotency_used: frozenset[str],
) -> str:
    return _canonical_sha256(
        {
            "slots": slots,
            "consumed": consumed,
            "idempotency_used": sorted(idempotency_used),
        }
    )


def _derive_security_result_digest(result: SandboxSecurityResultV1) -> str:
    return _canonical_sha256(
        {
            "privacy_result": result.privacy_result.model_dump(mode="json") if result.privacy_result else None,
            "capability_allowed": result.capability_allowed,
            "budget_allowed": result.budget_allowed,
            "injection_guard": result.injection_guard.model_dump(mode="json") if result.injection_guard else None,
            "overall_allowed": result.overall_allowed,
        }
    )


def _raise_error(code: SandboxSecurityFailureCode) -> NoReturn:
    raise SandboxSecurityError(code.value) from None


def _field_tokens(value: str) -> tuple[str, ...]:
    normalized = value.lower()
    for separator in ("-", ".", " ", "/", "[", "]"):
        normalized = normalized.replace(separator, "_")
    return tuple(token for token in normalized.split("_") if token)


def _field_matches_rule(field_name: str, pattern: str) -> bool:
    field_tokens = _field_tokens(field_name)
    pattern_tokens = _field_tokens(pattern)
    if not pattern_tokens:
        return False
    return field_tokens == pattern_tokens or (len(pattern_tokens) == 1 and pattern_tokens[0] in field_tokens)


def _contains_sensitive_marker(value: str) -> bool:
    """Return whether text contains a sensitive token on a field boundary.

    Boundary matching avoids the old ``"key" in "monkey"`` false positive
    while still treating common dotted, dashed, and underscored paths as the
    same logical field.
    """

    tokens = set(_field_tokens(value))
    sensitive_tokens = {token for marker in _SENSITIVE_FIELD_NAMES for token in _field_tokens(marker)}
    return bool(tokens.intersection(sensitive_tokens))


# ---------------------------------------------------------------------------
# PrivacyPolicy — pre-write redaction engine
# ---------------------------------------------------------------------------


class PrivacyPolicy:
    """Pre-write redaction engine with fail-closed semantics.

    Applies configured redaction rules to arbitrary dict data before write.
    When uncertain (unrecognized schema, conflicting rules), fails closed.
    """

    __slots__ = ("_policy",)

    def __init__(self, policy: PrivacyPolicyV1) -> None:
        self._policy = policy

    @property
    def policy(self) -> PrivacyPolicyV1:
        return self._policy

    def redact(self, data: Mapping[str, object]) -> PrivacyRedactionResultV1:
        """Apply redaction rules to *data*.

        Returns a result with redacted fields. If uncertain, fails closed.
        """
        unexpected_failure = False
        try:
            return self._redact(data)
        except SandboxSecurityError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxSecurityFailureCode.INTERNAL_FAILURE)
        raise AssertionError("unreachable")

    def redact_payload(self, data: Mapping[str, object]) -> tuple[dict[str, object], PrivacyRedactionResultV1]:
        """Return a sanitized shallow payload and its audit result.

        This helper is intentionally limited to mapping fields.  Nested
        structures are treated as uncertain rather than recursively guessed;
        callers must provide an explicit policy for each nested boundary.
        """
        if not isinstance(data, Mapping):
            _raise_error(SandboxSecurityFailureCode.PRIVACY_REDACTION_FAILED)
        result = self.redact(data)
        if result.uncertain:
            _raise_error(SandboxSecurityFailureCode.PRIVACY_UNCERTAIN)
        redacted_fields = set(result.fields_redacted)
        sanitized: dict[str, object] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                _raise_error(SandboxSecurityFailureCode.PRIVACY_REDACTION_FAILED)
            key_text = key
            if key_text in redacted_fields:
                rule = next(
                    (
                        item
                        for item in self._policy.redaction_fields
                        if _field_matches_rule(key_text, item.field_pattern)
                    ),
                    None,
                )
                sanitized[key_text] = rule.redact_with if rule is not None else "[REDACTED]"
            else:
                sanitized[key_text] = value
        return sanitized, result

    def _redact(self, data: Mapping[str, object]) -> PrivacyRedactionResultV1:
        if not isinstance(data, Mapping):
            _raise_error(SandboxSecurityFailureCode.PRIVACY_REDACTION_FAILED)

        redacted_fields: list[str] = []
        uncertain = False

        for field_name, value in data.items():
            if not isinstance(field_name, str):
                _raise_error(SandboxSecurityFailureCode.PRIVACY_REDACTION_FAILED)
            # Nested or opaque values cannot be safely redacted by this
            # intentionally shallow reference implementation.
            if isinstance(value, (Mapping, list, tuple, set, frozenset)):
                uncertain = True
                continue
            for rule in self._policy.redaction_fields:
                if _field_matches_rule(field_name, rule.field_pattern) or field_name in _SENSITIVE_FIELD_NAMES:
                    redacted_fields.append(field_name)
                    break

        if not redacted_fields:
            # No sensitive patterns matched - still check for uncertain cases
            for field_name, value in data.items():
                if not isinstance(field_name, str):
                    uncertain = True
                    break
                if _contains_sensitive_marker(field_name):
                    uncertain = True
                    break
                if isinstance(value, str) and _contains_sensitive_marker(value):
                    uncertain = True
                    break

        if uncertain and self._policy.fail_closed_on_uncertain:
            _raise_error(SandboxSecurityFailureCode.PRIVACY_UNCERTAIN)

        result = PrivacyRedactionResultV1(
            redacted=len(redacted_fields) > 0,
            fields_redacted=tuple(sorted(set(redacted_fields))),
            uncertain=uncertain,
            result_digest=_canonical_sha256(
                {
                    "redacted": len(redacted_fields) > 0,
                    "fields_redacted": sorted(set(redacted_fields)),
                    "uncertain": uncertain,
                }
            ),
        )
        return result


# ---------------------------------------------------------------------------
# CapabilityScope — closed-set allowlist enforcement
# ---------------------------------------------------------------------------


class CapabilityScope:
    """Enforce closed-set capability allowlist.

    Each requested operation must be explicitly allowed. Drift detection
    ensures no unrecognized capabilities can pass.
    """

    __slots__ = ("_scope",)

    def __init__(self, scope: CapabilityScopeV1) -> None:
        self._scope = scope

    @property
    def scope(self) -> CapabilityScopeV1:
        return self._scope

    def authorize(self, op: CapabilityOp | str) -> bool:
        """Check if *op* is authorized.

        Returns True if allowed, raises SandboxSecurityError otherwise.
        """
        unexpected_failure = False
        try:
            return self._authorize(op)
        except SandboxSecurityError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxSecurityFailureCode.INTERNAL_FAILURE)
        raise AssertionError("unreachable")

    def _authorize(self, op: CapabilityOp | str) -> bool:
        if isinstance(op, str):
            try:
                op = CapabilityOp(op)
            except ValueError:
                _raise_error(SandboxSecurityFailureCode.CAPABILITY_UNAUTHORIZED)

        if op not in CapabilityOp:
            _raise_error(SandboxSecurityFailureCode.CAPABILITY_UNAUTHORIZED)

        if op not in self._scope.allowed_ops:
            _raise_error(SandboxSecurityFailureCode.CAPABILITY_UNAUTHORIZED)

        return True

    def authorize_batch(self, ops: Sequence[CapabilityOp | str]) -> bool:
        """Authorize multiple ops atomically.

        All must be allowed; if any fails, all are rejected.
        """
        return all(self.authorize(op) for op in ops)


# ---------------------------------------------------------------------------
# BudgetLedger — atomic reserve/consume/release with idempotent-key dedup
# ---------------------------------------------------------------------------


class BudgetLedger:
    """Atomic budget tracking with reserve/consume/release.

    Enforces:
    - max model calls per slot
    - max total tokens per slot
    - deadline
    - max retries
    - idempotent-key dedup (same key cannot be consumed twice)
    """

    __slots__ = ("_ledger", "_lock", "_consumed")

    def __init__(self, ledger: BudgetLedgerV1) -> None:
        self._ledger = ledger
        self._lock = threading.RLock()
        # consumed[idempotency_key] = (slot_id, calls, tokens, retries).
        # A zero-usage tuple is a reservation that may be finalized exactly once.
        self._consumed: dict[str, tuple[str, int, int, int]] = {
            item.idempotency_key: (item.slot_id, item.model_calls, item.tokens, item.retries)
            for item in ledger.consumed
        }

    @property
    def ledger(self) -> BudgetLedgerV1:
        return self._ledger

    def reserve(self, *, slot_id: str, idempotency_key: str) -> None:
        """Reserve budget for an operation.

        Checks deadline, slot existence, and idempotency before allowing.
        """
        with self._lock:
            existing = self._consumed.get(idempotency_key)
            if existing is not None:
                if existing[0] == slot_id:
                    return
                _raise_error(SandboxSecurityFailureCode.BUDGET_IDEMPOTENCY_CONFLICT)
            slot = self._find_slot(slot_id)
            now = datetime.now(UTC)
            if now >= slot.deadline_at:
                _raise_error(SandboxSecurityFailureCode.BUDGET_DEADLINE_EXCEEDED)
            # Record the reservation
            self._consumed[idempotency_key] = (slot_id, 0, 0, 0)
            self._sync_ledger()

    def consume(
        self,
        *,
        slot_id: str,
        idempotency_key: str,
        model_calls: int = 1,
        tokens: int = 0,
        retries: int = 0,
    ) -> None:
        """Consume budget. Rejects if idempotency key already consumed differently."""
        with self._lock:
            slot = self._find_slot(slot_id)
            now = datetime.now(UTC)
            if now >= slot.deadline_at:
                _raise_error(SandboxSecurityFailureCode.BUDGET_DEADLINE_EXCEEDED)

            existing = self._consumed.get(idempotency_key)
            if existing is not None:
                existing_slot, existing_calls, existing_tokens, existing_retries = existing
                if existing_slot != slot_id:
                    _raise_error(SandboxSecurityFailureCode.BUDGET_IDEMPOTENCY_CONFLICT)
                # Idempotent replay of the same completed consumption is a no-op.
                if (existing_calls, existing_tokens, existing_retries) == (model_calls, tokens, retries):
                    return
                # A zero-usage reservation is finalized by this consume call.
                if (existing_calls, existing_tokens, existing_retries) != (0, 0, 0):
                    _raise_error(SandboxSecurityFailureCode.BUDGET_IDEMPOTENCY_CONFLICT)

            # Check slot-level limits
            slot_usage = self._accumulate_slot_usage(slot_id)
            if slot_usage[0] + model_calls > slot.max_model_calls:
                _raise_error(SandboxSecurityFailureCode.BUDGET_INSUFFICIENT)
            if slot_usage[1] + tokens > slot.max_total_tokens:
                _raise_error(SandboxSecurityFailureCode.BUDGET_INSUFFICIENT)
            if slot_usage[2] + retries > slot.max_retries:
                _raise_error(SandboxSecurityFailureCode.BUDGET_RETRIES_EXCEEDED)

            self._consumed[idempotency_key] = (slot_id, model_calls, tokens, retries)
            self._sync_ledger()

    def release(self, *, slot_id: str, idempotency_key: str) -> None:
        """Release a previously reserved budget slot."""
        with self._lock:
            _ = self._find_slot(slot_id)
            existing = self._consumed.get(idempotency_key)
            if existing is not None and existing[0] != slot_id:
                _raise_error(SandboxSecurityFailureCode.BUDGET_IDEMPOTENCY_CONFLICT)
            if existing is not None:
                del self._consumed[idempotency_key]
                self._sync_ledger()

    def _sync_ledger(self) -> None:
        consumed = tuple(
            BudgetConsumptionV1(
                idempotency_key=key,
                slot_id=slot_id,
                model_calls=calls,
                tokens=tokens,
                retries=retries,
            )
            for key, (slot_id, calls, tokens, retries) in sorted(self._consumed.items())
        )
        self._ledger = BudgetLedgerV1.build(self._ledger.slots, consumed=consumed)

    def _find_slot(self, slot_id: str) -> BudgetSlotV1:
        for slot in self._ledger.slots:
            if slot.slot_id == slot_id:
                return slot
        _raise_error(SandboxSecurityFailureCode.BUDGET_INSUFFICIENT)
        raise AssertionError("unreachable")

    def _accumulate_slot_usage(self, slot_id: str) -> tuple[int, int, int]:
        total_calls = 0
        total_tokens = 0
        total_retries = 0
        for _key, (consumed_slot, calls, tokens, retries) in self._consumed.items():
            if consumed_slot != slot_id:
                continue
            total_calls += calls
            total_tokens += tokens
            total_retries += retries
        return (total_calls, total_tokens, total_retries)


# ---------------------------------------------------------------------------
# PromptInjectionGuard — classify untrusted text
# ---------------------------------------------------------------------------


class PromptInjectionGuard:
    """Classify untrusted text for injection risk.

    Maintains a set of known high-risk patterns. If any match, the
    text is classified HIGH and a fixed no-payload error is returned.
    Untrusted text must never be promoted to system/policy role.
    """

    __slots__ = ("_high_risk_patterns",)

    def __init__(self, high_risk_patterns: Sequence[str] = _HIGH_RISK_PATTERNS) -> None:
        self._high_risk_patterns = tuple(high_risk_patterns)

    def classify(self, text: str) -> PromptInjectionClassificationV1:
        """Classify untrusted text.

        Returns a classification with risk level and any matched patterns.
        For HIGH risk, the caller must reject and not forward the text
        to system/policy roles.
        """
        if len(text.encode("utf-8")) > _MAX_UNTRUSTED_TEXT_BYTES:
            _raise_error(SandboxSecurityFailureCode.PROMPT_INJECTION_LIMIT_EXCEEDED)

        text_lower = text.lower()
        matched: list[str] = []

        for pattern in self._high_risk_patterns:
            if pattern in text_lower:
                matched.append(pattern)

        risk = InjectionRiskLevel.HIGH if matched else InjectionRiskLevel.LOW

        classification = PromptInjectionClassificationV1(
            risk_level=risk,
            matched_patterns=tuple(matched),
            classification_digest=_canonical_sha256(
                {
                    "risk_level": risk,
                    "matched_patterns": list(matched),
                }
            ),
        )
        return classification

    def check(self, text: str) -> PromptInjectionGuardV1:
        """Classify and produce a guard decision.

        HIGH risk -> guard fails closed with fixed no-payload error.
        """
        unexpected_failure = False
        try:
            classification = self.classify(text)
        except SandboxSecurityError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxSecurityFailureCode.INTERNAL_FAILURE)

        allowed = classification.risk_level != InjectionRiskLevel.HIGH

        if not allowed:
            _raise_error(SandboxSecurityFailureCode.PROMPT_INJECTION_HIGH_RISK)

        return PromptInjectionGuardV1.build(
            allowed=allowed,
            classification=classification,
        )


# ---------------------------------------------------------------------------
# SandboxSecurityAdapter — composite security check
# ---------------------------------------------------------------------------


class SandboxSecurityAdapter:
    """Composite security adapter that runs all L8-2 checks."""

    __slots__ = ("_privacy", "_capability", "_budget", "_injection")

    def __init__(
        self,
        *,
        privacy: PrivacyPolicy,
        capability: CapabilityScope,
        budget: BudgetLedger,
        injection: PromptInjectionGuard,
    ) -> None:
        self._privacy = privacy
        self._capability = capability
        self._budget = budget
        self._injection = injection

    def check_all(
        self,
        *,
        data: Mapping[str, object] | None = None,
        ops: Sequence[CapabilityOp | str] | None = None,
        slot_budget: str | None = None,
        idempotency_key: str | None = None,
        untrusted_text: str | None = None,
    ) -> SandboxSecurityResultV1:
        """Run all configured security checks.

        If any check fails, overall_allowed is False and details are
        recorded in the result.
        """
        privacy_result: PrivacyRedactionResultV1 | None = None
        capability_allowed = True
        budget_allowed = True
        injection_guard: PromptInjectionGuardV1 | None = None

        if data is not None:
            try:
                privacy_result = self._privacy.redact(data)
            except SandboxSecurityError:
                privacy_result = PrivacyRedactionResultV1(
                    redacted=False,
                    uncertain=True,
                    result_digest=_canonical_sha256(
                        {
                            "redacted": False,
                            "fields_redacted": [],
                            "uncertain": True,
                        }
                    ),
                )

        if ops is not None:
            try:
                self._capability.authorize_batch(ops)
            except SandboxSecurityError:
                capability_allowed = False

        if slot_budget is not None and idempotency_key is not None:
            try:
                self._budget.reserve(slot_id=slot_budget, idempotency_key=idempotency_key)
            except SandboxSecurityError:
                budget_allowed = False

        if untrusted_text is not None:
            try:
                injection_guard = self._injection.check(untrusted_text)
            except SandboxSecurityError:
                injection_guard = PromptInjectionGuardV1.build(
                    allowed=False,
                    classification=PromptInjectionClassificationV1.build(
                        risk_level=InjectionRiskLevel.HIGH,
                    ),
                )

        overall_allowed = (
            (data is None or (privacy_result is not None and not privacy_result.uncertain))
            and capability_allowed
            and budget_allowed
            and (untrusted_text is None or (injection_guard is not None and injection_guard.allowed))
        )

        result = SandboxSecurityResultV1(
            schema_version="sandbox-security-result.v1",
            adapter_version="sandbox-security-adapter.v1",
            privacy_result=privacy_result,
            capability_allowed=capability_allowed,
            budget_allowed=budget_allowed,
            injection_guard=injection_guard,
            overall_allowed=overall_allowed,
            result_digest=_canonical_sha256(
                {
                    "privacy_result": privacy_result.model_dump(mode="json") if privacy_result else None,
                    "capability_allowed": capability_allowed,
                    "budget_allowed": budget_allowed,
                    "injection_guard": injection_guard.model_dump(mode="json") if injection_guard else None,
                    "overall_allowed": overall_allowed,
                }
            ),
        )
        return result
