"""2a: 粗槽位 schema 与阈值判定单测(决策 12/25)。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent_runtime.intake_dimension_mapping import (
    dimension_slot_satisfied,
    slot_threshold_for,
)
from app.schemas.completeness import InquiryDimension
from app.schemas.intake import (
    SLOT_PROJECTION_SCHEMA_VERSION,
    DimensionSlotSnapshot,
    DimensionSlotValue,
    IntakeExtractionOutput,
    SlotCompleteness,
)


def test_slot_thresholds_derive_from_maturity_authority() -> None:
    # 决策 12 粗槽位: 单一真源 MATURITY_KEY_THRESHOLDS(寒热≥2/二便≥2/现病变化≥1),无阈值默认 1。
    assert slot_threshold_for(InquiryDimension.TEN_COLD_HEAT) == 2
    assert slot_threshold_for(InquiryDimension.TEN_STOOL_URINE) == 2
    assert slot_threshold_for(InquiryDimension.PRESENT_ILLNESS_CHANGE) == 1
    assert slot_threshold_for(InquiryDimension.TEN_SLEEP) == 1


def test_dimension_slot_satisfied_llm_signal_semantics() -> None:
    # 决策 25: LLM 主导 + 代码兜底。
    # partial → LLM 主导,槽位数达标也继续追问。
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 2, llm_signal="partial") is False
    # complete → 代码复核阈值(LLM 不能突破下限)。
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 2, llm_signal="complete") is True
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 1, llm_signal="complete") is False
    # 无信号 → 代码兜底阈值判定。
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 2) is True
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 1) is False
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 1, llm_signal="unknown") is False


def test_dimension_slot_snapshot_schema_contract() -> None:
    snapshot = DimensionSlotSnapshot(
        dimension="ten_questions.cold_heat",
        slots=(
            DimensionSlotValue(slot_name="aversion_cold", value="怕冷不明显"),
            DimensionSlotValue(slot_name="fever", value="不发烧"),
        ),
        completeness=SlotCompleteness.COMPLETE,
    )
    assert snapshot.dimension == "ten_questions.cold_heat"
    assert len(snapshot.slots) == 2
    assert snapshot.completeness is SlotCompleteness.COMPLETE

    # 模型不能自创维度/槽位名(程序定义枚举约束)。
    with pytest.raises(ValidationError):
        DimensionSlotSnapshot(dimension="symptoms.cold_heat", slots=())
    with pytest.raises(ValidationError):
        DimensionSlotValue(slot_name="chills!!", value="怕冷")
    with pytest.raises(ValidationError):
        DimensionSlotSnapshot(dimension="ten_questions.cold_heat", completeness="weird")

    # R2-A 权威投影字段可显式解析(除解析默认外, 显式字段也走同一契约)。
    r2a = DimensionSlotSnapshot(
        dimension="ten_questions.cold_heat",
        slots=(DimensionSlotValue(slot_id="abc-123", slot_name="chills", value="怕冷"),),
        completeness=SlotCompleteness.COMPLETE,
        slot_count=1,
        candidate_count=1,
        sanitized_count=0,
        truncated=False,
        per_dimension_cap=8,
        global_cap=32,
    )
    assert r2a.projection_version == SLOT_PROJECTION_SCHEMA_VERSION
    assert r2a.slots[0].slot_id == "abc-123"
    assert r2a.slot_count == 1
    assert r2a.candidate_count == 1
    assert r2a.per_dimension_cap == 8
    assert r2a.global_cap == 32


def test_intake_output_dimension_slots_optional() -> None:
    # 灰度关闭(默认): dimension_slots 为空,维持裸 fact_key 路径。
    output = IntakeExtractionOutput(decision="extracted", observations=())
    assert output.dimension_slots == ()

    # 灰度开启: 模型可产出槽位对象。
    output = IntakeExtractionOutput(
        decision="extracted",
        observations=(),
        dimension_slots=(
            DimensionSlotSnapshot(
                dimension="ten_questions.cold_heat",
                slots=(DimensionSlotValue(slot_name="aversion_cold", value="怕冷"),),
                completeness=SlotCompleteness.PARTIAL,
                missing_slots=("fever",),
            ),
        ),
    )
    assert output.dimension_slots[0].missing_slots == ("fever",)


def test_derive_dimension_slots_from_verified_observations() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus
    from app.schemas.intake import SLOT_PROJECTION_SCHEMA_VERSION

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    def mk(key: str, value: str) -> ObservationSchema:
        return ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        )

    facts = (
        mk("present_illness.chills", "怕冷不明显"),
        mk("present_illness.fever", "不发烧"),
        mk("chief_complaint.symptom", "受凉咳嗽一周"),
        mk("ten_questions.stool_urine", "大便正常"),
    )
    dimensions = frozenset(
        {
            InquiryDimension.TEN_COLD_HEAT,
            InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
            InquiryDimension.TEN_STOOL_URINE,
            InquiryDimension.TEN_SLEEP,
        }
    )
    snapshots = derive_dimension_slots(facts, dimensions=dimensions)
    by_dim = {item["dimension"]: item for item in snapshots}

    # 寒热采到 2 项 → 阈值 2 → complete。
    cold_heat = by_dim["ten_questions.cold_heat"]
    assert len(cold_heat["slots"]) == 2
    assert cold_heat["completeness"] == "complete"
    assert cold_heat["missing_slots"] == []

    # R2-A 权威投影元数据恒填充(版本/计数/cap)。
    assert cold_heat["projection_version"] == SLOT_PROJECTION_SCHEMA_VERSION
    assert cold_heat["slot_count"] == 2
    assert cold_heat["candidate_count"] == 2
    assert cold_heat["sanitized_count"] == 0
    assert cold_heat["truncated"] is False
    assert cold_heat["per_dimension_cap"] == 8
    assert cold_heat["global_cap"] == 32
    # 稳定 slot_id: uuid5(canonical 维度 + fact_key)。
    assert cold_heat["slots"][0]["slot_id"]

    # 主诉采到 1 项 → 阈值 1 → complete。
    assert by_dim["chief_complaint.symptom"]["completeness"] == "complete"

    # 二便只采到大便 1 项 → 阈值 2 → partial + 缺口。
    stool_urine = by_dim["ten_questions.stool_urine"]
    assert stool_urine["completeness"] == "partial"
    assert stool_urine["missing_slots"]

    # 睡眠无事实 → 产出 partial 快照(缺口提示,completeness 用它驱动追问)。
    sleep = by_dim["ten_questions.sleep"]
    assert sleep["completeness"] == "partial"
    assert sleep["missing_slots"]


# ---------------------------------------------------------------------------
# R2-A 权威槽位投影(derive_dimension_slots): 确定性/稳定性/cap/保留/清洗契约
# ---------------------------------------------------------------------------


def test_derive_projects_current_chain_heads_not_active_only() -> None:
    """R2-B1: 派生基于当前语义链头——CORRECTED 后继是当前真值被包含,其被取代的
    ACTIVE 根被排除,RETRACTED 头被排除;字符串形态的 status("active"/"retracted")
    同样由投影安全处理。"""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    root_id = uuid4()
    root = ObservationSchema(
        observation_id=root_id,
        session_id=session_id,
        fact_key="present_illness.chills",
        value="怕冷",
        source_message_id=source,
        status=ObservationStatus.ACTIVE,
        created_at=now,
    )
    corrected = ObservationSchema(
        observation_id=uuid4(),
        session_id=session_id,
        fact_key="present_illness.chills",
        value="现在不冷了",
        source_message_id=source,
        status=ObservationStatus.CORRECTED,
        supersedes_observation_id=root_id,
        created_at=now + timedelta(seconds=1),
    )
    retracted = ObservationSchema(
        observation_id=uuid4(),
        session_id=session_id,
        fact_key="present_illness.fever",
        value=None,
        source_message_id=source,
        status=ObservationStatus.RETRACTED,
        supersedes_observation_id=uuid4(),
        created_at=now,
    )
    # 字符串形态的 status("active"/"retracted")也安全处理。
    string_facts = (
        SimpleNamespace(
            observation_id=uuid4(), fact_key="present_illness.symptom.fever", value="低烧",
            normalized_value=None, source_message_id=source, status="active",
            created_at=now, confidence=0.9,
        ),
        SimpleNamespace(
            observation_id=uuid4(), fact_key="present_illness.symptom.chills", value="撤",
            normalized_value=None, source_message_id=source, status="retracted",
            created_at=now, confidence=0.9,
        ),
    )
    snapshots = derive_dimension_slots(
        (*string_facts, retracted, corrected, root),
        dimensions=frozenset({InquiryDimension.TEN_COLD_HEAT}),
    )
    assert len(snapshots) == 1
    cold_heat = snapshots[0]
    # 被取代的 ACTIVE 根(root)不派生; CORRECTED 后继与字符串 active 头派生。
    assert [slot["slot_name"] for slot in cold_heat["slots"]] == [
        "present_illness.chills",
        "present_illness.symptom.fever",
    ]
    assert cold_heat["slots"][0]["value"] == "现在不冷了"
    assert cold_heat["candidate_count"] == 2


def test_derive_is_permutation_stable() -> None:
    """R2-A: 输出与输入顺序无关——维度按枚举值、槽位按 canonical fact_key/slot_id 排序。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    def mk(key: str, value: str) -> ObservationSchema:
        return ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        )

    base_facts = [
        mk("present_illness.chills", "怕冷"),
        mk("present_illness.fever", "不发烧"),
        mk("present_illness.symptom.chills", "夜里怕冷"),
        mk("chief_complaint.symptom", "咳嗽三天"),
        mk("present_illness.sweat", "无汗"),
    ]
    dims = frozenset(
        {
            InquiryDimension.TEN_COLD_HEAT,
            InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
            InquiryDimension.TEN_SWEAT,
        }
    )
    orders = (
        tuple(base_facts),
        tuple(reversed(base_facts)),
        tuple(base_facts[index] for index in (2, 0, 4, 1, 3)),
    )
    expected = derive_dimension_slots(orders[0], dimensions=dims)
    for facts in orders[1:]:
        assert derive_dimension_slots(facts, dimensions=dims) == expected


def test_slot_id_stable_across_correction_and_new_observation() -> None:
    """R2-A: 纠正同一 canonical 槽位的值(新 observation、同 fact_key)时 slot_id 不变。"""
    from datetime import UTC, datetime, timedelta
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)
    dim = InquiryDimension.TEN_COLD_HEAT
    fact_key = "present_illness.chills"
    expected_slot_id = str(uuid5(NAMESPACE_URL, f"xuanhu:slot:{dim.value}:{fact_key}"))

    root_id = uuid4()
    old_fact = ObservationSchema(
        observation_id=root_id,
        session_id=session_id,
        fact_key=fact_key,
        value="怕冷",
        source_message_id=source,
        status=ObservationStatus.ACTIVE,
        created_at=now,
    )
    first = derive_dimension_slots((old_fact,), dimensions=frozenset({dim}))
    assert first[0]["slots"][0]["slot_id"] == expected_slot_id
    assert first[0]["slots"][0]["value"] == "怕冷"

    # 纠正后: 被取代的 ACTIVE 根不派生(被 CORRECTED 后继取代), CORRECTED 后继是
    # 当前链头派生换值 → 同 canonical 槽位 slot_id 保持。
    corrected = derive_dimension_slots(
        (
            old_fact,
            ObservationSchema(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key=fact_key,
                value="现在不冷了",
                source_message_id=source,
                status=ObservationStatus.CORRECTED,
                supersedes_observation_id=root_id,
                created_at=now + timedelta(seconds=1),
            ),
        ),
        dimensions=frozenset({dim}),
    )
    assert len(corrected[0]["slots"]) == 1
    assert corrected[0]["slots"][0]["slot_id"] == expected_slot_id
    assert corrected[0]["slots"][0]["value"] == "现在不冷了"


def test_duplicate_canonical_slot_deterministic_winner() -> None:
    """R2-A: 同一 canonical 槽位重复候选——newest created_at 赢; 平局由稳定 observation_id 决胜。"""
    from datetime import UTC, datetime, timedelta
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)
    dim = InquiryDimension.TEN_COLD_HEAT

    def mk(value: str, created_at: datetime, obs_id: uuid4) -> ObservationSchema:
        return ObservationSchema(
            observation_id=obs_id,
            session_id=session_id,
            fact_key="present_illness.chills",
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=created_at,
        )

    old = mk("旧值", now, uuid4())
    newer = mk("新值", now + timedelta(hours=1), uuid4())
    newest = mk("最新值", now + timedelta(hours=2), uuid4())

    # 输入顺序任意, 赢家恒为 created_at 最新者。
    forward = derive_dimension_slots((old, newer, newest), dimensions=frozenset({dim}))
    backward = derive_dimension_slots((newest, newer, old), dimensions=frozenset({dim}))
    assert forward == backward
    assert forward[0]["slots"][0]["value"] == "最新值"
    assert forward[0]["candidate_count"] == 1
    assert forward[0]["slot_count"] == 1

    # created_at 平局 → 稳定 observation_id 决胜(max str), 与输入顺序无关。
    a = mk("A", now, uuid5(NAMESPACE_URL, "fact:a"))
    b = mk("B", now, uuid5(NAMESPACE_URL, "fact:b"))
    tie_winner = max(a.observation_id, b.observation_id, key=str)
    expected_value = "A" if tie_winner == a.observation_id else "B"
    assert derive_dimension_slots((a, b), dimensions=frozenset({dim}))[0]["slots"][0]["value"] == expected_value
    assert derive_dimension_slots((b, a), dimensions=frozenset({dim}))[0]["slots"][0]["value"] == expected_value


def test_per_dimension_cap_metadata_uses_canonical_first() -> None:
    """R2-A: 每维 cap 截断取 canonical-first 子集, 元数据恒填充; 完整性按完整候选数判定。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus
    from app.schemas.intake import SLOT_PROJECTION_SCHEMA_VERSION

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    def mk(key: str, value: str) -> ObservationSchema:
        return ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        )

    facts = (
        mk("present_illness.chills", "怕冷"),
        mk("present_illness.fever", "不发烧"),
        mk("present_illness.aversion_cold", "怕风"),
        mk("present_illness.symptom.fever", "低烧"),
    )
    dim = InquiryDimension.TEN_COLD_HEAT
    snapshot = derive_dimension_slots(
        facts, dimensions=frozenset({dim}), max_slots_per_dimension=2
    )[0]
    assert snapshot["slot_count"] == 2
    assert snapshot["candidate_count"] == 4
    assert snapshot["sanitized_count"] == 0
    assert snapshot["truncated"] is True
    assert snapshot["per_dimension_cap"] == 2
    assert snapshot["global_cap"] == 32
    assert snapshot["projection_version"] == SLOT_PROJECTION_SCHEMA_VERSION
    # completeness 用完整去重候选数(4 ≥ 阈值 2)判定——cap 不改变医学完整性。
    assert snapshot["completeness"] == "complete"
    assert [slot["slot_name"] for slot in snapshot["slots"]] == [
        "present_illness.aversion_cold",
        "present_illness.chills",
    ]


def test_global_cap_metadata_across_dimensions() -> None:
    """R2-A: 全局 cap 跨维度确定性截断, 每维仍在每维 cap 内; 完整性按完整候选数判定。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    def mk(key: str, value: str) -> ObservationSchema:
        return ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        )

    cold_heat = (
        mk("present_illness.chills", "怕冷"),
        mk("present_illness.fever", "不发烧"),
        mk("present_illness.aversion_cold", "怕风"),
    )
    sweat = (
        mk("present_illness.sweat", "无汗"),
        mk("present_illness.sweating", "盗汗"),
    )
    dims = frozenset({InquiryDimension.TEN_COLD_HEAT, InquiryDimension.TEN_SWEAT})
    snapshots = derive_dimension_slots((*cold_heat, *sweat), dimensions=dims, max_total_slots=3)
    by_dim = {item["dimension"]: item for item in snapshots}
    # 全局 cap=3 全给了枚举值靠前的寒热维度; 二便维度被截到 0。
    assert sum(item["slot_count"] for item in snapshots) == 3
    assert by_dim["ten_questions.cold_heat"]["slot_count"] == 3
    assert by_dim["ten_questions.sweat"]["slot_count"] == 0
    assert by_dim["ten_questions.cold_heat"]["truncated"] is False
    assert by_dim["ten_questions.sweat"]["truncated"] is True
    # cap 元数据在每个快照上。
    for item in snapshots:
        assert item["per_dimension_cap"] == 8
        assert item["global_cap"] == 3
    # 完整性仍按完整候选数判定: sweat 2 ≥ 阈值 1 → complete, cap 截断不影响。
    assert by_dim["ten_questions.sweat"]["candidate_count"] == 2
    assert by_dim["ten_questions.sweat"]["completeness"] == "complete"


def test_correction_target_preservation_and_invalid_preserved_ids_ignored() -> None:
    """R2-A: 有效保留 observation_id 可为去重赢家(纠正目标不被丢弃); 无效保留 ID 被忽略。"""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)
    dim = InquiryDimension.TEN_COLD_HEAT
    fact_key = "present_illness.chills"

    def mk(value: str, created_at: datetime, obs_id: uuid4, *, status: ObservationStatus = ObservationStatus.ACTIVE) -> ObservationSchema:
        return ObservationSchema(
            observation_id=obs_id,
            session_id=session_id,
            fact_key=fact_key,
            value=value,
            source_message_id=source,
            status=status,
            supersedes_observation_id=None if status is ObservationStatus.ACTIVE else uuid4(),
            created_at=created_at,
        )

    target_id = uuid4()
    older = mk("纠正目标旧值", now, target_id)
    newer = mk("新值", now + timedelta(hours=1), uuid4())

    # 请求保留纠正目标 → 即使有更新的重复候选, 仍为赢家。
    preserved = derive_dimension_slots(
        (newer, older),
        dimensions=frozenset({dim}),
        preserve_observation_ids=frozenset({target_id}),
    )
    assert preserved[0]["slots"][0]["value"] == "纠正目标旧值"

    # 不请求保留 → 默认 newest 赢。
    default = derive_dimension_slots((newer, older), dimensions=frozenset({dim}))
    assert default[0]["slots"][0]["value"] == "新值"

    # 保留 ID 指向不存在/不活跃事实 → 被忽略, 仍默认 newest 赢。
    ghost = derive_dimension_slots(
        (newer, older),
        dimensions=frozenset({dim}),
        preserve_observation_ids=frozenset({uuid4()}),
    )
    assert ghost[0]["slots"][0]["value"] == "新值"
    # 保留 ID 指向已被取代的 ACTIVE 根(非当前链头)→ 被忽略; 当前链头为
    # newer 与 CORRECTED 后继, newest created_at(now+3h)赢。
    corrected_target = uuid4()
    correction_successor = ObservationSchema(
        observation_id=uuid4(),
        session_id=session_id,
        fact_key=fact_key,
        value="纠正后",
        source_message_id=source,
        status=ObservationStatus.CORRECTED,
        supersedes_observation_id=corrected_target,
        created_at=now + timedelta(hours=3),
    )
    inactive = derive_dimension_slots(
        (newer, mk("已纠正", now, corrected_target), correction_successor),
        dimensions=frozenset({dim}),
        preserve_observation_ids=frozenset({corrected_target}),
    )
    assert inactive[0]["slots"][0]["value"] == "纠正后"

    # 保留候选单独超每维 cap → 取 canonical-first 确定性子集(不因保留而超 cap)。
    cap_facts = tuple(
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        )
        for key, value in (
            ("present_illness.chills", "怕冷"),
            ("present_illness.fever", "不发烧"),
            ("present_illness.aversion_cold", "怕风"),
        )
    )
    capped = derive_dimension_slots(
        cap_facts,
        dimensions=frozenset({dim}),
        preserve_observation_ids=frozenset({fact.observation_id for fact in cap_facts}),
        max_slots_per_dimension=2,
    )[0]
    assert capped["candidate_count"] == 3
    assert capped["slot_count"] == 2
    assert capped["truncated"] is True
    assert [slot["slot_name"] for slot in capped["slots"]] == [
        "present_illness.aversion_cold",
        "present_illness.chills",
    ]


def test_non_json_and_nan_values_rejected_and_output_json_safe() -> None:
    """R2-A: 非 JSON 值(NaN/set)严格拒绝计入 sanitized_count; 全量输出可 json.dumps。"""
    import json as json_module
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    def mk(key: str, value: object) -> ObservationSchema:
        return ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        )

    facts = (
        mk("present_illness.chills", "怕冷"),
        mk("present_illness.fever", float("nan")),  # NaN → 严格 json.dumps(allow_nan=False) 拒绝
        mk("present_illness.aversion_cold", {1, 2, 3}),  # set → 非 JSON 拒绝
    )
    snapshots = derive_dimension_slots(facts, dimensions=frozenset({InquiryDimension.TEN_COLD_HEAT}))
    snapshot = snapshots[0]
    assert snapshot["candidate_count"] == 1
    assert snapshot["sanitized_count"] == 2
    assert snapshot["slot_count"] == 1
    # 拒绝的非 JSON 候选计入 sanitized_count, 不进 candidate_count → 无 cap 截断。
    assert snapshot["truncated"] is False
    assert [slot["slot_name"] for slot in snapshot["slots"]] == ["present_illness.chills"]
    assert snapshot["slots"][0]["value"] == "怕冷"
    for item in snapshots:
        json_module.dumps(item, allow_nan=False)


def test_invalid_cap_values_rejected() -> None:
    """R2-A: bool/零/负/非 int/超硬上限的 cap 一律 ValueError。"""
    from app.agent_runtime.intake_dimension_mapping import (
        MAX_SLOTS_PER_DIMENSION_HARD_CAP,
        MAX_TOTAL_SLOTS_HARD_CAP,
        derive_dimension_slots,
    )
    from app.schemas.completeness import InquiryDimension

    dims = frozenset({InquiryDimension.TEN_COLD_HEAT})
    with pytest.raises(ValueError, match="max_slots_per_dimension"):
        derive_dimension_slots((), dimensions=dims, max_slots_per_dimension=True)  # bool 是 int 子类
    with pytest.raises(ValueError, match="max_slots_per_dimension"):
        derive_dimension_slots((), dimensions=dims, max_slots_per_dimension=0)
    with pytest.raises(ValueError, match="max_slots_per_dimension"):
        derive_dimension_slots((), dimensions=dims, max_slots_per_dimension=-1)
    with pytest.raises(ValueError, match="max_slots_per_dimension"):
        derive_dimension_slots((), dimensions=dims, max_slots_per_dimension=1.5)  # 非 int
    with pytest.raises(ValueError, match="max_slots_per_dimension"):
        derive_dimension_slots((), dimensions=dims, max_slots_per_dimension=MAX_SLOTS_PER_DIMENSION_HARD_CAP + 1)

    with pytest.raises(ValueError, match="max_total_slots"):
        derive_dimension_slots((), dimensions=dims, max_total_slots=False)
    with pytest.raises(ValueError, match="max_total_slots"):
        derive_dimension_slots((), dimensions=dims, max_total_slots=0)
    with pytest.raises(ValueError, match="max_total_slots"):
        derive_dimension_slots((), dimensions=dims, max_total_slots=-5)
    with pytest.raises(ValueError, match="max_total_slots"):
        derive_dimension_slots((), dimensions=dims, max_total_slots=MAX_TOTAL_SLOTS_HARD_CAP + 1)

    # 合法边界: 硬上限本身可用; 空事实产出 partial 缺口快照(与既有空维度语义一致)。
    snapshots = derive_dimension_slots(
        (),
        dimensions=dims,
        max_slots_per_dimension=MAX_SLOTS_PER_DIMENSION_HARD_CAP,
        max_total_slots=MAX_TOTAL_SLOTS_HARD_CAP,
    )
    assert len(snapshots) == 1
    assert snapshots[0]["slots"] == []
    assert snapshots[0]["completeness"] == "partial"
    assert snapshots[0]["per_dimension_cap"] == MAX_SLOTS_PER_DIMENSION_HARD_CAP
    assert snapshots[0]["global_cap"] == MAX_TOTAL_SLOTS_HARD_CAP


def test_malicious_model_dimension_slots_do_not_affect_derivation() -> None:
    """R2-A: 模型侧 dimension_slots 只是候选契约, 权威派生只认 observations 的 fact_key。"""
    from datetime import UTC, datetime
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    # 模型声称 sleep 已 complete 且携带伪造 slot_id。
    model_output = IntakeExtractionOutput(
        decision="extracted",
        observations=(),
        dimension_slots=(
            DimensionSlotSnapshot(
                dimension="ten_questions.sleep",
                slots=(
                    DimensionSlotValue(slot_id="fabricated", slot_name="fabricated", value="x"),
                ),
                completeness=SlotCompleteness.COMPLETE,
            ),
        ),
    )
    assert model_output.dimension_slots[0].slots[0].slot_id == "fabricated"

    facts = (
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="present_illness.chills",
            value="怕冷",
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
    )
    dims = frozenset({InquiryDimension.TEN_COLD_HEAT, InquiryDimension.TEN_SLEEP})
    snapshots = derive_dimension_slots(facts, dimensions=dims)
    by_dim = {item["dimension"]: item for item in snapshots}

    # 模型声称 complete 不影响权威判定——sleep 实际无事实 → partial。
    sleep = by_dim["ten_questions.sleep"]
    assert sleep["slots"] == []
    assert sleep["completeness"] == "partial"

    # 真实槽位 slot_id 是程序 uuid5, 与模型文本无关; 伪造 slot_id 绝不进入权威派生。
    cold_heat = by_dim["ten_questions.cold_heat"]
    assert cold_heat["slots"][0]["slot_id"] == str(
        uuid5(NAMESPACE_URL, "xuanhu:slot:ten_questions.cold_heat:present_illness.chills")
    )
    assert all(
        slot["slot_id"] != "fabricated"
        for snapshot in snapshots
        for slot in snapshot["slots"]
    )


def test_slot_based_covered_requires_slot_completeness() -> None:
    """2c: 灰度开启后 covered 认「粗槽位齐」——修复问题 6「一键即过」。

    - slot_based=False(默认/现状): 寒热任一键命中即 covered(一键即过)。
    - slot_based=True: 寒热需 keyset 内 2 项(怕冷+发热)才 covered;1 项不齐。
    - 无阈值维度(如睡眠,阈值 1)两个口径一致。
    """
    from uuid import uuid4

    from app.agent_runtime.completeness_policy import evaluate_completeness_policy
    from app.schemas.completeness import (
        CompletenessDomainSnapshot,
        CompletenessObservationFact,
        CompletenessPolicyInput,
        CompletenessProgress,
    )
    from app.schemas.domain import GateDecision
    from app.schemas.triage import TriageGateDetails, TriageGateResult

    session_id = uuid4()

    def run(slot_based: bool, *, with_fever: bool) -> tuple[bool, bool]:
        def fact(key: str) -> CompletenessObservationFact:
            return CompletenessObservationFact(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key=key,
                value_fingerprint=key,
                normalized_code=None,
                status="active",
            )

        facts = [
            fact("chief_complaint.symptom"),
            fact("present_illness.chills"),
        ]
        if with_fever:
            facts.append(fact("present_illness.fever"))
        triage_gate = TriageGateResult(
            gate_name="triage",
            policy_version="triage-red-flag.v1",
            input_state_version=1,
            decision=GateDecision.PASSED,
            details=TriageGateDetails(
                disposition="continue",
                candidate_count=0,
                category_counts=(),
                rule_ids=(),
                rules=(),
                source_message_ids=(),
                risk_level="none",
            ),
        )
        domain_snapshot = CompletenessDomainSnapshot(
            session_id=session_id,
            state_version=1,
            observations=tuple(facts),
            safety_profile=None,
        )
        result = evaluate_completeness_policy(
            CompletenessPolicyInput(
                input_state_version=1,
                domain_snapshot=domain_snapshot,
                triage_gate=triage_gate,
                progress=CompletenessProgress(),
                slot_based=slot_based,
            )
        )
        return (
            InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions,
            InquiryDimension.TEN_SLEEP in result.covered_dimensions,
        )

    # 现状口径(灰度关闭): 单条 chills 即 covered(一键即过)。
    cold_heat, _ = run(slot_based=False, with_fever=False)
    assert cold_heat is True
    # 槽位口径(灰度开启): 单条 chills 不齐(阈值 2)→ 不 covered。
    cold_heat, _ = run(slot_based=True, with_fever=False)
    assert cold_heat is False
    # 槽位口径: chills + fever 两项齐 → covered。
    cold_heat, _ = run(slot_based=True, with_fever=True)
    assert cold_heat is True
    # 无阈值维度(睡眠阈值 1)两个口径一致: 无事实均不 covered。
    _, sleep = run(slot_based=True, with_fever=False)
    assert sleep is False

    # 整维 canonical 键(ten_questions.cold_heat)命中 → 视为齐(模型合键输出,
    # 如"怕冷有一点点,不发烧,也没有出汗"合成一条),避免粗槽位数键漏判。
    facts = [
        CompletenessObservationFact(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="ten_questions.cold_heat",
            value_fingerprint="ten_questions.cold_heat",
            normalized_code=None,
            status="active",
        )
    ]
    result = evaluate_completeness_policy(
        CompletenessPolicyInput(
            input_state_version=1,
            domain_snapshot=CompletenessDomainSnapshot(
                session_id=session_id,
                state_version=1,
                observations=tuple(facts),
                safety_profile=None,
            ),
            triage_gate=TriageGateResult(
                gate_name="triage",
                policy_version="triage-red-flag.v1",
                input_state_version=1,
                decision=GateDecision.PASSED,
                details=TriageGateDetails(
                    disposition="continue",
                    candidate_count=0,
                    category_counts=(),
                    rule_ids=(),
                    rules=(),
                    source_message_ids=(),
                    risk_level="none",
                ),
            ),
            progress=CompletenessProgress(),
            slot_based=True,
        )
    )
    assert InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions


def test_stagnation_cap_partial_vs_manual_handoff() -> None:
    """2d(决策 11): cap 到分流——缺非安全维度 → PARTIAL 推进;缺安全项 → STAGNATED 转人工。"""
    from uuid import uuid4

    from app.agent_runtime.completeness_policy import evaluate_completeness_policy
    from app.schemas.completeness import (
        CompletenessDomainSnapshot,
        CompletenessObservationFact,
        CompletenessPolicyInput,
        CompletenessProgress,
        CompletenessSafetyProfile,
    )
    from app.schemas.domain import GateDecision
    from app.schemas.triage import TriageGateDetails, TriageGateResult

    session_id = uuid4()

    def run(*keys: str, no_new_facts_rounds: int, safety_complete: bool = True) -> str:
        def fact(key: str) -> CompletenessObservationFact:
            return CompletenessObservationFact(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key=key,
                value_fingerprint=key,
                normalized_code="male" if key == "patient.sex" else None,
                status="active",
            )

        safety_profile = (
            CompletenessSafetyProfile(
                session_id=session_id,
                allergy_collection_status="explicitly_none",
                medications_collection_status="explicitly_none",
                major_conditions_collection_status="explicitly_none",
            )
            if safety_complete
            else None
        )

        triage_gate = TriageGateResult(
            gate_name="triage",
            policy_version="triage-red-flag.v1",
            input_state_version=1,
            decision=GateDecision.PASSED,
            details=TriageGateDetails(
                disposition="continue",
                candidate_count=0,
                category_counts=(),
                rule_ids=(),
                rules=(),
                source_message_ids=(),
                risk_level="none",
            ),
        )
        result = evaluate_completeness_policy(
            CompletenessPolicyInput(
                input_state_version=1,
                domain_snapshot=CompletenessDomainSnapshot(
                    session_id=session_id,
                    state_version=1,
                    observations=tuple(fact(key) for key in keys),
                    safety_profile=safety_profile,
                ),
                triage_gate=triage_gate,
                progress=CompletenessProgress(no_new_facts_rounds=no_new_facts_rounds),
            )
        )
        return result.disposition.value

    # cap 到(no_new_facts_rounds≥2)且缺非安全维度(寒热未采)→ PARTIAL 推进。
    # (male 性别使妊娠/哺乳不适用,安全三项已齐,missing 只剩非安全维度)
    assert run("chief_complaint.symptom", "patient.sex", no_new_facts_rounds=2) == "partial"
    # cap 到且安全项(过敏)缺失 → STAGNATED 转人工(铁律 9)。
    assert (
        run("chief_complaint.symptom", "present_illness.chills", "present_illness.fever",
            "patient.sex", no_new_facts_rounds=2, safety_complete=False)
        == "stagnated"
    )
    # 未到 cap → 正常 INCOMPLETE 继续追问。
    assert run("chief_complaint.symptom", "patient.sex", no_new_facts_rounds=0) == "incomplete"


def test_derive_slot_context_rows_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """3a: 下游投影槽位对象(问题 22)——灰度开启时辨证/开方输入为规整维度行。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_slot_context_rows
    from app.schemas.domain import ObservationSchema, ObservationStatus
    from app.schemas.intake import SLOT_PROJECTION_SCHEMA_VERSION

    session_id = uuid4()
    now = datetime.now(UTC)
    facts = (
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="present_illness.chills",
            value="怕冷不明显",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="present_illness.fever",
            value="不发烧",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="ten_questions.cold_heat",
            value="怕冷,不发烧",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="chief_complaint.symptom",
            value="咳嗽三天",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        # 非 active 不投影
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="ten_questions.sleep",
            value="差",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.RETRACTED,
            supersedes_observation_id=uuid4(),
            created_at=now,
        ),
    )
    rows = derive_slot_context_rows(
        facts,
        dimensions=frozenset(
            {
                InquiryDimension.TEN_COLD_HEAT,
                InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
                InquiryDimension.TEN_SLEEP,
            }
        ),
        state_version=3,
        session_id=session_id,
    )
    by_key = {row["fact_key"]: row for row in rows}
    # 寒热维度一行: fact_key=维度枚举(无漂移键),value=槽位快照(JSON-safe)。
    cold_heat = by_key["ten_questions.cold_heat"]
    assert cold_heat["value"]["dimension"] == "ten_questions.cold_heat"
    assert len(cold_heat["value"]["slots"]) >= 1
    assert cold_heat["state_version"] == 3
    # R2-A: value 携带投影版本/计数/cap 元数据(可观测, 不改变行形状)。
    assert cold_heat["value"]["projection_version"] == SLOT_PROJECTION_SCHEMA_VERSION
    assert cold_heat["value"]["per_dimension_cap"] == 8
    assert cold_heat["value"]["global_cap"] == 32
    assert cold_heat["value"]["slot_count"] == cold_heat["value"]["candidate_count"]
    # 主诉行存在。
    assert "chief_complaint.symptom" in by_key
    # 非 active 观察不产行。
    assert "ten_questions.sleep" not in by_key
    # 行可 JSON 序列化(下游 prompt 投影用)。
    import json as _json

    for row in rows:
        _json.dumps(row, ensure_ascii=False)


def test_syndrome_context_slot_projection_when_gray_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """3a review should-fix: 灰度开启时 syndrome_draft 的 context_observations 投影槽位对象。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.reducer import DomainState
    from app.agents.syndrome_draft import _context_from_domain_state
    from app.core.config import get_settings
    from app.schemas.domain import ObservationSchema, ObservationStatus

    monkeypatch.setenv("XUANHU_INTAKE_SLOT_PATH_ENABLED", "true")
    get_settings.cache_clear()

    session_id = uuid4()
    now = datetime.now(UTC)
    state = DomainState(
        session_id=session_id,
        state_version=5,
        observations=(
            ObservationSchema(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key="present_illness.chills",
                value="怕冷不明显",
                normalized_value=None,
                source_message_id=uuid4(),
                status=ObservationStatus.ACTIVE,
                created_at=now,
            ),
            ObservationSchema(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key="present_illness.fever",
                value="不发烧",
                normalized_value=None,
                source_message_id=uuid4(),
                status=ObservationStatus.ACTIVE,
                created_at=now,
            ),
        ),
    )
    try:
        rows = _context_from_domain_state(state)
        assert len(rows) >= 1
        cold_heat_rows = [row for row in rows if row.fact_key == "ten_questions.cold_heat"]
        assert cold_heat_rows, "槽位投影应产出寒热维度行"
        value = cold_heat_rows[0].value
        assert value["dimension"] == "ten_questions.cold_heat"
        assert len(value["slots"]) == 2
    finally:
        monkeypatch.delenv("XUANHU_INTAKE_SLOT_PATH_ENABLED")
        get_settings.cache_clear()
