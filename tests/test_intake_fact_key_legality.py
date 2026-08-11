"""E1: 抽取产出 fact_key 合法性闸门单元测试。

trigger session d449735a 的死循环根因是抽取模型把医生答的「夜晚怕冷，微微发热」漂移成
越界畸键 ``symptom.cold_heat``——既不在 canonical 维度 ``fact_keys`` 内，也不在 D1 键桥
``DIMENSION_KEYSETS`` 派生键集内 → 寒热维度永远 missing → gap_selector 锁死 → 命中写死模板。

本套件固化 E1 闸门（``app/agent_runtime/intake_fact_key_legality.py``）的 deterministic 行为：
越界 ADD 键被 reject 留痕、canonical/symptom.*/派生键透传、CORRECT 指向历史畸键降级为伪
RETRACT 清脏。
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.agent_runtime.intake_fact_key_legality import (
    LEGAL_FACT_KEYS,
    filter_legal_observations,
    normalized_observations_to_payload,
    rejected_observations_to_payload,
)
from app.schemas.intake import ObservationDelta, ObservationOperation


def _obs(
    source: UUID,
    key: str,
    *,
    value: object = "v",
    operation: ObservationOperation = ObservationOperation.ADD,
    target: UUID | None = None,
) -> ObservationDelta:
    return ObservationDelta(
        fact_key=key,
        value=value,
        normalized_value=value,
        source_message_id=source,
        confidence=0.9,
        operation=operation,
        target_observation_id=target,
    )


# ---------------------------------------------------------------------------
# 合法集聚合（与 D1 / canonical 真源同步，不允许脱钩）
# ---------------------------------------------------------------------------


def test_legal_set_covers_canonical_chief_complaint_and_change_keys() -> None:
    """canonical 键必须在合法集内——chnage/course/symptom 都不误拒。"""

    assert "present_illness.change" in LEGAL_FACT_KEYS
    assert "chief_complaint.symptom" in LEGAL_FACT_KEYS
    assert "chief_complaint.course" in LEGAL_FACT_KEYS


def test_legal_set_covers_d1_derivation_keys_and_subprefix() -> None:
    """D1 派生键与子前缀键必须在合法集内——派生覆盖的前提是这些键能落库。"""

    assert "present_illness.chills" in LEGAL_FACT_KEYS
    assert "present_illness.fever" in LEGAL_FACT_KEYS
    assert "present_illness.symptom.cough" in LEGAL_FACT_KEYS
    assert "ten_questions.stool_urine.stool" in LEGAL_FACT_KEYS


def test_legal_set_rejects_known_drifted_outliers() -> None:
    """实测畸键（symptom.cold_heat / 裸 symptom / 裸 fever）不在合法集内——E1 会拦它们。"""

    assert "symptom.cold_heat" not in LEGAL_FACT_KEYS
    assert "symptom" not in LEGAL_FACT_KEYS
    assert "fever" not in LEGAL_FACT_KEYS


def test_safety_dimension_names_present_as_legal_placeholder() -> None:
    """五个安全维度名占位进入合法集——避免误拒 extraction 极少产出的边界情形。"""

    assert "safety.allergy_status" in LEGAL_FACT_KEYS
    assert "safety.pregnancy_status" in LEGAL_FACT_KEYS
    assert "safety.medication_status" in LEGAL_FACT_KEYS


# ---------------------------------------------------------------------------
# ADD 越界键 reject + 留痕
# ---------------------------------------------------------------------------


def test_add_outlier_fact_key_rejected_with_trace() -> None:
    """d449735a 复现：抽取产出畸键 ``symptom.cold_heat``（ADD）→ 被 reject 留痕。"""

    source = uuid4()
    items = (
        _obs(source, "symptom.cold_heat", value="night_chills"),
        _obs(source, "present_illness.chills", value="chills"),
    )
    result = filter_legal_observations(items)

    assert [o.fact_key for o in result.kept] == ["present_illness.chills"]
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.fact_key == "symptom.cold_heat"
    assert rejected.operation == "add"
    assert rejected.reason == "fact_key_outside_jurisdiction"
    assert rejected.target_observation_id is None
    assert result.downgraded == ()


def test_bare_keys_rejected() -> None:
    """282a985a 复现：裸键 ``symptom`` / ``fever`` 缺前缀归属 → reject。"""

    source = uuid4()
    items = (
        _obs(source, "symptom", value="cough"),
        _obs(source, "fever", value="low"),
    )
    result = filter_legal_observations(items)

    assert [o.fact_key for o in result.kept] == []
    assert {o.fact_key for o in result.rejected} == {"symptom", "fever"}
    assert all(o.reason == "fact_key_outside_jurisdiction" for o in result.rejected)


def test_canonical_and_derived_keys_pass_through() -> None:
    """canonical + D1 派生 + 子前缀键原样透传——合法 delta 不被误删。"""

    source = uuid4()
    items = (
        _obs(source, "present_illness.change"),
        _obs(source, "present_illness.cough"),
        _obs(source, "present_illness.symptom.fever"),
        _obs(source, "ten_questions.stool_urine.stool"),
    )
    result = filter_legal_observations(items)

    assert [o.fact_key for o in result.kept] == [
        "present_illness.change",
        "present_illness.cough",
        "present_illness.symptom.fever",
        "ten_questions.stool_urine.stool",
    ]
    assert result.rejected == ()
    assert result.downgraded == ()


# ---------------------------------------------------------------------------
# CORRECT / RETRACT 越界键降级为伪 RETRACT 清历史畸键
# ---------------------------------------------------------------------------


def test_correct_outlier_targeting_active_dirty_key_downgrades_to_pseudo_retract() -> None:
    """历史畸键已落库为 active 时，CORRECT 越界键降级为伪 RETRACT 清掉它——d449735a 第 3 轮
    若 extraction 产出 correction 就能自治愈。"""

    source = uuid4()
    target = uuid5(NAMESPACE_URL, "historical-dirty:symptom.cold_heat")
    items = (
        _obs(
            source,
            "symptom.cold_heat",
            value="corrected",
            operation=ObservationOperation.CORRECT,
            target=target,
        ),
    )
    active_index: dict[str, frozenset[str]] = {
        "symptom.cold_heat": frozenset({str(target)}),
    }
    result = filter_legal_observations(items, active_observation_ids_by_fact_key=active_index)

    assert result.kept == ()
    assert result.rejected == ()
    assert len(result.downgraded) == 1
    pseudo = result.downgraded[0]
    assert pseudo.fact_key == "symptom.cold_heat"
    assert pseudo.operation is ObservationOperation.RETRACT
    assert pseudo.target_observation_id == target
    assert pseudo.value is None
    assert pseudo.normalized_value is None


def test_correct_outlier_without_active_target_rejected() -> None:
    """CORRECT 越界键的 target 不在 active 索引里（无脏键可清）→ reject 留痕，不强行降级。"""

    source = uuid4()
    target = uuid4()
    items = (
        _obs(
            source,
            "symptom.cold_heat",
            value="corrected",
            operation=ObservationOperation.CORRECT,
            target=target,
        ),
    )
    # active 索引为空
    result = filter_legal_observations(items, active_observation_ids_by_fact_key={})

    assert result.kept == ()
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.fact_key == "symptom.cold_heat"
    assert rejected.operation == "correct"
    assert rejected.reason == "correct_target_missing"
    assert result.downgraded == ()


def test_retract_outlier_targeting_active_dirty_key_downgrades_to_pseudo_retract() -> None:
    """RETRACT 越界键同样适用降级路径——与 CORRECT 对称。retraction 本就不带 value。"""

    source = uuid4()
    target = uuid5(NAMESPACE_URL, "historical-dirty:symptom.cold_heat")
    items = (
        _obs(
            source,
            "symptom.cold_heat",
            value=None,
            operation=ObservationOperation.RETRACT,
            target=target,
        ),
    )
    active_index = {"symptom.cold_heat": frozenset({str(target)})}
    result = filter_legal_observations(items, active_observation_ids_by_fact_key=active_index)

    assert len(result.downgraded) == 1
    assert result.downgraded[0].operation is ObservationOperation.RETRACT
    assert result.rejected == ()


def test_correct_canonical_key_passes_through_unchanged() -> None:
    """canonical 键的 CORRECT 不被 E1 触动——E1 只拦越界键，canonical correction 走原 reducer。"""

    source = uuid4()
    target = uuid5(NAMESPACE_URL, "historical:present_illness.change")
    items = (
        _obs(
            source,
            "present_illness.change",
            value="new_trend",
            operation=ObservationOperation.CORRECT,
            target=target,
        ),
    )
    active_index = {"present_illness.change": frozenset({str(target)})}
    result = filter_legal_observations(items, active_observation_ids_by_fact_key=active_index)

    # canonical 键不经降级分支，直接透传 kept（不因 active 索引有它就改 operation）。
    assert len(result.kept) == 1
    assert result.kept[0].operation is ObservationOperation.CORRECT
    assert result.downgraded == ()
    assert result.rejected == ()


# ---------------------------------------------------------------------------
# ADD 越界畸键归一为 canonical 键透传（b7bdf5ab 死循环根治）
# ---------------------------------------------------------------------------


def test_add_drifted_key_normalized_to_canonical_key_passthrough() -> None:
    """b7bdf5ab 复现：医生答「怕冷，微微发热」抽成 ``symptom.chills``/``symptom.fever`` 裸前缀
    漂畸键 → 不再 reject 丢失，而是归一为 ``present_illness.chills``/``present_illness.fever``
    透传落库（D1 派生覆盖即认寒热维度 covered，死循环解开）。"""

    source = uuid4()
    items = (
        _obs(source, "symptom.chills", value="怕冷"),
        _obs(source, "symptom.fever", value="微微发热"),
    )
    result = filter_legal_observations(items)

    # 归一后键改写透传 kept，非 reject 丢弃。
    assert [o.fact_key for o in result.kept] == [
        "present_illness.chills",
        "present_illness.fever",
    ]
    # value/evidence 原样保留，归一只改键名。
    assert result.kept[0].value == "怕冷"
    assert result.kept[0].operation is ObservationOperation.ADD
    assert result.rejected == ()
    assert result.downgraded == ()
    # 留痕记录原始漂移键 → 归一后 canonical 键。
    assert len(result.normalized) == 2
    n0, n1 = result.normalized
    assert n0.fact_key == "symptom.chills"
    assert n0.normalized_fact_key == "present_illness.chills"
    assert n0.operation == "add"
    assert n1.fact_key == "symptom.fever"
    assert n1.normalized_fact_key == "present_illness.fever"


def test_add_drifted_key_preserves_value_and_confidence() -> None:
    """归一只改键名：value / normalized_value / confidence / source_message_id 原样保留，
    确保下游 reducer/safety 拿到的证据链不被键名改写割断。"""

    source = uuid4()
    base = _obs(source, "symptom.cough", value="干咳")
    base_confidence = base.confidence

    result = filter_legal_observations((base,))

    kept = result.kept[0]
    assert kept.fact_key == "present_illness.cough"
    assert kept.value == "干咳"
    assert kept.confidence == base_confidence
    assert kept.source_message_id == source
    assert kept.operation is ObservationOperation.ADD


def test_add_unknown_drifted_key_still_rejected() -> None:
    """未在归一表登记的越界 ADD 键仍 reject（归一只解「已知高频漂移」，不预判新畸键）。
    ``symptom.cold_heat`` 这类语义越界漂移键（不是裸前缀寒热，而是把整维度捏成一个键）仍走 reject
    路径——它没有可归一的 canonical 单键语义。"""

    source = uuid4()
    items = (_obs(source, "symptom.cold_heat", value="night_chills"),)
    result = filter_legal_observations(items)

    assert result.kept == ()
    assert result.normalized == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].fact_key == "symptom.cold_heat"
    assert result.rejected[0].reason == "fact_key_outside_jurisdiction"


def test_correct_drifted_key_not_normalized_target_binding_preserved() -> None:
    """CORRECT 越界键不被归一——target 绑定指向历史事实，改键名会破坏 target。CORRECT 仍走
    reject / 降级伪 retract 路径。归一只对 ADD 生效。"""

    source = uuid4()
    target = uuid5(NAMESPACE_URL, "historical-dirty:symptom.chills")
    items = (
        _obs(
            source,
            "symptom.chills",
            value="corrected",
            operation=ObservationOperation.CORRECT,
            target=target,
        ),
    )
    active_index = {"symptom.chills": frozenset({str(target)})}
    result = filter_legal_observations(items, active_observation_ids_by_fact_key=active_index)

    # CORRECT 越界键命中 active 畸键 → 降级伪 RETRACT（保留原畸键名与 target），不归一。
    assert result.normalized == ()
    assert result.kept == ()
    assert len(result.downgraded) == 1
    pseudo = result.downgraded[0]
    assert pseudo.fact_key == "symptom.chills"  # 键名未被归一改写
    assert pseudo.operation is ObservationOperation.RETRACT
    assert pseudo.target_observation_id == target


def test_normalized_observations_to_payload_is_json_safe() -> None:
    """归一留痕序列化为 JSON-safe list，含 fact_key / normalized_fact_key / operation 三字段，
    写 claim intermediate_payload（归一路径可观测，与 reject 留痕同管道）。"""

    source = uuid4()
    items = (
        _obs(source, "symptom.chills", value="怕冷"),
        _obs(source, "symptom.fever", value="微微发热"),
    )
    result = filter_legal_observations(items)
    payload = normalized_observations_to_payload(result.normalized)

    assert isinstance(payload, list)
    assert len(payload) == 2
    entry = payload[0]
    assert set(entry) == {"fact_key", "normalized_fact_key", "operation"}
    assert entry["fact_key"] == "symptom.chills"
    assert entry["normalized_fact_key"] == "present_illness.chills"
    assert entry["operation"] == "add"


def test_normalized_empty_input_yields_empty_payload() -> None:
    """无归一时 payload 为空 list（写 claim 不留垃圾）。"""

    source = uuid4()
    items = (_obs(source, "present_illness.chills"),)
    result = filter_legal_observations(items)
    assert normalized_observations_to_payload(result.normalized) == []


def test_normalized_trace_is_frozen_dataclass() -> None:
    """NormalizedObservation 是 frozen slots dataclass——留痕不可被下游意外篡改（审计完整）。"""

    source = uuid4()
    items = (_obs(source, "symptom.chills", value="怕冷"),)
    result = filter_legal_observations(items)
    normalized = result.normalized[0]

    import dataclasses

    assert dataclasses.is_dataclass(normalized)
    try:
        normalized.fact_key = "tampered"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - 会被上面的 except 捕获
        raise AssertionError("NormalizedObservation must be frozen")
    assert normalized.fact_key == "symptom.chills"


# ---------------------------------------------------------------------------
# 留痕序列化（写 claim intermediate_payload 的契约）
# ---------------------------------------------------------------------------


def test_rejected_observations_to_payload_is_json_safe_and_filtered() -> None:
    """留痕序列化为 JSON-safe list，只含排障字段，value 截断 80 字符（PII 再脱敏一层）。"""

    source = uuid4()
    big_value = "x" * 200
    items = (_obs(source, "symptom.cold_heat", value=big_value),)
    result = filter_legal_observations(items)
    payload = rejected_observations_to_payload(result.rejected)

    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert set(entry) == {
        "fact_key",
        "operation",
        "reason",
        "target_observation_id",
        "value_preview",
    }
    assert entry["fact_key"] == "symptom.cold_heat"
    assert entry["operation"] == "add"
    assert entry["reason"] == "fact_key_outside_jurisdiction"
    assert entry["target_observation_id"] is None
    assert len(entry["value_preview"]) == 80  # 截断到 80


def test_rejected_empty_input_yields_empty_payload() -> None:
    """无 reject 时 payload 为空 list（写 claim 不留垃圾）。"""

    source = uuid4()
    items = (_obs(source, "present_illness.change"),)
    result = filter_legal_observations(items)
    assert rejected_observations_to_payload(result.rejected) == []


def test_rejection_trace_is_frozen_dataclass() -> None:
    """RejectedObservation 是 frozen slots dataclass——留痕不可被下游意外篡改（审计完整）。"""

    source = uuid4()
    items = (_obs(source, "symptom.cold_heat"),)
    result = filter_legal_observations(items)
    rejected = result.rejected[0]

    import dataclasses

    assert dataclasses.is_dataclass(rejected)
    try:
        rejected.fact_key = "tampered"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - 会被上面的 except 捕获
        raise AssertionError("RejectedObservation must be frozen")
    assert rejected.fact_key == "symptom.cold_heat"
