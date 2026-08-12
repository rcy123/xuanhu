"""同维度兼容值合并（2.8 增强）单元测试。

背景：REAL-SESSION d5df99f0 —— 医生对同一枚举维度逐步细化回答（冷热维度
先答"胃部非常怕冷"，再答"平时手脚也容易发凉"，再答"总体倾向是怕冷"），
旧逻辑按字面值差异一律丢弃并累计 no_new_facts，最终触发问诊停滞。

本测试覆盖：
- _semantic_value_relation 三分类（兼容 / 矛盾 / 无法判定）
- _merge_observation_texts 文本合并
- _drop_value_conflicting_adds 对兼容 ADD 转为 CORRECT、矛盾/未知仍丢弃
- completeness_policy._stagnation 在必需维度全覆盖时不停滞
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.agent_runtime.completeness_policy import (
    COMPLETENESS_POLICY_CONFIG,
    CompletenessProgress,
    _stagnation,
)
from app.agent_runtime.reducer import DomainState
from app.schemas.domain import ObservationSchema
from app.schemas.intake import ObservationDelta, ObservationOperation
from app.services.langgraph_intake import (
    _drop_value_conflicting_adds,
    _merge_observation_texts,
    _semantic_value_relation,
)

_SID = uuid.uuid4()
_MSG = uuid.uuid4()


def _observation(
    fact_key: str, value: str, *, status: str = "active", supersedes: uuid.UUID | None = None
) -> ObservationSchema:
    return ObservationSchema(
        observation_id=uuid.uuid4(),
        session_id=_SID,
        fact_key=fact_key,
        value=value,
        source_message_id=_MSG,
        status=status,  # type: ignore[arg-type]
        confidence=0.9,
        supersedes_observation_id=supersedes,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _delta(fact_key: str, value: str) -> ObservationDelta:
    return ObservationDelta(
        fact_key=fact_key,
        value=value,
        source_message_id=_MSG,
        confidence=0.9,
        operation=ObservationOperation.ADD,
        target_observation_id=None,
    )


def _state(*observations: ObservationSchema) -> DomainState:
    return DomainState(session_id=_SID, state_version=1, observations=tuple(observations))


# ---------------------------------------------------------------------------
# _semantic_value_relation
# ---------------------------------------------------------------------------


def test_relation_cold_heat_same_direction_compatible() -> None:
    # 同向细化：怕冷 → 手脚也凉 → 总体怕冷，全部兼容
    assert (
        _semantic_value_relation(
            "ten_questions.cold_heat", "平时手脚也容易发凉，整体体质偏怕冷", "胃部非常怕冷，不敢吃凉的"
        )
        == "compatible"
    )
    assert (
        _semantic_value_relation("ten_questions.cold_heat", "总体倾向是怕冷", "胃部非常怕冷，不敢吃凉的")
        == "compatible"
    )


def test_relation_cold_heat_opposite_direction_conflict() -> None:
    assert (
        _semantic_value_relation("ten_questions.cold_heat", "平时怕热，喜欢吹风扇", "胃部非常怕冷，不敢吃凉的")
        == "conflict"
    )


def test_relation_unknown_dimension_falls_back() -> None:
    # 未覆盖维度（如面色）→ unknown 保守
    assert _semantic_value_relation("ten_questions.head_body", "面色偏黄", "面色萎黄") == "unknown"
    # 非字符串值 → unknown
    assert _semantic_value_relation("ten_questions.cold_heat", {"raw": "怕冷"}, "胃部非常怕冷") == "unknown"


def test_relation_sleep_and_thirst() -> None:
    assert _semantic_value_relation("ten_questions.sleep", "入睡困难，多梦", "睡不好，容易醒") == "compatible"
    assert _semantic_value_relation("ten_questions.thirst", "不想喝水，不怎么喝", "口不渴") == "compatible"
    # 否定保护："不怎么想喝水"不应命中"想喝水"触发词
    assert _semantic_value_relation("ten_questions.thirst", "不怎么想喝水", "口不渴") == "unknown"


# ---------------------------------------------------------------------------
# _merge_observation_texts
# ---------------------------------------------------------------------------


def test_merge_appends_with_separator() -> None:
    assert _merge_observation_texts("胃部非常怕冷，不敢吃凉的", "平时手脚也容易发凉，整体体质偏怕冷") == (
        "胃部非常怕冷，不敢吃凉的；平时手脚也容易发凉，整体体质偏怕冷"
    )


def test_merge_dedupes_contained_text() -> None:
    # 新文本包含旧文本 → 取更完整者
    assert _merge_observation_texts("怕冷", "总体倾向是怕冷") == "总体倾向是怕冷"
    # 旧文本包含新文本 → 保留旧值
    assert (
        _merge_observation_texts("平时手脚也容易发凉，整体体质偏怕冷", "怕冷") == "平时手脚也容易发凉，整体体质偏怕冷"
    )


def test_merge_non_string_returns_none() -> None:
    assert _merge_observation_texts({"raw": "怕冷"}, "怕冷") is None
    assert _merge_observation_texts("怕冷", {"raw": "怕冷"}) is None


# ---------------------------------------------------------------------------
# _drop_value_conflicting_adds
# ---------------------------------------------------------------------------


def test_drop_converts_compatible_add_to_correct() -> None:
    active = _observation("ten_questions.cold_heat", "胃部非常怕冷，不敢吃凉的")
    state = _state(active)
    delta = _delta("ten_questions.cold_heat", "平时手脚也容易发凉，整体体质偏怕冷")
    kept = _drop_value_conflicting_adds((delta,), state=state, rejected_observations=None)
    assert len(kept) == 1
    converted = kept[0]
    assert converted.operation is ObservationOperation.CORRECT
    assert converted.target_observation_id == active.observation_id
    assert converted.value == "胃部非常怕冷，不敢吃凉的；平时手脚也容易发凉，整体体质偏怕冷"


def test_drop_conflicting_add_still_rejected() -> None:
    active = _observation("ten_questions.cold_heat", "胃部非常怕冷，不敢吃凉的")
    state = _state(active)
    rejected: list[object] = []
    delta = _delta("ten_questions.cold_heat", "平时怕热，喜欢吹风扇")
    kept = _drop_value_conflicting_adds((delta,), state=state, rejected_observations=rejected)  # type: ignore[arg-type]
    assert kept == ()
    assert len(rejected) == 1
    assert rejected[0].reason == "value_conflicts_active_fact"


def test_drop_unknown_dimension_conservatively_rejected() -> None:
    active = _observation("ten_questions.head_body", "面色萎黄")
    state = _state(active)
    rejected: list[object] = []
    delta = _delta("ten_questions.head_body", "面色偏黄")
    kept = _drop_value_conflicting_adds((delta,), state=state, rejected_observations=rejected)  # type: ignore[arg-type]
    assert kept == ()
    assert len(rejected) == 1
    assert rejected[0].reason == "value_incompatible_unknown"


def test_drop_same_literal_is_noop_kept() -> None:
    active = _observation("ten_questions.cold_heat", "怕冷")
    state = _state(active)
    delta = _delta("ten_questions.cold_heat", "怕冷")
    kept = _drop_value_conflicting_adds((delta,), state=state, rejected_observations=None)
    assert len(kept) == 1
    assert kept[0].operation is ObservationOperation.ADD


# ---------------------------------------------------------------------------
# _stagnation（missing_required 为空时不停滞）
# ---------------------------------------------------------------------------


def test_stagnation_cleared_when_no_missing_required() -> None:

    progress = CompletenessProgress(
        no_new_facts_rounds=COMPLETENESS_POLICY_CONFIG.no_new_facts_round_threshold,
        followup_rounds=4,
    )
    # 必需维度全部覆盖：确认性回答不构成空转 → 不停滞
    result = _stagnation(progress, missing_required=())
    assert result.stagnated is False


def test_stagnation_kept_when_missing_required() -> None:
    from app.schemas.completeness import InquiryDimension

    progress = CompletenessProgress(
        no_new_facts_rounds=COMPLETENESS_POLICY_CONFIG.no_new_facts_round_threshold,
        followup_rounds=4,
    )
    result = _stagnation(progress, missing_required=(InquiryDimension.TEN_SLEEP,))
    assert result.stagnated is True
