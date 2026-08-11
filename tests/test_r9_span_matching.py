"""Focused R9-D tests for the shared mask-wildcard span matcher (D3).

Identity sequences are masked with equal-length ``█`` before the model sees
them, so a model quote may hold ``█`` where the raw answer has digits.  Every
grounding check routes through these helpers so offsets stay aligned and no
implementation drifts.
"""

from __future__ import annotations

from app.schemas.span_matching import MASK_CHAR, find_quote_span, span_quote_matches


def test_exact_match_passes() -> None:
    assert span_quote_matches("有痰，痰是白色的", 0, 2, "有痰")
    assert span_quote_matches("有痰", 0, 2, "有痰")


def test_mask_wildcard_matches_identity_sequence() -> None:
    # Raw answer holds a phone number; the model saw it masked with █.
    raw = "我的电话是13812345678，痰是白色的"
    quote = f"是{MASK_CHAR * 11}，痰是白色的"
    assert span_quote_matches(raw, 4, 22, quote)


def test_offset_alignment_with_mask() -> None:
    raw = "13812345678"
    quote = MASK_CHAR * len(raw)
    assert span_quote_matches(raw, 0, len(raw), quote)
    assert span_quote_matches(raw, 2, 8, MASK_CHAR * 6)


def test_wrong_length_never_matches() -> None:
    assert not span_quote_matches("有痰", 0, 2, "有痰" + MASK_CHAR)
    assert not span_quote_matches("有痰", 0, 3, "有痰")
    assert not span_quote_matches("有痰", 0, 0, "")


def test_fabricated_quote_never_matches() -> None:
    assert not span_quote_matches("有痰", 0, 2, "干咳")
    assert not span_quote_matches("有痰", 0, 2, "干咳" + MASK_CHAR)


def test_find_quote_span_locates_exact_and_masked() -> None:
    raw = "咳嗽一周，痰是白色的"
    assert find_quote_span(raw, "痰是白色的") == (5, 10)
    assert find_quote_span(raw, "痰是" + MASK_CHAR + "色的") == (5, 10)


def test_find_quote_span_returns_none_when_absent() -> None:
    assert find_quote_span("有痰", "黄痰") is None
    assert find_quote_span("有痰", "") is None
    assert find_quote_span("有痰", "有痰" + MASK_CHAR) is None


def test_mask_matches_only_same_length_window() -> None:
    raw = "138"
    assert find_quote_span(raw, MASK_CHAR * 3) == (0, 3)
    assert find_quote_span(raw, MASK_CHAR * 2) == (0, 2)
    assert find_quote_span(raw, MASK_CHAR * 4) is None  # wider than content
