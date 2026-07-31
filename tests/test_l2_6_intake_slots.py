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
    """2b: 代码从已验证 observations 派生粗槽位(决策 12 改容器不改判定)。"""
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
