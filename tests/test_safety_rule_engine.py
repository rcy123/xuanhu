"""P6-3 Safety Rule Engine 测试。

纯函数测试（不依赖数据库）：测试全部规则函数的确定性逻辑。
集成测试（需 PostgreSQL）：测试 SafetyRuleEngine.check() 和 Supervisor 集成。

覆盖 `docs/安全审核规则设计文档.md` §13 的全部核心测试用例。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.safety.datasets import (
    EIGHTEEN_INCOMPATIBILITY_TABLE,
    NINETEEN_FEARS_TABLE,
    expand_group,
    is_toxic_or_strong_herb,
)
from app.safety.engine import (
    SafetyRuleEngine,
    _check_allergy,
    _check_combination_incompatibilities,
    _check_dose_limits,
    _check_eighteen_incompatibilities,
    _check_nineteen_fears,
    _check_pregnancy,
    _check_unknown_herbs,
    _convert_all_doses,
    _convert_dose,
    _deduplicate,
    _determine_passed,
    _normalize_composition,
    _sort_issues,
)
from app.safety.normalizer import HerbNormalizer
from app.safety.rule_version import SAFETY_RULE_VERSION
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    PatientInfo,
    SafetyIssue,
    SafetyRuleResult,
)
from app.schemas.types import (
    PregnancyStatus,
    SafetyIssueType,
    Severity,
)

# ---------------------------------------------------------------------------
# 测试辅助：构建内存测试对象
# ---------------------------------------------------------------------------


@dataclass
class _FakeHerb:
    """模拟 Herb ORM 的最小内存对象。"""

    name: str
    aliases: list[str]
    max_dose: float | None
    pregnancy_contraindication: str = "none"
    incompatibilities: list[dict[str, str]] | None = None

    def __hash__(self) -> int:
        return hash(self.name)


def _make_herb(
    name: str,
    aliases: list[str] | None = None,
    max_dose: float | None = None,
    pregnancy: str = "none",
    incompatibilities: list[dict[str, str]] | None = None,
) -> _FakeHerb:
    return _FakeHerb(
        name=name,
        aliases=aliases or [],
        max_dose=max_dose,
        pregnancy_contraindication=pregnancy,
        incompatibilities=incompatibilities,
    )


@dataclass
class _FakeDosageUnit:
    """模拟 DosageUnit ORM 的最小内存对象。"""

    unit_name: str
    aliases: list[str]
    to_grams: float | None
    conversion_type: str


def _make_unit(
    unit_name: str,
    aliases: list[str] | None = None,
    to_grams: float | None = None,
    conversion_type: str = "standard",
) -> _FakeDosageUnit:
    return _FakeDosageUnit(
        unit_name=unit_name,
        aliases=aliases or [],
        to_grams=to_grams,
        conversion_type=conversion_type,
    )


def _make_patient(
    pregnancy_status: str = "no",
    allergies: list[str] | None = None,
) -> PatientInfo:
    return PatientInfo(
        pregnancy_status=PregnancyStatus(pregnancy_status),
        allergies=allergies or [],
    )


def _make_formula(name: str, composition: list[HerbDose]) -> FormulaResult:
    return FormulaResult(name=name, composition=composition, rationale="测试")


def _build_normalizer(
    herbs: list[_FakeHerb] | None = None,
) -> HerbNormalizer:

    class _Adapter:
        __slots__ = ("_h",)

        def __init__(self, h: _FakeHerb) -> None:
            self._h = h

        @property
        def standard_name(self) -> str:
            return self._h.name

        @property
        def aliases(self) -> list[str]:
            return self._h.aliases

    n = HerbNormalizer()
    for h in (herbs or []):
        n.register(_Adapter(h))
    return n


def _build_herb_dict(herbs: list[_FakeHerb]) -> dict[str, Any]:
    return {h.name: h for h in herbs}


def _build_unit_dict(units: list[_FakeDosageUnit]) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for u in units:
        table[u.unit_name] = u
        for a in u.aliases:
            table[a] = u
    return table


# ---------------------------------------------------------------------------
# 1. 药名标准化
# ---------------------------------------------------------------------------


class TestNormalizeComposition:
    def test_alias_mapped_to_standard(self):
        herbs = [_make_herb("甘草", aliases=["国老", "甜草"])]
        normalizer = _build_normalizer(herbs)
        composition = [HerbDose(herb="国老", dose=6, unit="g")]
        result = _normalize_composition(composition, normalizer)
        assert result[0].herb == "甘草"

    def test_unknown_herb_returned_as_is(self):
        normalizer = _build_normalizer([])
        composition = [HerbDose(herb="未知药", dose=10, unit="g")]
        result = _normalize_composition(composition, normalizer)
        assert result[0].herb == "未知药"

    def test_empty_composition(self):
        normalizer = _build_normalizer([])
        result = _normalize_composition([], normalizer)
        assert result == []

    def test_conflicting_alias_is_order_independent_and_fails_closed(self):
        first = _build_normalizer(
            [
                _make_herb("canonical-a", aliases=["shared-alias"]),
                _make_herb("canonical-b", aliases=["shared-alias"]),
            ]
        )
        reversed_order = _build_normalizer(
            [
                _make_herb("canonical-b", aliases=["shared-alias"]),
                _make_herb("canonical-a", aliases=["shared-alias"]),
            ]
        )

        assert first.normalize("shared-alias") == "shared-alias"
        assert reversed_order.normalize("shared-alias") == "shared-alias"

    def test_canonical_name_wins_over_an_alias_with_the_same_text(self):
        normalizer = _build_normalizer(
            [
                _make_herb("canonical-a", aliases=["canonical-b"]),
                _make_herb("canonical-b"),
            ]
        )

        assert normalizer.normalize("canonical-b") == "canonical-b"


class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _CapturingDb:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statement: object | None = None

    async def execute(self, statement: object) -> _FakeExecuteResult:
        self.statement = statement
        return _FakeExecuteResult(self.rows)


class TestDeterministicAuthorityLoading:
    @pytest.mark.asyncio
    async def test_herb_query_excludes_soft_deleted_rows_with_stable_order(self):
        db = _CapturingDb([])
        engine = SafetyRuleEngine(db)  # type: ignore[arg-type]

        assert await engine._load_herb_records() == {}
        sql = str(db.statement)
        assert "herbs.deleted_at IS NULL" in sql
        assert "ORDER BY herbs.name, herbs.id" in sql

    @pytest.mark.asyncio
    async def test_conflicting_unit_alias_is_omitted_regardless_of_row_order(self):
        first = _make_unit("unit-a", aliases=["shared"], to_grams=1)
        second = _make_unit("unit-b", aliases=["shared"], to_grams=2)

        for rows in ([first, second], [second, first]):
            db = _CapturingDb(rows)
            engine = SafetyRuleEngine(db)  # type: ignore[arg-type]
            table = await engine._load_unit_table()
            assert set(table) == {"unit-a", "unit-b"}
            assert "ORDER BY dosage_units.unit_name, dosage_units.id" in str(db.statement)


# ---------------------------------------------------------------------------
# 1.5 未知药名检查
# ---------------------------------------------------------------------------


class TestUnknownHerbs:
    def test_known_herb_no_issue(self):
        herbs = {_make_herb("党参", max_dose=30).name: _make_herb("党参", max_dose=30)}
        normalizer = _build_normalizer(list(herbs.values()))
        issues = _check_unknown_herbs(["党参"], herbs, normalizer)
        assert len(issues) == 0

    def test_unknown_herb_generates_high_caution(self):
        """未知药名生成 high 级别 caution。"""
        herbs: dict[str, Any] = {_make_herb("党参").name: _make_herb("党参")}
        normalizer = _build_normalizer(list(herbs.values()))
        issues = _check_unknown_herbs(["未知药"], herbs, normalizer)
        assert len(issues) == 1
        assert issues[0].severity == Severity.HIGH
        assert issues[0].type == SafetyIssueType.CAUTION
        assert "未知药" in issues[0].herbs

    def test_mixed_known_and_unknown(self):
        """已知+未知的混合处方：仅对未知药报 issue。"""
        herbs = {_make_herb("党参", max_dose=30).name: _make_herb("党参", max_dose=30)}
        normalizer = _build_normalizer(list(herbs.values()))
        issues = _check_unknown_herbs(["党参", "外星草"], herbs, normalizer)
        assert len(issues) == 1
        assert issues[0].herbs == ["外星草"]

    def test_multiple_unknown_deduped_by_herb_name(self):
        """同一未知药名不重复报告（检查函数已去重）。"""
        herbs: dict[str, Any] = {}
        normalizer = _build_normalizer([])
        issues = _check_unknown_herbs(["X", "X", "Y"], herbs, normalizer)
        assert len(issues) == 2  # X, Y 各一条

    def test_unknown_herb_causes_passed_false_in_full_check(self):
        """未知药 + 正常剂量 → passed=False（high 阻断）。"""
        # 模拟完整流程：标准化 + 未知检查 + passed 判定
        herbs = {_make_herb("党参", max_dose=30).name: _make_herb("党参", max_dose=30)}
        normalizer = _build_normalizer(list(herbs.values()))
        herb_names = ["党参", "外星草"]

        all_issues: list[SafetyIssue] = []
        all_issues.extend(_check_unknown_herbs(herb_names, herbs, normalizer))
        all_issues.extend(
            _check_eighteen_incompatibilities(herb_names, normalizer)
        )
        all_issues.extend(
            _check_nineteen_fears(herb_names, normalizer)
        )

        all_issues = _deduplicate(all_issues)
        all_issues = _sort_issues(all_issues)

        # 未知药导致 high → passed=False
        assert _determine_passed(all_issues) is False
        assert any(
            i.type == SafetyIssueType.CAUTION and i.severity == Severity.HIGH
            for i in all_issues
        )


# ---------------------------------------------------------------------------
# 2. 剂量单位换算
# ---------------------------------------------------------------------------


class TestConvertDose:
    def test_standard_unit_g(self):
        units = [_make_unit("g", to_grams=1.0, conversion_type="standard")]
        normalizer = _build_normalizer([])
        dose, issue, warning = _convert_dose(
            "党参", 10.0, "g", _build_unit_dict(units), {}, normalizer
        )
        assert dose == 10.0
        assert issue is None
        assert warning is None

    def test_qian_to_g(self):
        units = [_make_unit("钱", to_grams=3.0, conversion_type="fixed")]
        normalizer = _build_normalizer([])
        dose, issue, warning = _convert_dose(
            "细辛", 2.0, "钱", _build_unit_dict(units), {}, normalizer
        )
        assert dose == 6.0
        assert issue is None

    def test_liang_to_g(self):
        units = [_make_unit("两", to_grams=30.0, conversion_type="fixed")]
        normalizer = _build_normalizer([])
        dose, issue, warning = _convert_dose(
            "石膏", 1.0, "两", _build_unit_dict(units), {}, normalizer
        )
        assert dose == 30.0
        assert issue is None

    def test_herb_specific_unit(self):
        units = [_make_unit("枚", to_grams=None, conversion_type="herb_specific")]
        normalizer = _build_normalizer([])
        dose, issue, warning = _convert_dose(
            "大枣", 5.0, "枚", _build_unit_dict(units), {}, normalizer
        )
        assert dose is None
        assert issue is not None
        assert issue.severity == Severity.WARNING
        assert issue.type == SafetyIssueType.UNIT_CONVERSION

    def test_unsupported_unit(self):
        units = [_make_unit("适量", to_grams=None, conversion_type="unsupported")]
        normalizer = _build_normalizer([])
        dose, issue, warning = _convert_dose(
            "茯苓", 3.0, "适量", _build_unit_dict(units), {}, normalizer
        )
        assert dose is None
        assert issue is not None
        assert issue.severity == Severity.WARNING

    def test_unknown_unit_normal_herb(self):
        normalizer = _build_normalizer([])
        dose, issue, warning = _convert_dose(
            "茯苓", 3.0, "撮", {}, {}, normalizer
        )
        assert dose is None
        assert issue is not None
        assert issue.severity == Severity.WARNING
        assert issue.type == SafetyIssueType.UNIT_CONVERSION

    def test_unknown_unit_toxic_herb(self):
        normalizer = _build_normalizer([])
        dose, issue, warning = _convert_dose(
            "附子", 5.0, "撮", {}, {}, normalizer
        )
        assert dose is None
        assert issue is not None
        assert issue.severity == Severity.HIGH
        assert issue.type == SafetyIssueType.UNIT_CONVERSION


class TestConvertAllDoses:
    def test_batch_convert(self):
        units = [
            _make_unit("g", to_grams=1.0, conversion_type="standard"),
            _make_unit("钱", to_grams=3.0, conversion_type="fixed"),
        ]
        normalizer = _build_normalizer([])
        composition = [
            HerbDose(herb="党参", dose=10, unit="g"),
            HerbDose(herb="细辛", dose=2, unit="钱"),
        ]
        issues, warnings, converted = _convert_all_doses(
            composition, _build_unit_dict(units), {}, normalizer
        )
        assert len(issues) == 0
        assert converted[0].dose == 10.0
        assert converted[1].dose == 6.0


# ---------------------------------------------------------------------------
# 3. 十八反
# ---------------------------------------------------------------------------


class TestEighteenIncompatibilities:
    def test_gancao_haizao_hit(self):
        """18A-1: 甘草-海藻 十八反命中"""
        herbs = [_make_herb("甘草"), _make_herb("海藻")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["甘草", "海藻", "茯苓"], normalizer
        )
        assert len(issues) == 1
        assert issues[0].severity == Severity.BLOCKER
        assert issues[0].type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES
        assert set(issues[0].herbs) == {"甘草", "海藻"}

    def test_fuzi_banxia_hit(self):
        """18A-2: 附子-半夏 十八反命中"""
        herbs = [_make_herb("附子"), _make_herb("半夏")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["附子", "半夏", "生姜"], normalizer
        )
        assert len(issues) == 1
        assert set(issues[0].herbs) == {"附子", "半夏"}

    def test_chuanwu_gualou_hit(self):
        """18A-3: 川乌-瓜蒌 十八反命中"""
        herbs = [_make_herb("川乌"), _make_herb("瓜蒌")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["川乌", "瓜蒌皮"], normalizer
        )
        assert len(issues) >= 1
        # 瓜蒌皮展开到瓜蒌
        assert any(
            set(i.herbs) & {"川乌", "瓜蒌"} for i in issues
            if i.type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES
        )

    def test_caowu_beimu_hit(self):
        """18A-4: 草乌-贝母 十八反命中"""
        herbs = [_make_herb("草乌"), _make_herb("浙贝母")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["草乌", "浙贝母"], normalizer
        )
        assert len(issues) >= 1
        assert any(
            i.type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES
            for i in issues
        )

    def test_lilu_renshen_hit(self):
        """18A-5: 藜芦-人参 十八反命中"""
        herbs = [_make_herb("藜芦"), _make_herb("人参")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["藜芦", "人参"], normalizer
        )
        assert len(issues) == 1
        assert set(issues[0].herbs) == {"藜芦", "人参"}

    def test_lilu_dangshen_conservative_hit(self):
        """18A-6: 藜芦-党参 保守命中"""
        herbs = [_make_herb("藜芦"), _make_herb("党参")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["藜芦", "党参"], normalizer
        )
        assert len(issues) >= 1
        assert any(
            i.type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES
            for i in issues
        )

    def test_lilu_shaoyao_hit(self):
        """18A-7: 藜芦-芍药 命中"""
        herbs = [_make_herb("藜芦"), _make_herb("白芍")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["藜芦", "白芍"], normalizer
        )
        assert len(issues) >= 1
        assert any(
            i.type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES
            for i in issues
        )

    def test_normal_prescription_no_hit(self):
        """18A-8: 正常处方 无十八反"""
        herbs = [_make_herb("麻黄"), _make_herb("桂枝"), _make_herb("杏仁"), _make_herb("甘草")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["麻黄", "桂枝", "杏仁", "甘草"], normalizer
        )
        assert len(issues) == 0

    def test_fuzi_gancao_not_incompat(self):
        """18A-9: 附子+甘草 非反药对"""
        herbs = [_make_herb("附子"), _make_herb("甘草")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["附子", "甘草"], normalizer
        )
        assert len(issues) == 0

    def test_alias_aconitum_hit(self):
        """18A-10: 别名匹配（乌头=川乌）"""
        herbs = [_make_herb("川乌", aliases=["乌头"]), _make_herb("半夏")]
        normalizer = _build_normalizer(herbs)
        issues = _check_eighteen_incompatibilities(
            ["乌头", "半夏"], normalizer
        )
        assert len(issues) >= 1
        assert any(
            i.type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES
            for i in issues
        )


# ---------------------------------------------------------------------------
# 4. 十九畏
# ---------------------------------------------------------------------------


class TestNineteenFears:
    def test_dingxiang_yujin_hit(self):
        """19A-1: 丁香-郁金 十九畏命中"""
        herbs = [_make_herb("丁香"), _make_herb("郁金")]
        normalizer = _build_normalizer(herbs)
        issues = _check_nineteen_fears(["丁香", "郁金"], normalizer)
        assert len(issues) == 1
        assert issues[0].severity == Severity.BLOCKER
        assert issues[0].type == SafetyIssueType.NINETEEN_FEARS

    def test_renshen_wulingzhi_hit(self):
        """19A-2: 人参-五灵脂 十九畏命中"""
        herbs = [_make_herb("人参"), _make_herb("五灵脂")]
        normalizer = _build_normalizer(herbs)
        issues = _check_nineteen_fears(["人参", "五灵脂"], normalizer)
        assert len(issues) == 1

    def test_badou_qianniuzi_hit(self):
        """19A-3: 巴豆-牵牛子 十九畏命中"""
        herbs = [_make_herb("巴豆"), _make_herb("牵牛子")]
        normalizer = _build_normalizer(herbs)
        issues = _check_nineteen_fears(["巴豆霜", "牵牛子"], normalizer)
        assert len(issues) >= 1
        assert any(
            i.type == SafetyIssueType.NINETEEN_FEARS for i in issues
        )

    def test_rougui_chishizhi_hit(self):
        """19A-4: 肉桂-赤石脂 十九畏命中"""
        herbs = [_make_herb("肉桂"), _make_herb("赤石脂")]
        normalizer = _build_normalizer(herbs)
        issues = _check_nineteen_fears(["肉桂", "赤石脂"], normalizer)
        assert len(issues) == 1

    def test_yaxiao_sanleng_hit(self):
        """19A-5: 牙硝-三棱 命中"""
        herbs = [_make_herb("芒硝"), _make_herb("三棱")]
        normalizer = _build_normalizer(herbs)
        issues = _check_nineteen_fears(["芒硝", "三棱"], normalizer)
        assert len(issues) >= 1
        assert any(
            i.type == SafetyIssueType.NINETEEN_FEARS for i in issues
        )

    def test_normal_prescription_no_hit(self):
        """19A-6: 正常处方 无十九畏"""
        herbs = [
            _make_herb("党参"), _make_herb("白术"),
            _make_herb("茯苓"), _make_herb("甘草"),
        ]
        normalizer = _build_normalizer(herbs)
        issues = _check_nineteen_fears(
            ["党参", "白术", "茯苓", "甘草"], normalizer
        )
        assert len(issues) == 0

    def test_renshen_baizhu_not_fear(self):
        """19A-7: 人参+白术 非畏药对"""
        herbs = [_make_herb("人参"), _make_herb("白术")]
        normalizer = _build_normalizer(herbs)
        issues = _check_nineteen_fears(["人参", "白术"], normalizer)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# 5. 妊娠禁忌
# ---------------------------------------------------------------------------


class TestPregnancy:
    def test_forbidden_ezhu(self):
        """PG-1: 妊娠禁用-莪术"""
        herbs = {_make_herb("莪术", pregnancy="forbidden").name: _make_herb("莪术", pregnancy="forbidden")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(pregnancy_status="pregnant")
        issues = _check_pregnancy(["莪术", "白术"], patient, herbs, normalizer)
        assert len(issues) == 1
        assert issues[0].severity == Severity.BLOCKER
        assert issues[0].type == SafetyIssueType.PREGNANCY
        assert "莪术" in issues[0].herbs

    def test_caution_taoren(self):
        """PG-3: 妊娠慎用-桃仁"""
        herbs = {_make_herb("桃仁", pregnancy="caution").name: _make_herb("桃仁", pregnancy="caution")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(pregnancy_status="pregnant")
        issues = _check_pregnancy(["桃仁", "当归"], patient, herbs, normalizer)
        assert len(issues) == 1
        assert issues[0].severity == Severity.HIGH
        assert "桃仁" in issues[0].herbs

    def test_not_pregnant_no_trigger(self):
        """PG-5: 非妊娠患者 不触发"""
        herbs = {_make_herb("莪术", pregnancy="forbidden").name: _make_herb("莪术", pregnancy="forbidden")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(pregnancy_status="no")
        issues = _check_pregnancy(["莪术", "三棱"], patient, herbs, normalizer)
        assert len(issues) == 0

    def test_unknown_pregnancy_caution(self):
        """PG-6: 妊娠未知 生成提示"""
        herbs: dict[str, Any] = {}
        normalizer = _build_normalizer([])
        patient = _make_patient(pregnancy_status="unknown")
        issues = _check_pregnancy(["莪术"], patient, herbs, normalizer)
        assert len(issues) == 1
        assert issues[0].type == SafetyIssueType.CAUTION
        assert issues[0].severity == Severity.WARNING

    def test_possible_pregnancy_strict(self):
        """PG-7: 妊娠可能 按严格标准"""
        herbs = {_make_herb("桃仁", pregnancy="caution").name: _make_herb("桃仁", pregnancy="caution")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(pregnancy_status="possible")
        issues = _check_pregnancy(["桃仁"], patient, herbs, normalizer)
        assert len(issues) == 1
        assert issues[0].severity == Severity.HIGH

    def test_banxia_caution_marked(self):
        """PG-8: 半夏 慎用标记"""
        herbs = {_make_herb("半夏", pregnancy="caution").name: _make_herb("半夏", pregnancy="caution")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(pregnancy_status="pregnant")
        issues = _check_pregnancy(["半夏", "生姜"], patient, herbs, normalizer)
        assert len(issues) == 1
        assert issues[0].severity == Severity.HIGH


# ---------------------------------------------------------------------------
# 6. 剂量上限
# ---------------------------------------------------------------------------


class TestDoseLimits:
    def test_normal_dose_pass(self):
        """DO-1: 正常剂量通过"""
        herbs = {_make_herb("党参", max_dose=30).name: _make_herb("党参", max_dose=30)}
        normalizer = _build_normalizer(list(herbs.values()))
        composition = [HerbDose(herb="党参", dose=12, unit="g")]
        units = _build_unit_dict([_make_unit("g", to_grams=1.0)])
        issues = _check_dose_limits(composition, normalizer, herbs, units)
        assert len(issues) == 0

    def test_double_high(self):
        """DO-2: 恰好 2 倍超量 → high"""
        herbs = {_make_herb("川芎", max_dose=10).name: _make_herb("川芎", max_dose=10)}
        normalizer = _build_normalizer(list(herbs.values()))
        composition = [HerbDose(herb="川芎", dose=20, unit="g")]
        units = _build_unit_dict([_make_unit("g", to_grams=1.0)])
        issues = _check_dose_limits(composition, normalizer, herbs, units)
        assert len(issues) == 1
        assert issues[0].severity == Severity.HIGH

    def test_severe_overdose_blocker(self):
        """DO-3: 严重超量 → blocker"""
        herbs = {_make_herb("附子", max_dose=15).name: _make_herb("附子", max_dose=15)}
        normalizer = _build_normalizer(list(herbs.values()))
        composition = [HerbDose(herb="附子", dose=45, unit="g")]
        units = _build_unit_dict([_make_unit("g", to_grams=1.0)])
        issues = _check_dose_limits(composition, normalizer, herbs, units)
        assert len(issues) == 1
        assert issues[0].severity == Severity.BLOCKER

    def test_no_max_dose_skip(self):
        """DO-4: 无 max_dose 的药"""
        herbs = {_make_herb("桑寄生", max_dose=None).name: _make_herb("桑寄生", max_dose=None)}
        normalizer = _build_normalizer(list(herbs.values()))
        composition = [HerbDose(herb="桑寄生", dose=15, unit="g")]
        units = _build_unit_dict([_make_unit("g", to_grams=1.0)])
        issues = _check_dose_limits(composition, normalizer, herbs, units)
        assert len(issues) == 0

    def test_qian_conversion_double_high(self):
        """DO-6: 钱→g 换算后恰好 2 倍超量"""
        herbs = {_make_herb("细辛", max_dose=3).name: _make_herb("细辛", max_dose=3)}
        normalizer = _build_normalizer(list(herbs.values()))
        composition = [HerbDose(herb="细辛", dose=6, unit="g")]  # 已换算
        units = _build_unit_dict([_make_unit("g", to_grams=1.0)])
        issues = _check_dose_limits(composition, normalizer, herbs, units)
        assert len(issues) == 1
        assert issues[0].severity == Severity.HIGH

    def test_liang_conversion_normal(self):
        """DO-7: 两→g 换算后正常"""
        herbs = {_make_herb("石膏", max_dose=60).name: _make_herb("石膏", max_dose=60)}
        normalizer = _build_normalizer(list(herbs.values()))
        composition = [HerbDose(herb="石膏", dose=30, unit="g")]  # 1两=30g
        units = _build_unit_dict([_make_unit("g", to_grams=1.0)])
        issues = _check_dose_limits(composition, normalizer, herbs, units)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# 7. 过敏检查
# ---------------------------------------------------------------------------


class TestAllergy:
    def test_allergy_hit(self):
        """AL-1: 过敏命中"""
        herbs = {_make_herb("甘草").name: _make_herb("甘草")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(allergies=["甘草"])
        issues = _check_allergy(["甘草"], patient.allergies, normalizer, herbs)
        assert len(issues) == 1
        assert issues[0].severity == Severity.BLOCKER
        assert issues[0].type == SafetyIssueType.ALLERGY

    def test_alias_match_hit(self):
        """AL-2: 别名匹配命中"""
        herbs = {_make_herb("甘草", aliases=["国老"]).name: _make_herb("甘草", aliases=["国老"])}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(allergies=["甘草"])
        issues = _check_allergy(["国老"], patient.allergies, normalizer, herbs)
        assert len(issues) == 1

    def test_multi_allergy_hit(self):
        """AL-3: 多过敏命中"""
        herbs = {
            _make_herb("甘草").name: _make_herb("甘草"),
            _make_herb("当归").name: _make_herb("当归"),
        }
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(allergies=["甘草", "当归"])
        issues = _check_allergy(
            ["甘草", "当归"], patient.allergies, normalizer, herbs
        )
        assert len(issues) == 2

    def test_no_allergy_no_trigger(self):
        """AL-4: 无过敏不触发"""
        herbs = {_make_herb("甘草").name: _make_herb("甘草")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(allergies=[])
        issues = _check_allergy(["甘草"], patient.allergies, normalizer, herbs)
        assert len(issues) == 0

    def test_different_herb_no_hit(self):
        """AL-5: 不同药不命中"""
        herbs = {_make_herb("甘草").name: _make_herb("甘草")}
        normalizer = _build_normalizer(list(herbs.values()))
        patient = _make_patient(allergies=["麻黄"])
        issues = _check_allergy(["甘草"], patient.allergies, normalizer, herbs)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# 8. 配伍禁忌
# ---------------------------------------------------------------------------


class TestCombinationIncompatibilities:
    def test_structured_incompat_hit(self):
        """CO-1: 结构化禁忌命中"""
        herbs = {
            _make_herb(
                "肉桂",
                incompatibilities=[{"herb": "石膏", "reason": "温热与寒凉冲突", "source": "《中药配伍禁忌表》"}],
            ).name: _make_herb(
                "肉桂",
                incompatibilities=[{"herb": "石膏", "reason": "温热与寒凉冲突", "source": "《中药配伍禁忌表》"}],
            ),
            _make_herb("石膏", incompatibilities=[]).name: _make_herb("石膏", incompatibilities=[]),
        }
        normalizer = _build_normalizer(list(herbs.values()))
        issues = _check_combination_incompatibilities(
            ["肉桂", "石膏"], herbs, normalizer, [], []
        )
        assert len(issues) == 1
        assert issues[0].type == SafetyIssueType.COMBINATION
        assert issues[0].severity == Severity.BLOCKER

    def test_no_incompat_data_no_trigger(self):
        """CO-2: 无禁忌数据不触发"""
        herbs = {
            _make_herb("肉桂", incompatibilities=None).name: _make_herb("肉桂", incompatibilities=None),
            _make_herb("石膏", incompatibilities=None).name: _make_herb("石膏", incompatibilities=None),
        }
        normalizer = _build_normalizer(list(herbs.values()))
        issues = _check_combination_incompatibilities(
            ["肉桂", "石膏"], herbs, normalizer, [], []
        )
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# 9. 去重与排序
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_same_herb_pair_keeps_higher_severity(self):
        issues = [
            SafetyIssue(
                type=SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES,
                severity=Severity.BLOCKER,
                herbs=["甘草", "海藻"],
                rule_source="十八反",
                suggestion="不可同用",
            ),
            SafetyIssue(
                type=SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES,
                severity=Severity.HIGH,
                herbs=["海藻", "甘草"],
                rule_source="十八反",
                suggestion="不可同用",
            ),
        ]
        deduped = _deduplicate(issues)
        assert len(deduped) == 1
        assert deduped[0].severity == Severity.BLOCKER

    def test_different_types_kept(self):
        issues = [
            SafetyIssue(
                type=SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES,
                severity=Severity.BLOCKER,
                herbs=["甘草", "海藻"],
                rule_source="十八反",
                suggestion="不可同用",
            ),
            SafetyIssue(
                type=SafetyIssueType.PREGNANCY,
                severity=Severity.BLOCKER,
                herbs=["海藻"],
                rule_source="妊娠禁忌",
                suggestion="妊娠禁用",
            ),
        ]
        deduped = _deduplicate(issues)
        assert len(deduped) == 2


class TestSortIssues:
    def test_blocker_before_high(self):
        issues = [
            SafetyIssue(
                type=SafetyIssueType.DOSE_LIMIT,
                severity=Severity.HIGH,
                herbs=["川芎"],
                rule_source="剂量上限",
                suggestion="超量",
            ),
            SafetyIssue(
                type=SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES,
                severity=Severity.BLOCKER,
                herbs=["甘草", "海藻"],
                rule_source="十八反",
                suggestion="不可同用",
            ),
        ]
        sorted_issues = _sort_issues(issues)
        assert sorted_issues[0].severity == Severity.BLOCKER
        assert sorted_issues[1].severity == Severity.HIGH


class TestDeterminePassed:
    def test_passed_with_no_blocker_or_high(self):
        issues = [
            SafetyIssue(
                type=SafetyIssueType.CAUTION,
                severity=Severity.WARNING,
                herbs=[],
                rule_source="test",
                suggestion="test",
            ),
        ]
        assert _determine_passed(issues) is True

    def test_not_passed_with_blocker(self):
        issues = [
            SafetyIssue(
                type=SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES,
                severity=Severity.BLOCKER,
                herbs=["甘草", "海藻"],
                rule_source="十八反",
                suggestion="不可同用",
            ),
        ]
        assert _determine_passed(issues) is False

    def test_not_passed_with_high(self):
        issues = [
            SafetyIssue(
                type=SafetyIssueType.DOSE_LIMIT,
                severity=Severity.HIGH,
                herbs=["川芎"],
                rule_source="剂量上限",
                suggestion="超量",
            ),
        ]
        assert _determine_passed(issues) is False

    def test_passed_with_empty(self):
        assert _determine_passed([]) is True


# ---------------------------------------------------------------------------
# 10. 集成测试（多规则同时命中）
# ---------------------------------------------------------------------------


class TestMultiRuleIntegration:
    def test_multi_rule_hit(self):
        """IT-1: 多规则同时命中"""
        normalizer = _build_normalizer(
            [_make_herb("甘草"), _make_herb("海藻")]
        )
        issues: list[SafetyIssue] = []
        issues.extend(
            _check_eighteen_incompatibilities(["甘草", "海藻"], normalizer)
        )
        issues = _deduplicate(issues)
        issues = _sort_issues(issues)
        assert len(issues) >= 1
        # 十八反命中
        assert any(
            i.type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES for i in issues
        )

    def test_all_pass(self):
        """IT-2: 完全通过"""
        herbs = [
            _make_herb("党参", max_dose=30),
            _make_herb("白术", max_dose=15),
            _make_herb("茯苓", max_dose=30),
            _make_herb("甘草", max_dose=10),
        ]
        normalizer = _build_normalizer(herbs)
        herb_dict = _build_herb_dict(herbs)
        patient = _make_patient()

        issues: list[SafetyIssue] = []
        issues.extend(_check_eighteen_incompatibilities(["党参", "白术", "茯苓", "甘草"], normalizer))
        issues.extend(_check_nineteen_fears(["党参", "白术", "茯苓", "甘草"], normalizer))
        issues.extend(_check_pregnancy(["党参", "白术", "茯苓", "甘草"], patient, herb_dict, normalizer))
        issues = _deduplicate(issues)
        assert len(issues) == 0
        assert _determine_passed(issues) is True

    def test_empty_formula(self):
        """IT-4: 空处方"""
        normalizer = _build_normalizer([])
        issues = _check_eighteen_incompatibilities([], normalizer)
        assert len(issues) == 0

    def test_single_herb(self):
        """IT-5: 单味药"""
        normalizer = _build_normalizer([_make_herb("人参")])
        issues = _check_eighteen_incompatibilities(["人参"], normalizer)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# 11. 数据集验证
# ---------------------------------------------------------------------------


class TestDatasets:
    def test_eighteen_table_not_empty(self):
        assert len(EIGHTEEN_INCOMPATIBILITY_TABLE) > 0

    def test_nineteen_table_not_empty(self):
        assert len(NINETEEN_FEARS_TABLE) > 0

    def test_expand_aconitum(self):
        g = expand_group("川乌")
        assert "附子" in g
        assert "草乌" in g

    def test_expand_unknown(self):
        g = expand_group("未知药")
        assert g == frozenset({"未知药"})

    def test_is_toxic_aconitum(self):
        assert is_toxic_or_strong_herb("附子") is True

    def test_is_not_toxic_normal(self):
        assert is_toxic_or_strong_herb("党参") is False


# ---------------------------------------------------------------------------
# 12. SafetyRuleResult schema
# ---------------------------------------------------------------------------


class TestSafetyRuleResultSchema:
    def test_has_rule_version(self):
        result = SafetyRuleResult(
            passed=True,
            issues=[],
            normalized_formula=_make_formula("test", [HerbDose(herb="党参", dose=10, unit="g")]),
        )
        assert result.rule_version == SAFETY_RULE_VERSION

    def test_has_execution_order(self):
        result = SafetyRuleResult(
            passed=True,
            issues=[],
            normalized_formula=_make_formula("test", [HerbDose(herb="党参", dose=10, unit="g")]),
            execution_order=["normalize", "eighteen_incompatibilities"],
        )
        assert result.execution_order == ["normalize", "eighteen_incompatibilities"]


# ---------------------------------------------------------------------------
# 13. Engine 集成测试（需 PostgreSQL）
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
class TestSafetyRuleEngineIntegration:
    @pytest_asyncio.fixture(loop_scope="module")
    async def db(self) -> AsyncSession:
        """提供集成测试数据库会话。"""
        from app.db.session import get_session_factory, reset_session_factory

        await reset_session_factory()
        factory = get_session_factory()
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}")

        async with factory() as session:
            yield session

    async def _setup_db(self, db: AsyncSession):
        """插入测试 herbs 和 dosage_units 种子数据（幂等：跳过已存在的）。"""
        from sqlalchemy import select as sa_select

        from app.models.consult import ConsultSession
        from app.models.knowledge import DosageUnit, Herb

        # 插入测试会话（safety_rule_runs 需 FK）
        self._test_session = ConsultSession(
            id=uuid.uuid4(),
            patient_ref="P6-3-ENGINE-TEST",
            patient_info={"gender": "unknown"},
            current_stage="safety",
            status="active",
            state_version=1,
            rollback_counts={},
        )
        db.add(self._test_session)

        # 仅插入不存在的 herbs（幂等）
        existing_herbs = set()
        result = await db.execute(sa_select(Herb.name))
        for row in result.all():
            existing_herbs.add(row[0])

        test_herbs = [
            {
                "name": "党参",
                "aliases": ["潞党参"],
                "max_dose": 30.0,
                "pregnancy_contraindication": "none",
                "doc_text": "党参 补中益气",
            },
            {
                "name": "白术",
                "aliases": ["于术"],
                "max_dose": 15.0,
                "pregnancy_contraindication": "none",
                "doc_text": "白术 补气健脾",
            },
            {
                "name": "甘草",
                "aliases": ["国老"],
                "max_dose": 10.0,
                "pregnancy_contraindication": "none",
                "doc_text": "甘草 调和诸药",
            },
            {
                "name": "海藻",
                "aliases": [],
                "max_dose": 15.0,
                "pregnancy_contraindication": "none",
                "doc_text": "海藻 软坚散结",
            },
            {
                "name": "半夏",
                "aliases": ["法半夏", "姜半夏"],
                "max_dose": 9.0,
                "pregnancy_contraindication": "caution",
                "doc_text": "半夏 燥湿化痰",
            },
            {
                "name": "附子",
                "aliases": [],
                "max_dose": 15.0,
                "pregnancy_contraindication": "caution",
                "doc_text": "附子 回阳救逆",
            },
            {
                "name": "莪术",
                "aliases": [],
                "max_dose": 9.0,
                "pregnancy_contraindication": "forbidden",
                "doc_text": "莪术 破血行气",
            },
            {
                "name": "川芎",
                "aliases": [],
                "max_dose": 10.0,
                "pregnancy_contraindication": "none",
                "doc_text": "川芎 活血行气",
            },
        ]
        for h in test_herbs:
            if h["name"] not in existing_herbs:
                db.add(Herb(**h))

        # 插入测试 dosage_units（幂等：跳过已存在的）
        existing_units = set()
        result = await db.execute(sa_select(DosageUnit.unit_name))
        for row in result.all():
            existing_units.add(row[0])

        test_units = [
            {
                "unit_name": "g",
                "aliases": ["克"],
                "to_grams": 1.0,
                "conversion_type": "standard",
                "is_standard": True,
                "enabled": True,
            },
            {
                "unit_name": "两",
                "aliases": ["市两"],
                "to_grams": 30.0,
                "conversion_type": "fixed",
                "is_standard": False,
                "enabled": True,
            },
            {
                "unit_name": "钱",
                "aliases": ["市钱"],
                "to_grams": 3.0,
                "conversion_type": "fixed",
                "is_standard": False,
                "enabled": True,
            },
            {
                "unit_name": "枚",
                "aliases": ["个"],
                "to_grams": None,
                "conversion_type": "herb_specific",
                "is_standard": False,
                "enabled": True,
            },
            {
                "unit_name": "适量",
                "aliases": ["少许"],
                "to_grams": None,
                "conversion_type": "unsupported",
                "is_standard": False,
                "enabled": True,
            },
        ]
        for u in test_units:
            if u["unit_name"] not in existing_units:
                db.add(DosageUnit(**u))
        await db.commit()

    async def _cleanup_db(self, db: AsyncSession):
        """清理本测试产生的 safety_rule_runs 和测试会话（herbs/dosage_units 复用种子数据）。"""

        await db.execute(text("DELETE FROM safety_rule_runs"))
        if getattr(self, "_test_session", None) is not None:
            await db.execute(
                text("DELETE FROM consult_sessions WHERE id = :sid"),
                {"sid": str(self._test_session.id)},
            )
        await db.commit()

    async def test_check_safe_prescription(self, db: AsyncSession):
        """安全处方通过规则引擎。"""
        await self._setup_db(db)
        try:
            from app.safety.engine import SafetyRuleEngine

            engine = SafetyRuleEngine(db)
            formula = FormulaResult(
                name="四君子汤",
                composition=[
                    HerbDose(herb="党参", dose=12, unit="g"),
                    HerbDose(herb="白术", dose=10, unit="g"),
                ],
                rationale="健脾益气",
            )
            patient = _make_patient()
            result = await engine.check(
                formula=formula,
                patient_info=patient,
                session_id=str(self._test_session.id),
                trace_id="test-trace",
            )
            assert result.passed is True
            assert result.rule_version == SAFETY_RULE_VERSION
            assert len(result.execution_order) > 0
            assert "normalize" in result.execution_order
        finally:
            await self._cleanup_db(db)

    async def test_check_eighteen_incompat_blocked(self, db: AsyncSession):
        """十八反处方被阻断。"""
        await self._setup_db(db)
        try:
            from app.safety.engine import SafetyRuleEngine

            engine = SafetyRuleEngine(db)
            formula = FormulaResult(
                name="违规方",
                composition=[
                    HerbDose(herb="甘草", dose=6, unit="g"),
                    HerbDose(herb="海藻", dose=10, unit="g"),
                ],
                rationale="测试十八反",
            )
            patient = _make_patient()
            result = await engine.check(
                formula=formula,
                patient_info=patient,
                session_id=str(self._test_session.id),
                trace_id="test-trace-18",
            )
            assert result.passed is False
            assert any(
                i.type == SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES
                for i in result.issues
            )
        finally:
            await self._cleanup_db(db)

    async def test_writes_safety_rule_runs(self, db: AsyncSession):
        """规则引擎写入 safety_rule_runs 表。"""
        await self._setup_db(db)
        try:
            from sqlalchemy import select

            from app.models.safety import SafetyRuleRun
            from app.safety.engine import SafetyRuleEngine

            engine = SafetyRuleEngine(db)
            formula = FormulaResult(
                name="四君子汤",
                composition=[HerbDose(herb="党参", dose=12, unit="g")],
                rationale="健脾益气",
            )
            patient = _make_patient()
            sid = str(self._test_session.id)
            await engine.check(
                formula=formula,
                patient_info=patient,
                session_id=sid,
                trace_id="test-trace-runs",
            )
            await db.commit()

            result = await db.execute(
                select(SafetyRuleRun).where(
                    SafetyRuleRun.session_id == uuid.UUID(sid)
                )
            )
            run = result.scalar_one_or_none()
            assert run is not None
            assert run.rule_version == SAFETY_RULE_VERSION
            assert run.passed is True
        finally:
            await self._cleanup_db(db)
