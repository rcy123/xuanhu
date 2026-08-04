from app.schemas.completeness import InquiryDimension
from app.services.sufficiency_report import missing_item_payloads


def test_missing_item_payloads_returns_clinician_facing_copy() -> None:
    items = missing_item_payloads(
        (
            InquiryDimension.FOUR_DIAGNOSIS,
            InquiryDimension.ALLERGY_STATUS,
        )
    )

    assert items == [
        {
            "key": "four_diagnosis",
            "label": "四诊信息",
            "reason": "望、闻、问、切相关信息尚未完整。",
            "suggested_question": "请补充舌象、面色、声音或脉象等四诊信息。",
        },
        {
            "key": "safety.allergy_status",
            "label": "过敏史",
            "reason": "药物或食物过敏史尚未确认。",
            "suggested_question": "是否有药物、食物或其他过敏史？",
        },
    ]


def test_missing_item_payloads_hides_unknown_technical_key_from_copy() -> None:
    item = missing_item_payloads(("future.technical_key",))[0]

    assert item["key"] == "future.technical_key"
    assert item["label"] == "待补充信息"
    assert item["reason"] == "该项问诊信息尚未完整。"
