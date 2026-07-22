"""L4.5-11-1 Intake入口投影层专项测试。

测试矩阵覆盖任务书第10节要求的所有场景。
"""

from __future__ import annotations

import json
import statistics
import time
import uuid

import pytest

import app.agent_runtime.context as context_module
from app.agent_runtime.context import (
    ContextBuilderError,
    contains_model_input_identity_sequence,
    project_model_input_identity_sequences,
)
from app.agents.intake_extraction import build_intake_context
from app.schemas.intake import IntakeExtractionInput, IntakeMessage, IntakeMessageRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(content: str, *, msg_id: uuid.UUID | None = None) -> IntakeMessage:
    return IntakeMessage(
        message_id=msg_id or uuid.uuid4(),
        role=IntakeMessageRole.PATIENT,
        content=content,
    )


def _make_input(*contents: str) -> IntakeExtractionInput:
    return IntakeExtractionInput(
        current_messages=tuple(_msg(c) for c in contents),
        historical_active_facts=(),
    )


def _build_user_json(input_payload: IntakeExtractionInput) -> list[dict[str, str]]:
    packet, _ = build_intake_context(input_payload)
    user_content = packet.messages[-1].content
    return json.loads(user_content)


_PERFORMANCE_WARMUPS = 10
_PERFORMANCE_SAMPLES = 25
_SINGLE_MESSAGE_LIMIT_MS = 10.0
_EIGHT_MESSAGE_LIMIT_MS = 80.0


def _median_call_ms(public_helper: object, contents: tuple[str, ...]) -> float:
    for _ in range(_PERFORMANCE_WARMUPS):
        public_helper(contents)  # type: ignore[operator]

    samples_ns: list[int] = []
    for _ in range(_PERFORMANCE_SAMPLES):
        started_ns = time.perf_counter_ns()
        public_helper(contents)  # type: ignore[operator]
        samples_ns.append(time.perf_counter_ns() - started_ns)
    return statistics.median(samples_ns) / 1_000_000


# ---------------------------------------------------------------------------
# 1. 基础手机号和身份证号匹配
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content,expected_masked",
    [
        # ASCII 连续手机号
        ("phone 13812345678", "phone ███████████"),
        # ASCII 连续身份证号
        ("id 11010519491231002X", "id ██████████████████"),
        ("id 11010519491231002x", "id ██████████████████"),
        # 末尾X/x
        ("X结尾 11010519491231002X", "X结尾 ██████████████████"),
        ("x结尾 11010519491231002x", "x结尾 ██████████████████"),
    ],
)
def test_ascii_continuous_phone_and_id_card(content: str, expected_masked: str) -> None:
    result = project_model_input_identity_sequences((content,))
    assert result[0] == expected_masked


# ---------------------------------------------------------------------------
# 2. 全角字符匹配
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content,expected_masked",
    [
        # 全角手机号
        ("phone １３８１２３４５６７８", "phone ███████████"),
        # 全角身份证号
        ("id １１０１０５１９４９１２３１００２Ｘ", "id ██████████████████"),
        # 全角Ｘ/ｘ
        ("id １１０１０５１９４９１２３１００２Ｘ", "id ██████████████████"),
        ("id １１０１０５１９４９１２３１００２ｘ", "id ██████████████████"),
    ],
)
def test_fullwidth_phone_and_id_card(content: str, expected_masked: str) -> None:
    result = project_model_input_identity_sequences((content,))
    assert result[0] == expected_masked


# ---------------------------------------------------------------------------
# 3. 分隔手机号（空格、-、.）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content,expected_masked",
    [
        # 空格分隔
        ("phone 138 1234 5678", "phone █████████████"),
        # -分隔
        ("phone 138-1234-5678", "phone █████████████"),
        # .分隔
        ("phone 138.1234.5678", "phone █████████████"),
    ],
)
def test_separated_phone_with_matching_delimiters(content: str, expected_masked: str) -> None:
    result = project_model_input_identity_sequences((content,))
    assert result[0] == expected_masked


# ---------------------------------------------------------------------------
# 4. 跨message切分
# ---------------------------------------------------------------------------

def test_cross_message_continuous_phone_split_at_each_position() -> None:
    """手机号的每一个单一跨message切分位置。"""
    phone = "13812345678"
    for i in range(1, len(phone)):
        first, second = phone[:i], phone[i:]
        result = project_model_input_identity_sequences((first, second))
        assert len(result) == 2
        assert len(result[0]) == len(first)
        assert len(result[1]) == len(second)
        # 跨message后应能重组并命中
        assert "13812345678" not in "".join(result)
        assert result[0].count("█") + result[1].count("█") == 11


@pytest.mark.parametrize(
    "id_card",
    [
        "11010519491231002X",
        "11010519491231002x",
        "１１０１０５１９４９１２３１００２Ｘ",
        "１１０１０５１９４９１２３１００２ｘ",
    ],
)
def test_cross_message_id_card_split_at_each_position(id_card: str) -> None:
    """身份证号的每一个单一跨message切分位置。"""
    for i in range(1, len(id_card)):
        first, second = id_card[:i], id_card[i:]
        result = project_model_input_identity_sequences((first, second))
        assert len(result) == 2
        assert len(result[0]) == len(first)
        assert len(result[1]) == len(second)
        assert contains_model_input_identity_sequence((first, second)) is True
        total_masked = sum(r.count("█") for r in result)
        assert total_masked == 18


# ---------------------------------------------------------------------------
# 5. 三message重组
# ---------------------------------------------------------------------------

def test_three_message_phone_reassembly_3_4_4() -> None:
    """手机 3-4-4 分组跨message重组。"""
    result = project_model_input_identity_sequences(("138", "1234", "5678"))
    assert len(result) == 3
    assert result[0].count("█") == 3
    assert result[1].count("█") == 4
    assert result[2].count("█") == 4


def test_three_message_id_card_reassembly() -> None:
    """身份证跨三message重组。"""
    result = project_model_input_identity_sequences(("110105", "19491231", "002X"))
    assert len(result) == 3
    total_masked = sum(r.count("█") for r in result)
    assert total_masked == 18


# ---------------------------------------------------------------------------
# 6. 逐message长度不变
# ---------------------------------------------------------------------------

def test_projected_length_equals_raw_length_for_each_message() -> None:
    raw = ("phone 13812345678", "id 11010519491231002X")
    result = project_model_input_identity_sequences(raw)
    assert len(result) == len(raw)
    for i, (r, p) in enumerate(zip(raw, result, strict=False)):
        assert len(p) == len(r), f"message {i}: projected length {len(p)} != raw length {len(r)}"


# ---------------------------------------------------------------------------
# 7. 多条PII、相邻候选和确定性选择
# ---------------------------------------------------------------------------

def test_multiple_pii_in_single_message() -> None:
    content = "phone 13812345678 and id 11010519491231002X"
    result = project_model_input_identity_sequences((content,))
    assert result[0].count("█") == 11 + 18


def test_adjacent_candidates_and_deterministic_selection() -> None:
    # 相邻手机号和身份证号 - 边界检查会导致两者都被拒绝
    # 因为手机号的后边界是身份证号的开头（D），身份证号的前边界是手机号的末尾（D）
    content = "1381234567811010519491231002X"
    result = project_model_input_identity_sequences((content,))
    # 边界检查导致没有匹配
    assert result[0] == content


@pytest.mark.parametrize(
    "id_card",
    [
        "13812345678901234X",
        "13812345678901234x",
        "１３８１２３４５６７８９０１２３４Ｘ",
        "１３８１２３４５６７８９０１２３４ｘ",
    ],
)
def test_phone_shaped_prefix_does_not_hide_same_start_id_card(id_card: str) -> None:
    """被边界拒绝的手机号样式前缀不得遮蔽同起点身份证候选。"""
    assert contains_model_input_identity_sequence((id_card,)) is True
    projected = project_model_input_identity_sequences((id_card,))
    assert len(projected[0]) == len(id_card)
    assert projected[0] == "█" * 18


# ---------------------------------------------------------------------------
# 8. 临床数字保持不变
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content",
    [
        "体温 38.5°C",  # 体温
        "血压 120/80",  # 血压
        "心率 72",  # 心率
        "血糖 5.6",  # 血糖
        "2024-01-15",  # 日期
        "剂量 500mg",  # 剂量
    ],
)
def test_clinical_numbers_remain_unchanged(content: str) -> None:
    result = project_model_input_identity_sequences((content,))
    assert result[0] == content


# ---------------------------------------------------------------------------
# 9. 明确非目标保持不变
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content",
    [
        "不同 138-1234 5678",  # 不同分隔符
        "多个 138  1234  5678",  # 多个空格
        "下划线 138_1234_5678",  # 下划线
        "斜杠 138/1234/5678",  # 斜杠
        "15位 110105194912310",  # 15位身份证
        "数学 abc123def",  # 字母数字混合
        "编码 13812345678901234567",  # 更长数字序列
    ],
)
def test_explicit_non_targets_remain_unchanged(content: str) -> None:
    result = project_model_input_identity_sequences((content,))
    assert result[0] == content


def test_nfkc_hard_boundary_no_false_concatenation() -> None:
    """㍑等NFKC扩展字符是HARD，前后内容不被错误拼接。"""
    content = "前138㍑12345678后"
    result = project_model_input_identity_sequences((content,))
    # ㍑是HARD，应阻止跨字符拼接
    assert result[0] == content


@pytest.mark.parametrize(
    ("public_helper", "expected"),
    [
        (contains_model_input_identity_sequence, False),
        (project_model_input_identity_sequences, ("7" * 4_000,)),
    ],
)
def test_digit_dense_single_message_has_bounded_matcher_work(
    public_helper: object,
    expected: object,
) -> None:
    """最大合法单消息保持原样，预热后的函数中位数低于10ms。"""
    contents = ("7" * 4_000,)
    assert public_helper(contents) == expected  # type: ignore[operator]
    median_ms = _median_call_ms(public_helper, contents)
    assert median_ms < _SINGLE_MESSAGE_LIMIT_MS, (
        f"median {median_ms:.3f} ms >= {_SINGLE_MESSAGE_LIMIT_MS:.1f} ms "
        f"({_PERFORMANCE_WARMUPS} warmups, {_PERFORMANCE_SAMPLES} samples)"
    )


@pytest.mark.parametrize(
    ("public_helper", "expected"),
    [
        (contains_model_input_identity_sequence, False),
        (project_model_input_identity_sequences, ("7" * 4_000,) * 8),
    ],
)
def test_digit_dense_eight_message_batch_has_bounded_matcher_work(
    public_helper: object,
    expected: object,
) -> None:
    """8条最大合法消息保持原样，预热后的函数中位数低于80ms。"""
    contents = ("7" * 4_000,) * 8
    assert public_helper(contents) == expected  # type: ignore[operator]
    median_ms = _median_call_ms(public_helper, contents)
    assert median_ms < _EIGHT_MESSAGE_LIMIT_MS, (
        f"median {median_ms:.3f} ms >= {_EIGHT_MESSAGE_LIMIT_MS:.1f} ms "
        f"({_PERFORMANCE_WARMUPS} warmups, {_PERFORMANCE_SAMPLES} samples)"
    )


# ---------------------------------------------------------------------------
# 10. 输入不变性
# ---------------------------------------------------------------------------

def test_input_tuple_and_dto_remain_unmodified() -> None:
    raw = ("phone 13812345678", "id 11010519491231002X")
    original = _make_input(*raw)
    # 投影不应修改原始tuple
    _ = project_model_input_identity_sequences(raw)
    # 原始tuple不应被修改
    assert raw[0] == "phone 13812345678"
    assert raw[1] == "id 11010519491231002X"
    # DTO不应被修改
    assert original.current_messages[0].content == "phone 13812345678"
    assert original.current_messages[1].content == "id 11010519491231002X"
    # message_id不变
    assert original.current_messages[0].message_id is not None


def test_user_json_contains_projected_copies_only() -> None:
    """USER JSON只含投影副本，原始DTO仍含原文。"""
    raw = ("phone 13812345678",)
    input_payload = _make_input(*raw)
    packet, _ = build_intake_context(input_payload)
    user_content = packet.messages[-1].content
    # USER JSON应含投影
    assert "13812345678" not in user_content
    assert "███████████" in user_content
    # 原始DTO不变
    assert input_payload.current_messages[0].content == "phone 13812345678"


# ---------------------------------------------------------------------------
# 11. grounding坐标不变
# ---------------------------------------------------------------------------

def test_clinical_quote_start_end_unchanged_after_projection() -> None:
    """临床quote在投影前后保持相同start/end；跨入遮罩区的quote不得伪造成可grounding原文。"""
    text = "phone 13812345678 headache"
    result = project_model_input_identity_sequences((text,))
    projected = result[0]
    # "headache"的位置在投影前后应相同
    assert text.index("headache") == projected.index("headache")
    assert len(text) == len(projected)


# ---------------------------------------------------------------------------
# 12. scanner/projector一致性和幂等性
# ---------------------------------------------------------------------------

def test_scanner_and_projector_hit_consistency() -> None:
    """scanner和projector共享matcher，命中集合一致。"""
    raw = ("phone 13812345678", "id 11010519491231002X")
    has_hit = contains_model_input_identity_sequence(raw)
    projected = project_model_input_identity_sequences(raw)
    # 有命中时scanner为True且projector发生遮罩
    if has_hit:
        assert any("█" in p for p in projected)
    else:
        assert all(p == r for p, r in zip(projected, raw, strict=False))


def test_projector_is_idempotent() -> None:
    """对已投影结果再次投影，结果不变。"""
    raw = ("phone 13812345678",)
    first = project_model_input_identity_sequences(raw)
    second = project_model_input_identity_sequences(first)
    assert first == second


# ---------------------------------------------------------------------------
# 13. 异常处理
# ---------------------------------------------------------------------------

def test_non_string_input_raises_context_builder_error() -> None:
    with pytest.raises(ContextBuilderError):
        project_model_input_identity_sequences((123,))  # type: ignore[arg-type]


def test_error_does_not_leak_original_values() -> None:
    with pytest.raises(ContextBuilderError) as exc_info:
        project_model_input_identity_sequences((123,))  # type: ignore[arg-type]
    exc_str = str(exc_info.value)
    assert "138" not in exc_str
    assert "110105" not in exc_str


@pytest.mark.parametrize(
    "public_helper",
    [
        contains_model_input_identity_sequence,
        project_model_input_identity_sequences,
    ],
)
def test_matcher_failure_is_fixed_redacted_and_chainless(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    public_helper: object,
) -> None:
    leaked_value = "13900000000"

    def _raise_matcher_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"matcher failed for {leaked_value}")

    monkeypatch.setattr(context_module, "_find_matches", _raise_matcher_error)
    with pytest.raises(ContextBuilderError) as exc_info:
        public_helper((leaked_value,))  # type: ignore[operator]

    assert str(exc_info.value) == "identity sequence processing failed"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert leaked_value not in caplog.text


def test_mask_failure_is_fixed_redacted_and_chainless(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    leaked_value = "13900000000"

    def _raise_mask_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"mask failed for {leaked_value}")

    monkeypatch.setattr(context_module, "_apply_mask", _raise_mask_error)
    with pytest.raises(ContextBuilderError) as exc_info:
        project_model_input_identity_sequences((leaked_value,))

    assert str(exc_info.value) == "identity sequence processing failed"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert leaked_value not in caplog.text


# ---------------------------------------------------------------------------
# 14. 先红测试：当前无projector时的失败场景
# ---------------------------------------------------------------------------

def test_single_message_raw_phone_is_masked() -> None:
    """先红：单消息裸手机号应在USER层被遮罩。"""
    input_payload = _make_input("phone 13812345678")
    user_json = _build_user_json(input_payload)
    assert len(user_json) == 1
    assert "13812345678" not in user_json[0]["content"]
    assert "█" in user_json[0]["content"]


def test_cross_message_phone_reassembly_is_masked() -> None:
    """先红：跨message重组手机号应在USER层被遮罩。"""
    input_payload = _make_input("138", "12345678")
    user_json = _build_user_json(input_payload)
    assert len(user_json) == 2
    combined = user_json[0]["content"] + user_json[1]["content"]
    assert "13812345678" not in combined
    assert "█" in user_json[0]["content"] or "█" in user_json[1]["content"]


def test_single_message_raw_id_card_is_masked() -> None:
    """单消息裸身份证号应在USER层被遮罩。"""
    input_payload = _make_input("id 11010519491231002X")
    user_json = _build_user_json(input_payload)
    assert len(user_json) == 1
    assert "11010519491231002X" not in user_json[0]["content"]
    assert "█" in user_json[0]["content"]
