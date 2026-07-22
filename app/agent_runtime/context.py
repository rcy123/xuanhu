"""Pure, privacy-preserving context and prompt boundary for L2-3.

This module deliberately has no model, state, persistence, or logging dependencies.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import string
from collections.abc import Mapping, Sequence
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

# ---------------------------------------------------------------------------
# L4.5-11-1 有限身份 scanner / projector
# ---------------------------------------------------------------------------

# 全角到ASCII的映射
_FULLWIDTH_DIGITS = {
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
}
_FULLWIDTH_X = {"Ｘ": "X", "ｘ": "x"}


def _normalize_char(ch: str) -> str | None:
    """将单个字符归一化为允许token或None(HARD)。"""
    if ch in _FULLWIDTH_DIGITS:
        return _FULLWIDTH_DIGITS[ch]
    if "0" <= ch <= "9":
        return ch
    if ch in _FULLWIDTH_X:
        return _FULLWIDTH_X[ch]
    if ch in "Xx":
        return ch
    if ch in " -.":
        return ch  # 保留具体分隔符种类
    return None  # HARD


def _tokenize(contents: Sequence[str]) -> tuple[list[tuple[str | None, int, int]], list[int]]:
    """将contents序列化为token流。

    返回:
        tokens: 列表，每个元素为(normalized_char_or_None, message_index, raw_char_index)
                None表示HARD或B边界
        message_ends: 每个message在token流中的结束索引（exclusive）
    """
    tokens: list[tuple[str | None, int, int]] = []
    message_ends: list[int] = []
    for msg_idx, content in enumerate(contents):
        if not isinstance(content, str):
            raise ContextBuilderError("invalid input: expected string sequence")
        for char_idx, ch in enumerate(content):
            norm = _normalize_char(ch)
            tokens.append((norm, msg_idx, char_idx))
        message_ends.append(len(tokens))
        # 在message之间插入虚拟边界B（最后一个message后不加）
        if msg_idx < len(contents) - 1:
            tokens.append((None, msg_idx, -1))  # None表示B边界
    return tokens, message_ends


def _find_matches(tokens: list[tuple[str | None, int, int]]) -> list[tuple[int, int]]:
    """在token流中查找所有匹配的身份序列。

    Grammar:
    - 连续手机号: 1[3-9]D{9}
    - 分隔手机号: 1[3-9]D S D{4} S D{4}，两个S必须是同一种具体字符
    - 身份证号: D{17}(D|X)
    - B可位于上述grammar任意两个字符token之间
    - 匹配前后的最近非B token若为D或X，该候选必须拒绝

    返回: 列表，每个元素为(start_idx, end_idx)，表示token流中的匹配范围（exclusive）
    """
    # B只影响最终raw span，不参与grammar或最近非B边界判断。先在线性遍历中
    # 折叠B，保留每个显著token的原始索引，避免每个数字起点重复跳B和构造候选列表。
    significant_indices: list[int] = []
    significant_chars: list[str | None] = []
    for token_index, (normalized, _, raw_char_index) in enumerate(tokens):
        if normalized is None and raw_char_index == -1:
            continue
        significant_indices.append(token_index)
        significant_chars.append(normalized)

    significant_count = len(significant_chars)
    digit_run_lengths = [0] * (significant_count + 1)
    for position in range(significant_count - 1, -1, -1):
        normalized = significant_chars[position]
        if normalized is not None and normalized in "0123456789":
            digit_run_lengths[position] = digit_run_lengths[position + 1] + 1

    def _is_digit_or_x(normalized: str | None) -> bool:
        return normalized is not None and normalized in "0123456789Xx"

    def _has_valid_boundary(start: int, end: int) -> bool:
        return not (
            (start > 0 and _is_digit_or_x(significant_chars[start - 1]))
            or (end < significant_count and _is_digit_or_x(significant_chars[end]))
        )

    selected: list[tuple[int, int]] = []
    last_end = -1
    for position, normalized in enumerate(significant_chars):
        start = significant_indices[position]
        if start < last_end or normalized is None or normalized not in "0123456789":
            continue

        best_end_position = -1

        if normalized == "1" and position + 1 < significant_count:
            second = significant_chars[position + 1]
            if second is not None and second in "3456789":
                # 连续手机号: 1[3-9]D{9}
                continuous_end = position + 11
                if digit_run_lengths[position] >= 11 and _has_valid_boundary(position, continuous_end):
                    best_end_position = continuous_end

                # 分隔手机号: 1[3-9]D S D{4} S D{4}
                separated_end = position + 13
                if position + 12 < significant_count and digit_run_lengths[position] >= 3:
                    separator = significant_chars[position + 3]
                    if (
                        separator is not None
                        and separator in " -."
                        and digit_run_lengths[position + 4] >= 4
                        and significant_chars[position + 8] == separator
                        and digit_run_lengths[position + 9] >= 4
                        and _has_valid_boundary(position, separated_end)
                    ):
                        best_end_position = separated_end

        # 身份证号: D{17}(D|X)。同起点最长优先，仍同长时身份证优先。
        id_end = position + 18
        if (
            position + 17 < significant_count
            and digit_run_lengths[position] >= 17
            and _is_digit_or_x(significant_chars[position + 17])
            and _has_valid_boundary(position, id_end)
            and id_end >= best_end_position
        ):
            best_end_position = id_end

        if best_end_position < 0:
            continue

        end = significant_indices[best_end_position - 1] + 1
        # 身份证在上方以>=最后参与择优；同end时其优先级被保留，且range可观察结果相同。
        selected.append((start, end))
        last_end = end

    return selected


def _apply_mask(contents: Sequence[str], tokens: list[tuple[str | None, int, int]], matches: list[tuple[int, int]]) -> tuple[str, ...]:
    """将匹配结果回写到原始message，生成投影副本。

    对每个命中的token位置写入'█'，B边界不写入任何字符。
    返回新的tuple[str, ...]。
    """
    # 为每个message创建字符列表
    result_chars: list[list[str]] = []
    for content in contents:
        result_chars.append(list(content))

    for start, end in matches:
        for idx in range(start, end):
            token = tokens[idx]
            norm, msg_idx, char_idx = token
            if norm is None and char_idx == -1:
                # B边界，不写入
                continue
            if msg_idx >= 0 and char_idx >= 0:
                result_chars[msg_idx][char_idx] = "█"

    return tuple("".join(chars) for chars in result_chars)


def contains_model_input_identity_sequence(contents: Sequence[str]) -> bool:
    """检查contents中是否包含有限身份序列。"""
    try:
        tokens, _ = _tokenize(contents)
        return bool(_find_matches(tokens))
    except Exception:
        pass
    raise ContextBuilderError("identity sequence processing failed") from None


def project_model_input_identity_sequences(contents: Sequence[str]) -> tuple[str, ...]:
    """对contents中的身份序列进行等长投影遮罩。

    使用'█'（U+2588）作为唯一遮罩字符。
    返回新的tuple[str, ...]，每条message长度与原文相同。
    """
    try:
        tokens, _ = _tokenize(contents)
        matches = _find_matches(tokens)
        return _apply_mask(contents, tokens, matches)
    except Exception:
        pass
    raise ContextBuilderError("identity sequence processing failed") from None




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
