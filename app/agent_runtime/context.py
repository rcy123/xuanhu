"""Pure, privacy-preserving context and prompt boundary for L2-3.

This module deliberately has no model, state, persistence, or logging dependencies.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import string
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PromptLayer(StrEnum):
    SYSTEM = "system"
    DEVELOPER = "developer"
    CONTEXT = "context"
    USER = "user"


class ContextBuilderError(ValueError):
    """Deterministic rejection of unsafe context or template input."""


class TemplateValidationError(ContextBuilderError):
    pass


class TokenBudgetExceeded(ContextBuilderError):
    pass


class PseudonymKeyUnavailable(ContextBuilderError):
    """Raised when identity projection needs a runtime-injected key."""


class PseudonymKeyProvider(Protocol):
    """A secret-store adapter. Implementations must not expose the key in logs."""

    def get_pseudonym_key(self) -> bytes: ...


class PromptMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: PromptLayer
    content: str = Field(min_length=1)


class TokenBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    limit: int = Field(gt=0)
    used: int = Field(ge=0)
    strategy: str = "reject"

    @property
    def remaining(self) -> int:
        return self.limit - self.used


class ContextPacket(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fields: dict[str, Any] = Field(default_factory=dict)
    messages: tuple[PromptMessage, ...] = ()
    token_budget: TokenBudget

    @field_validator("messages")
    @classmethod
    def ordered_layers(cls, value: tuple[PromptMessage, ...]) -> tuple[PromptMessage, ...]:
        order = {layer: i for i, layer in enumerate(PromptLayer)}
        if any(order[value[i].role] > order[value[i + 1].role] for i in range(len(value) - 1)):
            raise ValueError("prompt layers must be ordered system, developer, context, user")
        return value


_PII_KEYS = frozenset({"name", "patient_name", "phone", "mobile", "id_card", "identity_card", "outpatient_no", "medical_record_no"})
_PII_PATTERNS = (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"))


def pseudonym(value: Any, *, key: bytes | None = None) -> str:
    """Return a stable non-reversible identifier using an injected secret key."""
    if not key:
        raise PseudonymKeyUnavailable("pseudonym key is unavailable")
    digest = hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()[:16]
    return f"subject-{digest}"


def _redact_free_text(value: str) -> str:
    for pattern in _PII_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _project(value: Any, allowed: frozenset[str], pseudonym_key: bytes | None, *, root: bool) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for name, item in value.items():
            if root and name not in allowed:
                continue
            if name in _PII_KEYS:
                if pseudonym_key is None:
                    raise PseudonymKeyUnavailable("pseudonym key is unavailable")
                result[name] = pseudonym(item, key=pseudonym_key)
            else:
                result[name] = _project(item, allowed, pseudonym_key, root=False)
        return result
    if isinstance(value, list):
        return [_project(item, allowed, pseudonym_key, root=False) for item in value]
    if isinstance(value, str):
        return _redact_free_text(value)
    return value


class ContextBuilder:
    """Build a bounded, immutable packet from explicitly allowed fields."""

    def __init__(
        self,
        *,
        allowed_fields: set[str] | frozenset[str],
        token_limit: int,
        overflow: str = "reject",
        pseudonym_key: bytes | None = None,
        key_provider: PseudonymKeyProvider | None = None,
    ) -> None:
        if overflow not in {"reject", "truncate"}:
            raise ValueError("overflow must be reject or truncate")
        if pseudonym_key is not None and key_provider is not None:
            raise ValueError("provide pseudonym_key or key_provider, not both")
        self.allowed_fields = frozenset(allowed_fields)
        self.token_limit = token_limit
        self.overflow = overflow
        self._pseudonym_key = pseudonym_key
        self._key_provider = key_provider

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Conservative, deterministic estimate: Unicode chars and ASCII words both count.
        return max(1, (len(text) + 3) // 4)

    def project(self, source: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _project(source, self.allowed_fields, self._resolve_pseudonym_key(), root=True))

    def _resolve_pseudonym_key(self) -> bytes | None:
        if self._pseudonym_key is not None:
            return self._pseudonym_key
        if self._key_provider is None:
            return None
        key = self._key_provider.get_pseudonym_key()
        if not key:
            raise PseudonymKeyUnavailable("pseudonym key is unavailable")
        return key

    def build(self, *, system: str, developer: str, context: Mapping[str, Any] | str = "", user: str = "") -> ContextPacket:
        projected = (
            self.project(context)
            if isinstance(context, Mapping)
            else _project(context, self.allowed_fields, self._resolve_pseudonym_key(), root=True)
        )
        context_text = self._serialize(projected)
        messages = tuple(PromptMessage(role=role, content=text) for role, text in (
            (PromptLayer.SYSTEM, system), (PromptLayer.DEVELOPER, developer),
            (PromptLayer.CONTEXT, context_text), (PromptLayer.USER, user),
        ) if text)
        used = sum(self.estimate_tokens(message.content) for message in messages)
        if used > self.token_limit:
            if self.overflow == "reject":
                raise TokenBudgetExceeded(f"prompt token budget exceeded: {used}>{self.token_limit}")
            messages = self._truncate(messages)
            used = sum(self.estimate_tokens(message.content) for message in messages)
        return ContextPacket(fields=projected if isinstance(projected, dict) else {}, messages=messages,
                             token_budget=TokenBudget(limit=self.token_limit, used=used, strategy=self.overflow))

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _truncate(self, messages: tuple[PromptMessage, ...]) -> tuple[PromptMessage, ...]:
        remaining = self.token_limit
        result: list[PromptMessage] = []
        for message in messages:
            allowance = remaining * 4
            content = message.content[:allowance]
            result.append(PromptMessage(role=message.role, content=content))
            remaining -= self.estimate_tokens(content)
            if remaining <= 0:
                break
        return tuple(result)


def render_template(template: str, variables: Mapping[str, Any], *, authorized: set[str] | frozenset[str]) -> str:
    """Strictly render simple ``{name}`` placeholders; no attribute/index access."""
    fields = [field for _, field, _, _ in string.Formatter().parse(template) if field]
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field) for field in fields):
        raise TemplateValidationError("template contains an invalid variable")
    expected = set(fields)
    allowed = frozenset(authorized)
    if expected - allowed:
        raise TemplateValidationError(f"unauthorized template variables: {sorted(expected - allowed)}")
    if expected - set(variables):
        raise TemplateValidationError(f"missing template variables: {sorted(expected - set(variables))}")
    if set(variables) - expected:
        raise TemplateValidationError(f"unknown template variables: {sorted(set(variables) - expected)}")
    return template.format_map(dict(variables))
