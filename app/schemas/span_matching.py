"""Shared evidence-span matching used across every R9 grounding check.

Model products are produced against privacy-masked content
(``project_model_input_identity_sequences``): identity sequences are replaced
with equal-length ``█`` runs, so a model-emitted quote may hold ``█`` where the
raw answer holds digits/letters.  Every grounding check compares through the
same wildcard-aware matcher so no implementation drifts (R9-D D3).
"""

from __future__ import annotations

MASK_CHAR = "█"


def span_quote_matches(content: str, start: int, end: int, quote: str) -> bool:
    """True when ``content[start:end]`` matches ``quote`` byte-for-byte,
    allowing a mask character in ``quote`` to match any single raw character
    (masking is equal-length, so offsets stay aligned)."""

    if end <= start or end > len(content) or len(quote) != end - start:
        return False
    raw = content[start:end]
    if raw == quote:
        return True
    return all(mask == MASK_CHAR or raw_char == mask for raw_char, mask in zip(raw, quote, strict=True))


def find_quote_span(content: str, quote: str) -> tuple[int, int] | None:
    """Return the first half-open ``[start, end)`` whose slice matches ``quote``
    (mask-wildcard aware), or ``None`` when the quote is not present."""

    width = len(quote)
    if width == 0 or width > len(content):
        return None
    for start in range(len(content) - width + 1):
        if span_quote_matches(content, start, start + width, quote):
            return start, start + width
    return None


__all__ = ["MASK_CHAR", "find_quote_span", "span_quote_matches"]
