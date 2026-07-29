"""P2-2 样例知识库导入测试。

覆盖：
- doc_text 生成（herbs/formulas/acupoints/theory/cases 均有非空输出）
- 校验失败（必填字段、枚举、max_dose、pregnancy_contraindication）
- 未知药味/未知单位进入 warning/gap，不静默吞掉
- dosage_units 两=30g/钱=3g
- composition 校验通过 herb/unit lookup
- 医案脱敏检查
- 幂等性（同一数据两次导入不重复）
- 现有 P1/P2-1 测试继续通过（导入不破坏其他测试）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.import_knowledge import (
    LIANG_GRAMS,
    QIAN_GRAMS,
    VALID_CONVERSION_TYPE,
    VALID_ENTRY_TYPE,
    VALID_PREGNANCY,
    build_acupoint_doc_text,
    build_formula_doc_text,
    build_herb_doc_text,
    build_theory_case_doc_text,
    load_json,
    validate_acupoint,
    validate_dosage_unit,
    validate_formula,
    validate_herb,
    validate_theory_case,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ===================================================================
# 辅助 fixtures
# ===================================================================


def _sample_herb() -> dict:
    return {
        "name": "党参",
        "aliases": ["潞党参"],
        "properties": "甘，平",
        "meridians": ["脾", "肺"],
        "effects": "补中益气",
        "indications": "脾肺气虚",
        "dosage": "9-30g",
        "max_dose": 30,
        "contraindications": ["实证慎用"],
        "eighteen_incompatibilities": ["藜芦"],
        "nineteen_fears": [],
        "pregnancy_contraindication": "none",
        "incompatibilities": [],
    }


def _sample_formula() -> dict:
    return {
        "name": "参苓白术散",
        "aliases": ["参苓白术丸"],
        "composition": [
            {"herb": "党参", "dose": 12, "unit": "g"},
            {"herb": "白术", "dose": 9, "unit": "g"},
        ],
        "effect": "益气健脾",
        "indications": "脾虚湿困",
        "usage": "水煎服",
        "source": "模拟教材",
        "modification_rules": [],
    }


def _sample_acupoint() -> dict:
    return {
        "name": "足三里",
        "aliases": ["下陵"],
        "meridian": "足阳明胃经",
        "location": "小腿外侧",
        "indications": "胃痛，腹胀",
        "operation": "直刺1-2寸",
        "contraindications": [],
        "source": "模拟针灸学",
    }


def _sample_theory() -> dict:
    return {
        "entry_type": "theory",
        "title": "脾虚湿困证辨治要点",
        "disease_category": "脾胃病",
        "syndrome": "脾虚湿困",
        "treatment_principle": "健脾益气",
        "formula_summary": "参考参苓白术散",
        "content": "脾主运化...此为模拟内容。",
        "source": "模拟理论条目",
        "metadata": {"tags": ["脾虚"], "license": "synthetic"},
    }


def _sample_case_ok() -> dict:
    return {
        "entry_type": "case",
        "title": "模拟医案：脾虚便溏",
        "disease_category": "泄泻",
        "syndrome": "脾虚湿困",
        "treatment_principle": "健脾化湿",
        "formula_summary": "参考参苓白术散",
        "content": "患者A，成年人，主诉便溏。此为完全模拟医案，不含真实患者信息。",
        "source": "模拟脱敏医案集 v0.1",
        "metadata": {"deidentified": True, "tags": ["便溏"], "license": "synthetic"},
    }


# ===================================================================
# doc_text 生成测试
# ===================================================================


class TestBuildHerbDocText:
    """doc_text 生成 — herbs。"""

    def test_generates_non_empty(self):
        item = _sample_herb()
        text = build_herb_doc_text(item)
        assert text is not None
        assert len(text.strip()) > 0
        assert "党参" in text
        assert "潞党参" in text
        assert "甘，平" in text
        assert "补中益气" in text

    def test_includes_all_key_fields(self):
        item = _sample_herb()
        text = build_herb_doc_text(item)
        assert "别名：" in text
        assert "性味：" in text
        assert "归经：" in text
        assert "功效：" in text
        assert "主治：" in text
        assert "用量：" in text

    def test_handles_empty_optional_fields(self):
        item = {"name": "测试药", "aliases": [], "properties": "", "meridians": []}
        text = build_herb_doc_text(item)
        assert "测试药" in text
        assert len(text.strip()) > 0

    def test_handles_minimal_item(self):
        item = {"name": "最简"}
        text = build_herb_doc_text(item)
        assert "最简" in text
        assert text.endswith("。")

    def test_ends_with_period(self):
        text = build_herb_doc_text(_sample_herb())
        assert text.strip().endswith("。")


class TestBuildFormulaDocText:
    """doc_text 生成 — formulas。"""

    def test_generates_non_empty(self):
        item = _sample_formula()
        text = build_formula_doc_text(item)
        assert text is not None
        assert len(text.strip()) > 0
        assert "参苓白术散" in text

    def test_includes_composition(self):
        item = _sample_formula()
        text = build_formula_doc_text(item)
        assert "组成：" in text
        assert "党参" in text
        assert "白术" in text

    def test_includes_effect_indications(self):
        item = _sample_formula()
        text = build_formula_doc_text(item)
        assert "功效：" in text
        assert "主治：" in text
        assert "益气健脾" in text

    def test_composition_with_note(self):
        item = _sample_formula()
        item["composition"] = [
            {"herb": "砂仁", "dose": 3, "unit": "g", "note": "后下"},
        ]
        text = build_formula_doc_text(item)
        assert "后下" in text
        assert "砂仁3g（后下）" in text

    def test_composition_without_dose(self):
        item = _sample_formula()
        item["composition"] = [
            {"herb": "甘草", "unit": "g"},
        ]
        text = build_formula_doc_text(item)
        assert "甘草" in text

    def test_handles_empty_aliases(self):
        item = _sample_formula()
        item["aliases"] = []
        text = build_formula_doc_text(item)
        assert "别名：" not in text


class TestBuildAcupointDocText:
    """doc_text 生成 — acupoints。"""

    def test_generates_non_empty(self):
        item = _sample_acupoint()
        text = build_acupoint_doc_text(item)
        assert text is not None
        assert len(text.strip()) > 0
        assert "足三里" in text

    def test_includes_meridian_location(self):
        item = _sample_acupoint()
        text = build_acupoint_doc_text(item)
        assert "足阳明胃经" in text
        assert "小腿外侧" in text

    def test_includes_operation(self):
        item = _sample_acupoint()
        text = build_acupoint_doc_text(item)
        assert "操作：" in text
        assert "直刺" in text

    def test_handles_minimal_item(self):
        item = {"name": "测试穴"}
        text = build_acupoint_doc_text(item)
        assert "测试穴" in text
        assert text.endswith("。")


class TestBuildTheoryCaseDocText:
    """doc_text 生成 — theory/cases。"""

    def test_theory_generates_non_empty(self):
        item = _sample_theory()
        text = build_theory_case_doc_text(item)
        assert text is not None
        assert len(text.strip()) > 0
        assert "脾虚湿困证辨治要点" in text

    def test_case_generates_non_empty(self):
        item = _sample_case_ok()
        text = build_theory_case_doc_text(item)
        assert text is not None
        assert len(text.strip()) > 0
        assert "模拟医案" in text

    def test_theory_includes_type_label(self):
        item = _sample_theory()
        text = build_theory_case_doc_text(item)
        assert "类型：" in text
        assert "理论" in text

    def test_case_includes_type_label(self):
        item = _sample_case_ok()
        text = build_theory_case_doc_text(item)
        assert "类型：" in text
        assert "医案" in text

    def test_includes_syndrome_treatment(self):
        item = _sample_theory()
        text = build_theory_case_doc_text(item)
        assert "证型：" in text
        assert "治法：" in text
        assert "方药：" in text

    def test_handles_missing_optional_fields(self):
        item = {"entry_type": "theory", "title": "测试", "content": "内容"}
        text = build_theory_case_doc_text(item)
        assert "测试" in text
        assert "内容" in text


# ===================================================================
# 校验测试 — dosage_units
# ===================================================================


class TestValidateDosageUnit:
    """剂量单位校验。"""

    def test_valid_standard_unit(self):
        item = {"unit_name": "g", "to_grams": 1.0, "conversion_type": "standard", "is_standard": True}
        issues = validate_dosage_unit(item, 0)
        assert len(issues) == 0

    def test_valid_fixed_unit(self):
        item = {"unit_name": "两", "to_grams": 30.0, "conversion_type": "fixed", "is_standard": False}
        issues = validate_dosage_unit(item, 0)
        assert len(issues) == 0

    def test_missing_unit_name(self):
        item = {"unit_name": "", "conversion_type": "standard"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) >= 1
        assert any("unit_name" in b["message"].lower() or b["field"] == "unit_name" for b in blockers)

    def test_invalid_conversion_type(self):
        item = {"unit_name": "test", "conversion_type": "invalid_type"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("conversion_type" in b.get("field", "") for b in blockers)

    def test_standard_without_to_grams(self):
        item = {"unit_name": "test", "conversion_type": "standard", "to_grams": None}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("to_grams" in b.get("field", "") for b in blockers)

    def test_fixed_without_to_grams(self):
        item = {"unit_name": "test", "conversion_type": "fixed", "to_grams": None}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("to_grams" in b.get("field", "") for b in blockers)

    def test_liang_must_be_30g(self):
        """两必须为 30g（P0 锚定值）。"""
        item = {"unit_name": "两", "to_grams": 50.0, "conversion_type": "fixed"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        liang_issues = [b for b in blockers if "两" in b.get("message", "")]
        assert len(liang_issues) >= 1
        assert any("30" in b["message"] for b in liang_issues)

    def test_qian_must_be_3g(self):
        """钱必须为 3g（P0 锚定值）。"""
        item = {"unit_name": "钱", "to_grams": 5.0, "conversion_type": "fixed"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        qian_issues = [b for b in blockers if "钱" in b.get("message", "")]
        assert len(qian_issues) >= 1
        assert any("3" in b["message"] for b in qian_issues)

    def test_liang_30g_passes(self):
        """两=30g 应通过校验。"""
        item = {"unit_name": "两", "to_grams": 30.0, "conversion_type": "fixed"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_qian_3g_passes(self):
        """钱=3g 应通过校验。"""
        item = {"unit_name": "钱", "to_grams": 3.0, "conversion_type": "fixed"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_herb_specific_allows_null_to_grams(self):
        """herb_specific 允许 to_grams 为空。"""
        item = {"unit_name": "枚", "to_grams": None, "conversion_type": "herb_specific"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_unsupported_allows_null_to_grams(self):
        """unsupported 允许 to_grams 为空。"""
        item = {"unit_name": "适量", "to_grams": None, "conversion_type": "unsupported"}
        issues = validate_dosage_unit(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0


# ===================================================================
# 校验测试 — herbs
# ===================================================================


class TestValidateHerb:
    """中药校验。"""

    def test_valid_herb_passes(self):
        item = _sample_herb()
        issues = validate_herb(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_missing_name(self):
        item = {"name": ""}
        issues = validate_herb(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("名称" in b["message"] for b in blockers)

    def test_invalid_pregnancy_contraindication(self):
        item = _sample_herb()
        item["pregnancy_contraindication"] = "invalid_value"
        issues = validate_herb(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("pregnancy_contraindication" in b.get("field", "") for b in blockers)

    def test_valid_pregnancy_values(self):
        for val in VALID_PREGNANCY:
            item = _sample_herb()
            item["pregnancy_contraindication"] = val
            issues = validate_herb(item, 0)
            blockers = [x for x in issues if x["level"] == "blocker"]
            assert len(blockers) == 0, f"'{val}' should be valid"

    def test_max_dose_null_produces_warning(self):
        """max_dose 为空时应产生 warning（数据缺口），不产生 blocker。"""
        item = _sample_herb()
        item["max_dose"] = None
        issues = validate_herb(item, 0)
        warnings = [x for x in issues if x["level"] == "warning"]
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(warnings) >= 1
        assert any("max_dose" in w.get("field", "") for w in warnings)
        assert len(blockers) == 0

    def test_max_dose_negative_produces_blocker(self):
        item = _sample_herb()
        item["max_dose"] = -5
        issues = validate_herb(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("max_dose" in b.get("field", "") for b in blockers)

    def test_max_dose_zero_produces_blocker(self):
        item = _sample_herb()
        item["max_dose"] = 0
        issues = validate_herb(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("max_dose" in b.get("field", "") for b in blockers)


# ===================================================================
# 校验测试 — formulas
# ===================================================================


class TestValidateFormula:
    """方剂校验 — 需要 herb_lookup + unit_lookup。"""

    @pytest.fixture
    def herb_lookup(self) -> dict:
        return {"党参": "herb_id_1", "白术": "herb_id_2", "陈皮": "herb_id_3"}

    @pytest.fixture
    def unit_lookup(self) -> dict:
        return {"g": "unit_id_1", "枚": "unit_id_2", "钱": "unit_id_3"}

    def test_valid_formula_passes(self, herb_lookup, unit_lookup):
        item = _sample_formula()
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_missing_name(self, herb_lookup, unit_lookup):
        item = {"name": "", "composition": []}
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("名称" in b["message"] for b in blockers)

    def test_empty_composition(self, herb_lookup, unit_lookup):
        item = {"name": "测试方", "composition": []}
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("composition" in b.get("field", "") for b in blockers)

    def test_unknown_herb_produces_warning(self, herb_lookup, unit_lookup):
        """未知药味应产生 warning，不静默吞掉。"""
        item = _sample_formula()
        item["composition"] = [
            {"herb": "未知药", "dose": 10, "unit": "g"},
        ]
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        warnings = [x for x in issues if x["level"] == "warning"]
        assert len(warnings) >= 1
        assert any("未知药" in w.get("message", "") for w in warnings)

    def test_unknown_unit_produces_warning(self, herb_lookup, unit_lookup):
        """未知单位应产生 warning，不静默吞掉。"""
        item = _sample_formula()
        item["composition"] = [
            {"herb": "党参", "dose": 3, "unit": "片"},
        ]
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        warnings = [x for x in issues if x["level"] == "warning"]
        assert len(warnings) >= 1
        assert any("片" in w.get("message", "") for w in warnings)

    def test_dose_negative_produces_blocker(self, herb_lookup, unit_lookup):
        item = _sample_formula()
        item["composition"] = [
            {"herb": "党参", "dose": -5, "unit": "g"},
        ]
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("dose" in b.get("message", "") for b in blockers)

    def test_dose_zero_produces_blocker(self, herb_lookup, unit_lookup):
        item = _sample_formula()
        item["composition"] = [
            {"herb": "党参", "dose": 0, "unit": "g"},
        ]
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("dose" in b.get("message", "") for b in blockers)

    def test_missing_herb_field(self, herb_lookup, unit_lookup):
        item = _sample_formula()
        item["composition"] = [
            {"dose": 10, "unit": "g"},
        ]
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("herb" in b.get("message", "") for b in blockers)

    def test_missing_unit_defaults_to_g(self, herb_lookup, unit_lookup):
        """缺少 unit 字段时默认使用 'g'，不应产生 warning（g 为标准单位）。"""
        item = _sample_formula()
        item["composition"] = [
            {"herb": "党参", "dose": 10},
        ]
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        # 无 warning — 默认 'g' 是合法标准单位
        assert len(issues) == 0

    def test_known_herb_no_warning(self, herb_lookup, unit_lookup):
        """已知药味不产生 warning。"""
        item = _sample_formula()
        item["composition"] = [
            {"herb": "党参", "dose": 10, "unit": "g"},
            {"herb": "白术", "dose": 9, "unit": "g"},
        ]
        issues = validate_formula(item, 0, herb_lookup, unit_lookup)
        warnings = [x for x in issues if x["level"] == "warning"]
        herb_warnings = [w for w in warnings if "药味" in w.get("message", "")]
        assert len(herb_warnings) == 0

    def test_maps_herb_aliases(self, herb_lookup, unit_lookup):
        """药材别名应能映射。"""
        lookup_with_aliases = {"党参": "id1", "潞党参": "id1"}
        item = _sample_formula()
        item["composition"] = [
            {"herb": "潞党参", "dose": 10, "unit": "g"},
        ]
        issues = validate_formula(item, 0, lookup_with_aliases, unit_lookup)
        warnings = [x for x in issues if x["level"] == "warning"]
        herb_warnings = [w for w in warnings if "药味" in w.get("message", "")]
        assert len(herb_warnings) == 0


# ===================================================================
# 校验测试 — acupoints
# ===================================================================


class TestValidateAcupoint:
    """穴位校验。"""

    def test_valid_passes(self):
        item = _sample_acupoint()
        issues = validate_acupoint(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_missing_name(self):
        item = {"name": ""}
        issues = validate_acupoint(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("名称" in b["message"] for b in blockers)


# ===================================================================
# 校验测试 — theory_cases
# ===================================================================


class TestValidateTheoryCase:
    """理论/医案校验。"""

    def test_valid_theory_passes(self):
        item = _sample_theory()
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_valid_case_passes(self):
        item = _sample_case_ok()
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert len(blockers) == 0

    def test_invalid_entry_type(self):
        item = {"entry_type": "invalid", "title": "test", "content": "test"}
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("entry_type" in b.get("field", "") for b in blockers)

    def test_missing_title(self):
        item = {"entry_type": "theory", "title": "", "content": "test"}
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("标题" in b["message"] for b in blockers)

    def test_missing_content(self):
        item = {"entry_type": "theory", "title": "test", "content": ""}
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("正文" in b["message"] for b in blockers)

    def test_case_without_deidentified_marked(self):
        """未标记 deidentified 的医案应产生 blocker。"""
        item = _sample_case_ok()
        item["metadata"] = {"tags": ["test"]}
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert any("deidentified" in b.get("field", "") or "脱敏" in b.get("message", "")
                   for b in blockers), f"blockers: {blockers}"

    def test_theory_does_not_require_deidentified(self):
        """理论条目不需要 deidentified 标记。"""
        item = _sample_theory()
        item["metadata"] = {"tags": ["test"]}
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        deid_blockers = [b for b in blockers if "deidentified" in b.get("field", "") or "脱敏" in b.get("message", "")]
        assert len(deid_blockers) == 0

    def test_generic_patient_phrasing_is_not_treated_as_a_name(self):
        """临床叙述中的通用“患者”措辞不应被误判为姓名。"""
        item = _sample_case_ok()
        item["content"] = "患者：男，35岁。患者家属诉头痛，患者忌食寒凉。"
        item["metadata"]["deidentified"] = True
        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]
        assert blockers == []

    @pytest.mark.parametrize(
        ("content", "expected_code", "secret"),
        [
            ("患者姓名：张伟，主诉头痛。", "labeled_name", "张伟"),
            ("联系电话：13800138000，主诉头痛。", "mobile_phone", "13800138000"),
            ("身份证号：110101199001011234，主诉头痛。", "national_id", "110101199001011234"),
            ("病历号：ABC-123，主诉头痛。", "record_identifier", "ABC-123"),
        ],
    )
    def test_explicit_direct_identifier_is_blocked_without_echoing_value(
        self,
        content,
        expected_code,
        secret,
    ):
        item = _sample_case_ok()
        item["content"] = content
        item["metadata"]["deidentified"] = True

        issues = validate_theory_case(item, 0)
        blockers = [x for x in issues if x["level"] == "blocker"]

        assert len(blockers) == 1
        assert blockers[0]["indicator_counts"][expected_code] >= 1
        assert secret not in json.dumps(blockers, ensure_ascii=False)


# ===================================================================
# 数据文件加载测试
# ===================================================================


class TestLoadJson:
    """JSON 文件加载。"""

    def test_loads_sample_dosage_units(self):
        path = _PROJECT_ROOT / "data" / "sample_dosage_units.json"
        data = load_json(path)
        assert isinstance(data, list)
        assert len(data) > 0
        assert all(isinstance(item, dict) for item in data)

    def test_loads_sample_herbs(self):
        path = _PROJECT_ROOT / "data" / "sample_herbs.json"
        data = load_json(path)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_loads_sample_formulas(self):
        path = _PROJECT_ROOT / "data" / "sample_formulas.json"
        data = load_json(path)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_loads_sample_acupoints(self):
        path = _PROJECT_ROOT / "data" / "sample_acupoints.json"
        data = load_json(path)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_loads_sample_theory(self):
        path = _PROJECT_ROOT / "data" / "sample_theory.json"
        data = load_json(path)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_loads_sample_cases(self):
        path = _PROJECT_ROOT / "data" / "sample_cases.json"
        data = load_json(path)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_json(Path("/nonexistent/file.json"))


# ===================================================================
# 真实样例数据校验测试
# ===================================================================


class TestSampleDataValidation:
    """对真实样例 JSON 文件进行校验，验证预期 warning。"""

    def test_all_dosage_units_valid(self):
        path = _PROJECT_ROOT / "data" / "sample_dosage_units.json"
        data = load_json(path)
        all_blockers = []
        for i, item in enumerate(data):
            issues = validate_dosage_unit(item, i)
            all_blockers.extend([x for x in issues if x["level"] == "blocker"])
        assert len(all_blockers) == 0, f"Unexpected blockers in dosage_units: {all_blockers}"

    def test_all_herbs_valid(self):
        path = _PROJECT_ROOT / "data" / "sample_herbs.json"
        data = load_json(path)
        all_blockers = []
        all_warnings = []
        for i, item in enumerate(data):
            issues = validate_herb(item, i)
            all_blockers.extend([x for x in issues if x["level"] == "blocker"])
            all_warnings.extend([x for x in issues if x["level"] == "warning"])
        assert len(all_blockers) == 0, f"Unexpected blockers in herbs: {all_blockers}"
        # 所有样例药材都有 max_dose，不应有 max_dose 缺口
        max_dose_warnings = [w for w in all_warnings if "max_dose" in w.get("field", "")]
        assert len(max_dose_warnings) == 0, f"Unexpected max_dose gaps: {max_dose_warnings}"

    def test_sample_formulas_have_expected_unknown_herbs(self):
        """验证 sample_formulas.json 中存在无法映射到 sample_herbs.json 的药味。

        这不是错误——样例数据故意留了缺口用于测试 gap report。
        本测试确认这些缺口能被检测到。
        """
        # 从 sample_herbs.json 构建 lookup
        herbs_data = load_json(_PROJECT_ROOT / "data" / "sample_herbs.json")
        herb_names: set[str] = set()
        for h in herbs_data:
            herb_names.add(h["name"])
            for alias in h.get("aliases", []):
                herb_names.add(str(alias))

        # 收集所有方剂中用到的药味
        formulas_data = load_json(_PROJECT_ROOT / "data" / "sample_formulas.json")
        all_comp_herbs: set[str] = set()
        for f in formulas_data:
            for c in f.get("composition", []):
                herb = (c.get("herb") or "").strip()
                if herb:
                    all_comp_herbs.add(herb)

        # 找出不匹配的药味
        unknown = all_comp_herbs - herb_names
        assert len(unknown) > 0, (
            "预期方剂中存在无法映射的药味（如山药、甘草等未在 sample_herbs.json 中），"
            "这用于测试 gap report"
        )

    def test_sample_formulas_have_unknown_unit_pian(self):
        """验证二陈汤中的'片'单位无法映射到 dosage_units——预期缺口。"""
        units_data = load_json(_PROJECT_ROOT / "data" / "sample_dosage_units.json")
        unit_names: set[str] = set()
        for u in units_data:
            unit_names.add(u["unit_name"])
            for alias in u.get("aliases", []):
                unit_names.add(str(alias))

        formulas_data = load_json(_PROJECT_ROOT / "data" / "sample_formulas.json")
        all_units: set[str] = set()
        for f in formulas_data:
            for c in f.get("composition", []):
                unit = (c.get("unit") or "").strip()
                if unit:
                    all_units.add(unit)

        unknown_units = all_units - unit_names
        assert "片" in unknown_units, (
            "预期'片'单位无法映射到 dosage_units（二陈汤中生姜 3片），"
            "用于测试 gap report"
        )

    def test_dosage_units_no_to_gram_factor(self):
        """验证 dosage_units 样例数据中不存在 to_gram_factor 字段。"""
        path = _PROJECT_ROOT / "data" / "sample_dosage_units.json"
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        assert "to_gram_factor" not in raw, "sample_dosage_units.json 不应包含 to_gram_factor"

    def test_all_sample_cases_are_deidentified(self):
        """验证 sample_cases.json 中所有条目都标记为模拟/脱敏数据。"""
        path = _PROJECT_ROOT / "data" / "sample_cases.json"
        data = load_json(path)
        for i, item in enumerate(data):
            issues = validate_theory_case(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            deid_blockers = [b for b in blockers
                             if "deidentified" in b.get("field", "") or "脱敏" in b.get("message", "")
                             or "真实" in b.get("message", "")]
            assert len(deid_blockers) == 0, (
                f"医案[{i}] '{item.get('title','')}' 疑似未脱敏: {deid_blockers}"
            )

    def test_all_theory_entries_valid(self):
        """验证 sample_theory.json 中所有条目校验通过。"""
        path = _PROJECT_ROOT / "data" / "sample_theory.json"
        data = load_json(path)
        for i, item in enumerate(data):
            issues = validate_theory_case(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            assert len(blockers) == 0, f"Theory[{i}] blockers: {blockers}"

    def test_all_acupoints_valid(self):
        """验证 sample_acupoints.json 中所有条目校验通过。"""
        path = _PROJECT_ROOT / "data" / "sample_acupoints.json"
        data = load_json(path)
        for i, item in enumerate(data):
            issues = validate_acupoint(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            assert len(blockers) == 0, f"Acupoint[{i}] blockers: {blockers}"


# ===================================================================
# 常量验证
# ===================================================================


class TestConstants:
    """基础常量验证。"""

    def test_liang_grams(self):
        assert LIANG_GRAMS == 30.0

    def test_qian_grams(self):
        assert QIAN_GRAMS == 3.0

    def test_valid_conversion_types(self):
        assert {"standard", "fixed", "herb_specific", "unsupported"} == VALID_CONVERSION_TYPE

    def test_valid_pregnancy_values(self):
        assert {"forbidden", "caution", "none"} == VALID_PREGNANCY

    def test_valid_entry_types(self):
        assert {"theory", "case"} == VALID_ENTRY_TYPE


# ===================================================================
# 幂等性测试（需要数据库）
# ===================================================================


@pytest.mark.integration
class TestIdempotency:
    """幂等性集成测试 — 需要 PostgreSQL。

    运行方式：
        docker compose up -d postgres
        uv run alembic upgrade head
        uv run pytest tests/test_import_knowledge.py::TestIdempotency -v
    """

    @pytest.fixture(autouse=True)
    async def _setup_db(self):
        """确保数据库可用；不可用时自动跳过集成测试。

        每个测试方法独立获取全新 session factory，避免跨测试方法
        复用全局引擎导致连接池中的连接被清理后仍被复用的竞态问题。
        """
        from sqlalchemy import text

        from app.db.session import get_session_factory, reset_session_factory
        from app.models import (  # noqa: F401
            Acupoint,
            DosageUnit,
            Formula,
            Herb,
            KnowledgeSource,
            TheoryCase,
        )

        # 重置全局引擎，确保每个测试方法获得独立连接池
        await reset_session_factory()
        session_factory = get_session_factory()
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            # 只跳过真正的连接拒绝异常（PostgreSQL 未启动/网络不通），
            # 不吞掉 AttributeError、TypeError 等代码逻辑错误。
            if isinstance(exc, (AttributeError, TypeError)):
                raise
            pytest.fail(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}")

        self._session_factory = session_factory

    async def _count_table(self, model) -> int:
        from sqlalchemy import func, select

        async with self._session_factory() as session:
            result = await session.execute(select(func.count()).select_from(model))
            return result.scalar_one()

    async def test_dosage_units_idempotent(self):
        """dosage_units 重复导入记录数不增加。"""
        from scripts.import_knowledge import KnowledgeImporter, load_json

        path = _PROJECT_ROOT / "data" / "sample_dosage_units.json"
        data = load_json(path)

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)

            # 第一次导入
            _r1 = await importer.import_dosage_units(data)
            await session.commit()

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)

            # 第二次导入
            result2 = await importer.import_dosage_units(data)
            await session.commit()

        # 第二次导入不应产生新增
        assert result2.inserted == 0, f"第二次导入应无新增，实际: {result2.inserted}"
        assert result2.updated >= 0  # 可能更新
        assert result2.skipped == 0

    async def test_herbs_idempotent(self):
        """herbs 重复导入记录数不增加。"""
        from scripts.import_knowledge import KnowledgeImporter, load_json

        path = _PROJECT_ROOT / "data" / "sample_herbs.json"
        data = load_json(path)

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            _r1 = await importer.import_herbs(data, str(path))
            await session.commit()

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            result2 = await importer.import_herbs(data, str(path))
            await session.commit()

        assert result2.inserted == 0, f"第二次导入应无新增，实际: {result2.inserted}"

    async def test_formulas_idempotent(self):
        """formulas 重复导入记录数不增加。"""
        from scripts.import_knowledge import KnowledgeImporter, load_json

        path = _PROJECT_ROOT / "data" / "sample_formulas.json"
        data = load_json(path)

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            # 需要先导入 herbs 以建立 lookup
            herbs_data = load_json(_PROJECT_ROOT / "data" / "sample_herbs.json")
            await importer.import_herbs(herbs_data, str(_PROJECT_ROOT / "data" / "sample_herbs.json"))
            await session.commit()

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            _r1 = await importer.import_formulas(data, str(path))
            await session.commit()

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            result2 = await importer.import_formulas(data, str(path))
            await session.commit()

        assert result2.inserted == 0, f"第二次导入应无新增，实际: {result2.inserted}"

    async def test_acupoints_idempotent(self):
        """acupoints 重复导入记录数不增加。"""
        from scripts.import_knowledge import KnowledgeImporter, load_json

        path = _PROJECT_ROOT / "data" / "sample_acupoints.json"
        data = load_json(path)

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            _r1 = await importer.import_acupoints(data, str(path))
            await session.commit()

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            result2 = await importer.import_acupoints(data, str(path))
            await session.commit()

        assert result2.inserted == 0, f"第二次导入应无新增，实际: {result2.inserted}"

    async def test_theory_cases_idempotent(self):
        """theory_cases 重复导入记录数不增加。"""
        from scripts.import_knowledge import KnowledgeImporter, load_json

        path = _PROJECT_ROOT / "data" / "sample_theory.json"
        data = load_json(path)

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            _r1 = await importer.import_theory_cases(data, str(path), entry_type_override="theory")
            await session.commit()

        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)
            result2 = await importer.import_theory_cases(data, str(path), entry_type_override="theory")
            await session.commit()

        assert result2.inserted == 0, f"第二次导入应无新增，实际: {result2.inserted}"

    async def test_full_import_idempotent(self):
        """完整 --all 导入两次，各表记录数稳定。"""
        from scripts.import_knowledge import KnowledgeImporter, load_json

        # 第一次全量导入（不包含 dosage_units 以避免与 seed 冲突）
        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)

            # herbs
            await importer.import_herbs(
                load_json(_PROJECT_ROOT / "data" / "sample_herbs.json"),
                str(_PROJECT_ROOT / "data" / "sample_herbs.json"),
            )
            # formulas
            await importer.import_formulas(
                load_json(_PROJECT_ROOT / "data" / "sample_formulas.json"),
                str(_PROJECT_ROOT / "data" / "sample_formulas.json"),
            )
            # acupoints
            await importer.import_acupoints(
                load_json(_PROJECT_ROOT / "data" / "sample_acupoints.json"),
                str(_PROJECT_ROOT / "data" / "sample_acupoints.json"),
            )
            # theory
            await importer.import_theory_cases(
                load_json(_PROJECT_ROOT / "data" / "sample_theory.json"),
                str(_PROJECT_ROOT / "data" / "sample_theory.json"),
                entry_type_override="theory",
            )
            # cases
            await importer.import_theory_cases(
                load_json(_PROJECT_ROOT / "data" / "sample_cases.json"),
                str(_PROJECT_ROOT / "data" / "sample_cases.json"),
                entry_type_override="case",
            )
            await session.commit()

        # 第二次全量导入
        async with self._session_factory() as session:
            importer = KnowledgeImporter(session)

            r_herbs = await importer.import_herbs(
                load_json(_PROJECT_ROOT / "data" / "sample_herbs.json"),
                str(_PROJECT_ROOT / "data" / "sample_herbs.json"),
            )
            r_formulas = await importer.import_formulas(
                load_json(_PROJECT_ROOT / "data" / "sample_formulas.json"),
                str(_PROJECT_ROOT / "data" / "sample_formulas.json"),
            )
            r_acupoints = await importer.import_acupoints(
                load_json(_PROJECT_ROOT / "data" / "sample_acupoints.json"),
                str(_PROJECT_ROOT / "data" / "sample_acupoints.json"),
            )
            r_theory = await importer.import_theory_cases(
                load_json(_PROJECT_ROOT / "data" / "sample_theory.json"),
                str(_PROJECT_ROOT / "data" / "sample_theory.json"),
                entry_type_override="theory",
            )
            r_cases = await importer.import_theory_cases(
                load_json(_PROJECT_ROOT / "data" / "sample_cases.json"),
                str(_PROJECT_ROOT / "data" / "sample_cases.json"),
                entry_type_override="case",
            )
            await session.commit()

        # 第二次导入所有类型都应为 0 新增
        assert r_herbs.inserted == 0, f"herbs 重复导入新增: {r_herbs.inserted}"
        assert r_formulas.inserted == 0, f"formulas 重复导入新增: {r_formulas.inserted}"
        assert r_acupoints.inserted == 0, f"acupoints 重复导入新增: {r_acupoints.inserted}"
        assert r_theory.inserted == 0, f"theory 重复导入新增: {r_theory.inserted}"
        assert r_cases.inserted == 0, f"cases 重复导入新增: {r_cases.inserted}"
