"""确定性安全规则引擎。

编排全部安全规则检查，输出 ``SafetyRuleResult``，写入 ``safety_rule_runs``。
不依赖 LLM，所有规则为纯函数式检查。

执行顺序（与设计文档 §10.1 一致）：
1. 药名标准化
2. 剂量单位换算
3. 十八反检查
4. 十九畏检查
5. 妊娠禁忌检查
6. 配伍禁忌检查
7. 剂量上限检查
8. 过敏检查
9. 药材禁忌检查（患者条件 × Herb.contraindications 精确匹配，R4-A）
10. 用药相互作用覆盖率门禁（R4-A）
11. 去重 + 排序
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import observe_safety_outcome
from app.models.knowledge import DosageUnit, Herb
from app.models.safety import SafetyRuleRun
from app.safety.datasets import (
    EIGHTEEN_INCOMPATIBILITY_TABLE,
    NINETEEN_FEARS_TABLE,
    expand_group,
    is_toxic_or_strong_herb,
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
from app.schemas.types import SafetyIssueType, Severity, is_pregnancy_risk_status

logger = logging.getLogger("xuanhu.safety")

# ---------------------------------------------------------------------------
# 辅助：内存中的 Herb/ DosageUnit 轻量包装
# ---------------------------------------------------------------------------


class _HerbAliasAdapter:
    """将 Herb ORM 对象适配为 HerbAliasProvider。"""

    __slots__ = ("_herb",)

    def __init__(self, herb: Herb) -> None:
        self._herb = herb

    @property
    def standard_name(self) -> str:
        return self._herb.name

    @property
    def aliases(self) -> list[str]:
        raw = self._herb.aliases or []
        return [a for a in raw if isinstance(a, str)]


# ---------------------------------------------------------------------------
# R4-A 患者特定安全检查：有界归一化常量
# ---------------------------------------------------------------------------

#: 对抗性增长防护：患者条件/药物条目数与字符串长度上限。
_MAX_PATIENT_CONDITION_ITEMS = 200
_MAX_PATIENT_MEDICATION_ITEMS = 200
_MAX_TEXT_LENGTH = 512
#: 单味药 contraindications 条目数上限。
_MAX_HERB_CONTRAINDICATION_ENTRIES = 200

#: 严格 dict 禁忌条目中承载条件/名称的固定键集（按优先级取首个可接受的字符串）。
_CONTRAINDICATION_DICT_KEYS = ("condition", "name")

_WHITESPACE_RE = re.compile(r"\s+")

#: 固定权威规则来源（不随数据变化，保证审计可复现）。
_HERB_CONTRAINDICATION_RULE_SOURCE = "Herb.contraindications"
_MEDICATION_INTERACTION_RULE_SOURCE = "medication_interaction_coverage"
# R4-B：fail-closed 覆盖率门禁同样使用固定来源，不随数据变化。
_PATIENT_CONTEXT_COVERAGE_RULE_SOURCE = "patient_context_coverage"
_HERB_CONTRAINDICATION_COVERAGE_RULE_SOURCE = "herb_contraindication_coverage"

#: 本仓库当前没有权威中药-西药相互作用数据表；若将来引入该数据，置 True 关闭覆盖率门禁。
_HAS_AUTHORITATIVE_HERB_DRUG_INTERACTION_DATA = False


# ---------------------------------------------------------------------------
# 核心引擎
# ---------------------------------------------------------------------------


class SafetyRuleEngine:
    """确定性安全规则引擎。

    不依赖 LLM，不调用 Safety Agent。每次调用 ``check()`` 从数据库加载
    herbs 与 dosage_units 数据，执行全部规则后返回 ``SafetyRuleResult``，
    同时写入 ``safety_rule_runs`` 表（不可变记录）。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def check(
        self,
        formula: FormulaResult,
        patient_info: PatientInfo,
        *,
        session_id: str,
        trace_id: str,
        formula_source: str = "agent_output",
        agent_run_id: str | None = None,
    ) -> SafetyRuleResult:
        """执行全部安全规则检查。

        Args:
            formula: 待审核处方（已标准化之前的原始处方）。
            patient_info: 患者信息（含过敏史、妊娠状态）。
            session_id: 会话 ID。
            trace_id: 追踪 ID。
            formula_source: 处方来源，``agent_output`` 或 ``doctor_override``。
            agent_run_id: 关联的 Agent 运行 ID（可选）。

        Returns:
            SafetyRuleResult，包含 ``passed`` / ``issues`` / ``warnings`` /
            ``normalized_formula`` / ``rule_version`` / ``execution_order``。
        """
        # check() is the authoritative safety gate — always observe the metric.
        result = await self.evaluate(formula, patient_info, observe_metric=True)
        await self._write_safety_rule_run(
            session_id=session_id,
            trace_id=trace_id,
            result=result,
            formula=formula,
            patient_info=patient_info,
            formula_source=formula_source,
            agent_run_id=agent_run_id,
        )

        return result

    async def evaluate(
        self,
        formula: FormulaResult,
        patient_info: PatientInfo,
        *,
        observe_metric: bool = False,
    ) -> SafetyRuleResult:
        """Evaluate every deterministic rule without writing a run row.

        Product LangGraph flows use this pure persistence boundary so the
        result, Domain artifact, gate, compatibility projection and session
        transition can be committed atomically by the Domain Repository.
        Legacy ``check()`` keeps its existing evaluate-and-persist behaviour.

        R5: the bounded safety pass/block metric is opt-in at the authoritative
        boundary.  ``evaluate()`` defaults to ``observe_metric=False`` so
        pure/advisory evaluation does not count; callers that own the
        authoritative decision (``check()`` and the review gate) pass
        ``observe_metric=True`` explicitly.  This never alters the result or
        any transaction.
        """

        execution_order: list[str] = []
        herb_records = await self._load_herb_records()
        unit_table = await self._load_unit_table()
        normalizer = HerbNormalizer(_HerbAliasAdapter(h) for h in herb_records.values())

        execution_order.append("normalize")
        normalized = _normalize_composition(formula.composition, normalizer)
        execution_order.append("convert_dose")
        unit_issues, unit_warnings, converted = _convert_all_doses(
            normalized, unit_table, herb_records, normalizer
        )
        all_issues: list[SafetyIssue] = list(unit_issues)
        all_warnings: list[str] = list(unit_warnings)
        normalized_formula = FormulaResult(
            name=formula.name,
            composition=converted,
            source=formula.source,
            rationale=formula.rationale,
            citations=formula.citations,
        )
        herb_names = [item.herb for item in converted]

        execution_order.append("unknown_herb")
        all_issues.extend(_check_unknown_herbs(herb_names, herb_records, normalizer))
        execution_order.append("eighteen_incompatibilities")
        eighteen_issues = _check_eighteen_incompatibilities(herb_names, normalizer)
        all_issues.extend(eighteen_issues)
        execution_order.append("nineteen_fears")
        nineteen_issues = _check_nineteen_fears(herb_names, normalizer)
        all_issues.extend(nineteen_issues)
        execution_order.append("pregnancy")
        all_issues.extend(_check_pregnancy(herb_names, patient_info, herb_records, normalizer))
        execution_order.append("combination")
        all_issues.extend(
            _check_combination_incompatibilities(
                herb_names,
                herb_records,
                normalizer,
                eighteen_issues,
                nineteen_issues,
            )
        )
        execution_order.append("dose_limit")
        all_issues.extend(_check_dose_limits(converted, normalizer, herb_records, unit_table))
        execution_order.append("allergy")
        all_issues.extend(_check_allergy(herb_names, patient_info.allergies, normalizer, herb_records))
        execution_order.append("herb_contraindication")
        all_issues.extend(
            _check_herb_contraindications(herb_names, patient_info, herb_records, normalizer)
        )
        execution_order.append("medication_interaction_coverage")
        all_issues.extend(_check_medication_interaction_coverage(patient_info))
        execution_order.extend(("deduplicate", "sort"))
        all_issues = _sort_issues(_deduplicate(all_issues))

        passed = _determine_passed(all_issues)
        if observe_metric:
            # Observation must never alter the safety decision: a metric
            # failure is swallowed so the authoritative result still returns.
            try:
                observe_safety_outcome(passed)
            except Exception:  # noqa: BLE001 - metrics must never mask the safety result
                logger.warning("safety outcome metric observation failed")
        return SafetyRuleResult(
            passed=passed,
            issues=all_issues,
            normalized_formula=normalized_formula,
            warnings=all_warnings,
            rule_version=SAFETY_RULE_VERSION,
            execution_order=execution_order,
        )

    async def persist_result(
        self,
        *,
        session_id: str,
        trace_id: str,
        result: SafetyRuleResult,
        formula: FormulaResult,
        patient_info: PatientInfo,
        formula_source: str = "agent_output",
        agent_run_id: str | None = None,
    ) -> None:
        """将已构造的 SafetyRuleResult 写入 safety_rule_runs。

        用于无法走完整 ``check()`` 流程但仍需留痕的场景
        （如处方缺失的 blocked 路径）。
        """
        await self._write_safety_rule_run(
            session_id=session_id,
            trace_id=trace_id,
            result=result,
            formula=formula,
            patient_info=patient_info,
            formula_source=formula_source,
            agent_run_id=agent_run_id,
        )

    # ------------------------------------------------------------------
    # 数据库加载
    # ------------------------------------------------------------------

    async def _load_herb_records(self) -> dict[str, Herb]:
        result = await self._db.execute(
            select(Herb)
            .where(Herb.deleted_at.is_(None))
            .order_by(Herb.name, Herb.id)
        )
        return {herb.name: herb for herb in result.scalars().all()}

    async def _load_unit_table(self) -> dict[str, DosageUnit]:
        result = await self._db.execute(
            select(DosageUnit)
            .where(DosageUnit.enabled.is_(True))
            .order_by(DosageUnit.unit_name, DosageUnit.id)
        )
        units = result.scalars().all()
        table = {unit.unit_name: unit for unit in units}
        alias_candidates: dict[str, dict[str, DosageUnit]] = {}
        for du in units:
            for alias in (du.aliases or []):
                if isinstance(alias, str) and alias:
                    alias_candidates.setdefault(alias, {})[du.unit_name] = du
        for alias, candidates in alias_candidates.items():
            if alias not in table and len(candidates) == 1:
                table[alias] = next(iter(candidates.values()))
        return table

    # ------------------------------------------------------------------
    # 写入 safety_rule_runs
    # ------------------------------------------------------------------

    async def _write_safety_rule_run(
        self,
        *,
        session_id: str,
        trace_id: str,
        result: SafetyRuleResult,
        formula: FormulaResult,
        patient_info: PatientInfo,
        formula_source: str,
        agent_run_id: str | None,
    ) -> None:
        run = SafetyRuleRun(
            session_id=uuid.UUID(session_id),
            agent_run_id=uuid.UUID(agent_run_id) if agent_run_id else None,
            formula_source=formula_source,
            passed=result.passed,
            issues=[i.model_dump(mode="json") for i in result.issues],
            formula_snapshot=formula.model_dump(mode="json"),
            normalized_formula=result.normalized_formula.model_dump(mode="json"),
            patient_snapshot=patient_info.model_dump(mode="json", exclude={"name"}),
            rule_version=result.rule_version,
            trace_id=trace_id,
        )
        self._db.add(run)
        await self._db.flush()


# ---------------------------------------------------------------------------
# 规则函数（模块级纯函数，便于单测）
# ---------------------------------------------------------------------------


def _normalize_composition(
    composition: list[HerbDose],
    normalizer: HerbNormalizer,
) -> list[HerbDose]:
    """标准化处方中所有药名。"""
    return [
        HerbDose(
            herb=normalizer.normalize(h.herb),
            dose=h.dose,
            unit=h.unit,
            note=h.note,
        )
        for h in composition
    ]


def _convert_dose(
    herb_name: str,
    dose: float,
    unit: str,
    unit_table: dict[str, DosageUnit],
    herb_records: dict[str, Herb],
    normalizer: HerbNormalizer,
) -> tuple[float | None, SafetyIssue | None, str | None]:
    """将单味药剂量转换为 g。

    Returns:
        (dose_in_grams, issue, warning)。
        - 成功时 issue 和 warning 均为 None。
        - 无法换算时 dose_in_grams 为 None，issue 或 warning 非空。
    """
    unit_entry = unit_table.get(unit)
    std_name = normalizer.normalize(herb_name)

    if unit_entry is None:
        # 单位未在表中
        if is_toxic_or_strong_herb(std_name):
            issue = SafetyIssue(
                type=SafetyIssueType.UNIT_CONVERSION,
                severity=Severity.HIGH,
                herbs=[std_name],
                rule_source="单位换算",
                suggestion=(
                    f"「{std_name}」的单位「{unit}」无法识别，"
                    "且该药为毒性/峻烈药材，请确认剂量。"
                ),
            )
            return None, issue, None
        issue = SafetyIssue(
            type=SafetyIssueType.UNIT_CONVERSION,
            severity=Severity.WARNING,
            herbs=[std_name],
            rule_source="单位换算",
            suggestion=(
                f"单位「{unit}」无法识别，无法自动审核「{std_name}」的剂量。"
                "请医师自行判断。"
            ),
        )
        return None, issue, None

    ct = unit_entry.conversion_type

    if ct in ("standard", "fixed"):
        if unit_entry.to_grams is None:
            return None, None, f"单位「{unit}」的换算系数缺失，无法审核「{std_name}」的剂量。"
        return dose * float(unit_entry.to_grams), None, None

    if ct == "herb_specific":
        issue = SafetyIssue(
            type=SafetyIssueType.UNIT_CONVERSION,
            severity=Severity.WARNING,
            herbs=[std_name],
            rule_source="单位换算",
            suggestion=(
                f"「{std_name}」的单位「{unit}」为药材特异性单位，"
                "无法自动换算，请医师自行判断剂量。"
            ),
        )
        return None, issue, None

    if ct == "unsupported":
        issue = SafetyIssue(
            type=SafetyIssueType.UNIT_CONVERSION,
            severity=Severity.WARNING,
            herbs=[std_name],
            rule_source="单位换算",
            suggestion=f"单位「{unit}」目前不支持自动换算，无法自动审核「{std_name}」的剂量。",
        )
        return None, issue, None

    return None, None, f"未知换算类型: {ct}"


def _convert_all_doses(
    composition: list[HerbDose],
    unit_table: dict[str, DosageUnit],
    herb_records: dict[str, Herb],
    normalizer: HerbNormalizer,
) -> tuple[list[SafetyIssue], list[str], list[HerbDose]]:
    """批量转换处方中所有药味剂量。

    Returns:
        (issues, warnings, converted_composition)。
        converted_composition 中无法换算的剂量 dose=None，保留原单位。
    """
    issues: list[SafetyIssue] = []
    warnings: list[str] = []
    converted: list[HerbDose] = []

    for h in composition:
        if h.dose is None:
            warnings.append(f"「{h.herb}」缺少剂量值，无法审核。")
            converted.append(h)
            continue

        dose_g, issue, warning = _convert_dose(
            h.herb, h.dose, h.unit, unit_table, herb_records, normalizer
        )
        if issue is not None:
            issues.append(issue)
        if warning is not None:
            warnings.append(warning)
        converted.append(
            HerbDose(
                herb=h.herb,
                dose=dose_g,
                unit="g" if dose_g is not None else h.unit,
                note=h.note,
            )
        )

    return issues, warnings, converted


# ---------------------------------------------------------------------------
# 十八反
# ---------------------------------------------------------------------------


def _check_eighteen_incompatibilities(
    herbs: list[str],
    normalizer: HerbNormalizer,
) -> list[SafetyIssue]:
    """检查十八反。对每对 (A, B) 查询硬编码表，命中则生成 blocker。

    采用"任一药属于展开组即视为该组全部药在场"的保守策略
    （§4.3 乌头类展开）。这会带来跨组连边（如川乌-半夏 与 附子-半夏），
    通过 (type, sorted_herbs) 去重收敛为对每个不同药对一条 issue。
    """
    issues: list[SafetyIssue] = []
    if len(herbs) < 2:
        return issues

    seen: set[frozenset[str]] = set()

    # 标准化药名并去重，保留出现顺序
    normalized_herbs: list[str] = []
    seen_normalized: set[str] = set()
    for h in herbs:
        std = normalizer.normalize(h)
        if std not in seen_normalized:
            seen_normalized.add(std)
            normalized_herbs.append(std)

    for i in range(len(normalized_herbs)):
        a = normalized_herbs[i]
        # a 展开后的组内候选（含 a 自身）
        a_group = expand_group(a)
        for j in range(i + 1, len(normalized_herbs)):
            b = normalized_herbs[j]
            b_group = expand_group(b)
            # 笛卡尔积检查组内成员之间的反药关系
            for ga in a_group:
                for gb in b_group:
                    if ga == gb:
                        continue
                    source = EIGHTEEN_INCOMPATIBILITY_TABLE.get((ga, gb)) or \
                        EIGHTEEN_INCOMPATIBILITY_TABLE.get((gb, ga))
                    if source is None:
                        continue
                    # 以原始两味药（a, b）作为 issue 的 herbs，避免组内
                    # 其他成员污染；同时作为去重键。
                    pair = frozenset([a, b])
                    if pair in seen:
                        continue
                    seen.add(pair)
                    issues.append(
                        SafetyIssue(
                            type=SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES,
                            severity=Severity.BLOCKER,
                            herbs=sorted([a, b]),
                            rule_source=source,
                            suggestion=(
                                f"「{a}」与「{b}」属十八反配伍禁忌"
                                f"（{source}），不可同方使用。"
                                "建议移除其中一味或替换。"
                            ),
                        )
                    )
    return issues


# ---------------------------------------------------------------------------
# 十九畏
# ---------------------------------------------------------------------------


def _check_nineteen_fears(
    herbs: list[str],
    normalizer: HerbNormalizer,
) -> list[SafetyIssue]:
    """检查十九畏。对每对 (A, B) 查询硬编码表，命中则生成 blocker。"""
    issues: list[SafetyIssue] = []
    if len(herbs) < 2:
        return issues

    seen: set[frozenset[str]] = set()

    normalized_herbs: list[str] = []
    seen_normalized: set[str] = set()
    for h in herbs:
        std = normalizer.normalize(h)
        if std not in seen_normalized:
            seen_normalized.add(std)
            normalized_herbs.append(std)

    for i in range(len(normalized_herbs)):
        a = normalized_herbs[i]
        a_group = expand_group(a)
        for j in range(i + 1, len(normalized_herbs)):
            b = normalized_herbs[j]
            b_group = expand_group(b)
            for ga in a_group:
                for gb in b_group:
                    if ga == gb:
                        continue
                    source = NINETEEN_FEARS_TABLE.get((ga, gb)) or \
                        NINETEEN_FEARS_TABLE.get((gb, ga))
                    if source is None:
                        continue
                    pair = frozenset([a, b])
                    if pair in seen:
                        continue
                    seen.add(pair)
                    issues.append(
                        SafetyIssue(
                            type=SafetyIssueType.NINETEEN_FEARS,
                            severity=Severity.BLOCKER,
                            herbs=sorted([a, b]),
                            rule_source=source,
                            suggestion=(
                                f"「{a}」与「{b}」属十九畏配伍禁忌"
                                f"（{source}），不可同方使用。"
                                "建议移除其中一味或替换。"
                            ),
                        )
                    )
    return issues


# ---------------------------------------------------------------------------
# 妊娠禁忌
# ---------------------------------------------------------------------------


def _check_pregnancy(
    herbs: list[str],
    patient_info: PatientInfo,
    herb_records: dict[str, Herb],
    normalizer: HerbNormalizer,
) -> list[SafetyIssue]:
    """检查妊娠禁忌。

    仅当 pregnancy_status 为 ``pregnant`` 或 ``possible`` 时触发硬规则检查；
    ``unknown`` / ``null`` 生成 caution warning。
    2026-08：男性患者妊娠不适用——跳过未知警告（真实会话 f6a5ffb7 复盘的误报）。
    """
    if getattr(patient_info, "gender", None) == "male":
        return []
    status = patient_info.pregnancy_status
    status_str = status.value if hasattr(status, "value") else str(status) if status else "unknown"

    if status_str in ("unknown", "null", "") or status is None:
        return [
            SafetyIssue(
                type=SafetyIssueType.CAUTION,
                severity=Severity.WARNING,
                herbs=[],
                rule_source="PatientInfo.pregnancy_status",
                suggestion=(
                    "患者妊娠状态未确认。请医师在确认处方前补充或核实妊娠状态。"
                ),
            )
        ]
    if status_str in ("no", "lactating"):
        return []

    if not is_pregnancy_risk_status(status_str):
        return []

    # pregnant 或 possible 均按严格标准执行
    issues: list[SafetyIssue] = []
    seen: set[str] = set()

    for herb_name in herbs:
        std = normalizer.normalize(herb_name)
        if std in seen:
            continue
        seen.add(std)
        record = herb_records.get(std)
        if record is None:
            continue
        level = record.pregnancy_contraindication
        level = level.strip().lower() if isinstance(level, str) else "none"

        if level == "forbidden":
            issues.append(
                SafetyIssue(
                    type=SafetyIssueType.PREGNANCY,
                    severity=Severity.BLOCKER,
                    herbs=[std],
                    rule_source="《中国药典》",
                    suggestion=(
                        f"「{std}」为妊娠禁用药，不可用于妊娠期患者。"
                        "请移除该药。"
                    ),
                )
            )
        elif level == "caution":
            issues.append(
                SafetyIssue(
                    type=SafetyIssueType.PREGNANCY,
                    severity=Severity.HIGH,
                    herbs=[std],
                    rule_source="《中国药典》",
                    suggestion=(
                        f"「{std}」为妊娠慎用药，原则上妊娠期不宜使用。"
                        "如确需使用，请医师审慎评估并注明理由。"
                    ),
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 配伍禁忌（herbs.incompatibilities）
# ---------------------------------------------------------------------------


def _check_combination_incompatibilities(
    herbs: list[str],
    herb_records: dict[str, Herb],
    normalizer: HerbNormalizer,
    eighteen_issues: list[SafetyIssue],
    nineteen_issues: list[SafetyIssue],
) -> list[SafetyIssue]:
    """检查配伍禁忌（排除已在十八反/十九畏中命中的药对）。"""
    # 收集已命中药对
    hit_pairs: set[frozenset[str]] = set()
    for issue in eighteen_issues:
        if len(issue.herbs) >= 2:
            hit_pairs.add(frozenset(issue.herbs[:2]))
    for issue in nineteen_issues:
        if len(issue.herbs) >= 2:
            hit_pairs.add(frozenset(issue.herbs[:2]))

    normalized_set = {normalizer.normalize(h) for h in herbs}
    issues: list[SafetyIssue] = []
    seen: set[frozenset[str]] = set()

    for std_name in normalized_set:
        record = herb_records.get(std_name)
        if record is None:
            continue
        incompat = record.incompatibilities
        if not incompat or not isinstance(incompat, list):
            continue
        for entry in incompat:
            if not isinstance(entry, dict):
                continue
            target = entry.get("herb", "")
            target_std = normalizer.normalize(str(target))
            if target_std not in normalized_set:
                continue
            pair = frozenset([std_name, target_std])
            if pair in hit_pairs or pair in seen:
                continue
            seen.add(pair)
            issues.append(
                SafetyIssue(
                    type=SafetyIssueType.COMBINATION,
                    severity=Severity.BLOCKER,
                    herbs=sorted([std_name, target_std]),
                    rule_source=entry.get("source", ""),
                    suggestion=(
                        f"「{std_name}」与「{target_std}」存在配伍禁忌"
                        f"（{entry.get('reason', '')}）。请调整处方。"
                    ),
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 剂量上限
# ---------------------------------------------------------------------------


def _check_dose_limits(
    composition: list[HerbDose],
    normalizer: HerbNormalizer,
    herb_records: dict[str, Herb],
    unit_table: dict[str, DosageUnit],
) -> list[SafetyIssue]:
    """检查剂量上限。

    对每味药，若 dose_in_grams 非空且 max_dose 非空，比较：
    - D <= max_dose：通过
    - max_dose < D <= 2*max_dose：high
    - D > 2*max_dose：blocker
    """
    issues: list[SafetyIssue] = []
    seen: set[str] = set()

    for h in composition:
        std = normalizer.normalize(h.herb)
        if std in seen:
            continue
        seen.add(std)

        if h.dose is None:
            # 剂量已在 _convert_dose 阶段处理，此处跳过
            continue

        record = herb_records.get(std)
        if record is None:
            continue

        max_dose = record.max_dose
        if max_dose is None:
            continue

        try:
            max_dose_f = float(max_dose)
        except (TypeError, ValueError):
            continue

        dose_g = h.dose
        if dose_g <= max_dose_f:
            continue

        if dose_g <= 2 * max_dose_f:
            severity = Severity.HIGH
            level = "一般超量"
        else:
            severity = Severity.BLOCKER
            level = "严重超量"

        issues.append(
            SafetyIssue(
                type=SafetyIssueType.DOSE_LIMIT,
                severity=severity,
                herbs=[std],
                rule_source="《中国药典》",
                suggestion=(
                    f"「{std}」剂量 {dose_g:.1f}g 超过上限 {max_dose_f:.1f}g"
                    f"（{level}）。请调整剂量。"
                ),
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 过敏检查
# ---------------------------------------------------------------------------


def _check_allergy(
    herbs: list[str],
    patient_allergies: list[str],
    normalizer: HerbNormalizer,
    herb_records: dict[str, Herb],
) -> list[SafetyIssue]:
    """检查过敏史。"""
    if not patient_allergies:
        return []

    issues: list[SafetyIssue] = []
    seen: set[str] = set()

    for herb_name in herbs:
        std = normalizer.normalize(herb_name)
        if std in seen:
            continue
        seen.add(std)

        record = herb_records.get(std)
        all_names = {std}
        if record is not None:
            all_names.update(a for a in (record.aliases or []) if isinstance(a, str))

        for allergy in patient_allergies:
            allergy_std = normalizer.normalize(allergy)
            if allergy_std in all_names or allergy in all_names:
                issues.append(
                    SafetyIssue(
                        type=SafetyIssueType.ALLERGY,
                        severity=Severity.BLOCKER,
                        herbs=[std],
                        rule_source=f"患者过敏史: {allergy}",
                        suggestion=(
                            f"患者已知对「{allergy}」过敏，"
                            f"处方中含有「{std}」"
                            f"（标准名匹配或别名匹配）。请移除或替换该药。"
                        ),
                    )
                )
                break
    return issues


# ---------------------------------------------------------------------------
# R4-A 患者特定检查：有界归一化纯函数
# ---------------------------------------------------------------------------


def _normalize_bounded_text(value: Any, *, max_length: int = _MAX_TEXT_LENGTH) -> str | None:
    """NFKC + 空白折叠 + casefold 的有界文本归一化。

    非字符串、空白后为空、或归一化后超长的条目一律返回 ``None``（安全忽略），
    不抛异常、不做截断——截断可能制造并不存在的前缀假匹配。结果只依赖输入，
    不依赖模型/网络/时间/随机。
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    text = text.casefold()
    if len(text) > max_length:
        return None
    return text


def _bounded_normalized_conditions(
    major_conditions: Any,
    special_conditions: Any,
) -> frozenset[str]:
    """有界归一化患者条件集（major_conditions + special_conditions）。"""
    entries = [*(major_conditions or ()), *(special_conditions or ())]
    entries = entries[:_MAX_PATIENT_CONDITION_ITEMS]
    result: set[str] = set()
    for entry in entries:
        norm = _normalize_bounded_text(entry)
        if norm is not None:
            result.add(norm)
    return frozenset(result)


def _bounded_normalized_medications(medications: Any) -> tuple[str, ...]:
    """有界去重归一化患者当前用药（仅用于判定是否存在用药，名称不外泄）。"""
    entries = (medications or ())[:_MAX_PATIENT_MEDICATION_ITEMS]
    seen: set[str] = set()
    result: list[str] = []
    for entry in entries:
        norm = _normalize_bounded_text(entry)
        if norm is not None and norm not in seen:
            seen.add(norm)
            result.append(norm)
    return tuple(result)


def _patient_conditions_overflow(major_conditions: Any, special_conditions: Any) -> bool:
    """患者条件（major + special）合并**原始条目数**是否超过有界上限。

    只统计条目数、不扫描条目内容，保证 O(1) 工作量且不依赖输入顺序。
    超过上限时上层必须 fail-closed，不能把截断子集当作完整结果评估。
    """
    return len(major_conditions or ()) + len(special_conditions or ()) > _MAX_PATIENT_CONDITION_ITEMS


def _medications_overflow(medications: Any) -> bool:
    """患者当前用药**原始条目数**是否超过有界上限。

    超过上限即可能隐藏后续用药，必须 fail-closed 触发覆盖率门禁。
    """
    return len(medications or ()) > _MAX_PATIENT_MEDICATION_ITEMS


def _has_non_whitespace_medication(value: Any) -> bool:
    """有界判断单条用药是否构成"非空白报告条目"。

    只对字符串判定：非字符串忽略；字符串先做长度检查（不扫描超过
    ``_MAX_TEXT_LENGTH`` 的内容），超长条目 fail-closed 视为存在——无法在不
    越界扫描的前提下确认超长字符串为纯空白。空白折叠后非空的字符串视为存在。
    """
    if not isinstance(value, str):
        return False
    if len(value) > _MAX_TEXT_LENGTH:
        return True
    return bool(value.strip())


def _herb_contraindications_overflow(contraindications: Any) -> bool:
    """单味药 ``contraindications`` 原始条目数是否超过有界上限。

    超过上限时上层必须对该味药 fail-closed，避免截断后漏检禁忌命中。
    """
    return isinstance(contraindications, list) and len(contraindications) > _MAX_HERB_CONTRAINDICATION_ENTRIES


def _contraindication_entry_condition(entry: Any) -> str | None:
    """从单条 ``Herb.contraindications`` 条目提取有界归一化条件。

    - 字符串条目：直接归一化；
    - 严格 dict 条目：仅当固定键（condition/name）携带可接受的字符串时取该值；
    - 其余（非字符串、非 dict、键缺失、值非字符串/无界/超长/空白）一律忽略。
    """
    if isinstance(entry, str):
        return _normalize_bounded_text(entry)
    if isinstance(entry, dict):
        for key in _CONTRAINDICATION_DICT_KEYS:
            value = entry.get(key)
            norm = _normalize_bounded_text(value)
            if norm is not None:
                return norm
    return None


def _herb_contraindication_conditions(contraindications: Any) -> tuple[str, ...]:
    """提取一株药 ``contraindications`` 列表中全部去重后的有界归一化条件。"""
    if not isinstance(contraindications, list):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for entry in contraindications[:_MAX_HERB_CONTRAINDICATION_ENTRIES]:
        norm = _contraindication_entry_condition(entry)
        if norm is not None and norm not in seen:
            seen.add(norm)
            result.append(norm)
    return tuple(result)


def _patient_context_coverage_issue() -> SafetyIssue:
    """固定的患者上下文覆盖率门禁 issue（R4-B，fail-closed）。

    患者条件（major + special）合并条目数超过有界上限时，无法在不截断的前提下
    评估药材禁忌——不能把截断子集当作完整结果。此 issue 文本完全固定、不含任何
    条件名，保证审计可复现。
    """
    return SafetyIssue(
        type=SafetyIssueType.PATIENT_CONTEXT_COVERAGE,
        severity=Severity.HIGH,
        herbs=[],
        rule_source=_PATIENT_CONTEXT_COVERAGE_RULE_SOURCE,
        suggestion=(
            "PATIENT_CONTEXT_VERIFICATION_REQUIRED: 患者条件数量超过可审核上限，"
            "无法完整执行药材禁忌覆盖审核，发药前必须由医师人工核对患者情况。"
        ),
    )


def _herb_contraindication_coverage_issue(herb: str) -> SafetyIssue:
    """固定的单味药禁忌覆盖率门禁 issue（R4-B，fail-closed）。

    该味药 ``contraindications`` 条目数超过有界上限时，无法确认是否存在与患者
    条件的禁忌命中——不能静默截断后当作完整匹配。文本固定、不含任何原始禁忌
    条目，保证审计可复现。
    """
    return SafetyIssue(
        type=SafetyIssueType.HERB_CONTRAINDICATION_COVERAGE,
        severity=Severity.HIGH,
        herbs=[herb],
        rule_source=_HERB_CONTRAINDICATION_COVERAGE_RULE_SOURCE,
        suggestion=(
            f"「{herb}」的禁忌条目超过可审核上限，无法确认是否与患者情况冲突，"
            "发药前必须由医师人工核对。"
        ),
    )


def _check_herb_contraindications(
    herbs: list[str],
    patient_info: PatientInfo,
    herb_records: dict[str, Herb],
    normalizer: HerbNormalizer,
) -> list[SafetyIssue]:
    """患者条件与每味药 ``contraindications`` 的精确匹配检查（R4-A/R4-B）。

    仅做归一化后的**精确**相等匹配，不做子串/模糊匹配。条件来自知识库
    ``Herb.contraindications`` 中既有字符串条目或严格 dict 条目，不虚构任何
    医学交互表。命中的每味药产生一条 BLOCKER（suggestion 列出全部命中条件，
    条件做确定性排序）；同一药名重复出现只检查一次。空患者条件直接返回。

    R4-B fail-closed 边界：
    - 患者条件合并条目数超过上限时，**不评估截断子集**，改为发射恰好一条
      HIGH 的 ``PATIENT_CONTEXT_COVERAGE`` 覆盖率门禁；
    - 单味药 ``contraindications`` 条目数超过上限时，对该味药发射一条固定的
      HIGH ``HERB_CONTRAINDICATION_COVERAGE`` 覆盖率门禁，替代精确匹配。
    """
    if _patient_conditions_overflow(
        patient_info.major_conditions,
        patient_info.special_conditions,
    ):
        return [_patient_context_coverage_issue()]
    patient_conditions = _bounded_normalized_conditions(
        patient_info.major_conditions,
        patient_info.special_conditions,
    )
    if not patient_conditions:
        return []
    issues: list[SafetyIssue] = []
    seen_herbs: set[str] = set()
    for herb_name in herbs:
        std = normalizer.normalize(herb_name)
        if not std or std in seen_herbs:
            continue
        seen_herbs.add(std)
        record = herb_records.get(std)
        if record is None:
            continue
        contraindications = getattr(record, "contraindications", None)
        if _herb_contraindications_overflow(contraindications):
            issues.append(_herb_contraindication_coverage_issue(std))
            continue
        matched = sorted(
            condition
            for condition in _herb_contraindication_conditions(contraindications)
            if condition in patient_conditions
        )
        if not matched:
            continue
        issues.append(
            SafetyIssue(
                type=SafetyIssueType.HERB_CONTRAINDICATION,
                severity=Severity.BLOCKER,
                herbs=[std],
                rule_source=_HERB_CONTRAINDICATION_RULE_SOURCE,
                suggestion=(
                    f"「{std}」与患者情况存在用药禁忌："
                    + "、".join(f"「{condition}」" for condition in matched)
                    + "（知识库 Herb.contraindications 精确匹配）。请医师核对后决定是否换药或调整。"
                ),
            )
        )
    return issues


def _medication_interaction_coverage_issue() -> SafetyIssue:
    """固定的用药相互作用覆盖率门禁 issue（R4-A/R4-B，fail-closed）。

    文本完全固定、不含任何药物名称，保证审计可复现。
    """
    return SafetyIssue(
        type=SafetyIssueType.MEDICATION_INTERACTION_COVERAGE,
        severity=Severity.HIGH,
        herbs=[],
        rule_source=_MEDICATION_INTERACTION_RULE_SOURCE,
        suggestion=(
            "MEDICATION_INTERACTION_VERIFICATION_REQUIRED: 患者正在用药，"
            "知识库无权威中药-西药相互作用数据，发药前必须由医师人工核对相互作用。"
        ),
    )


def _check_medication_interaction_coverage(patient_info: PatientInfo) -> list[SafetyIssue]:
    """用药-药物相互作用覆盖率门禁（R4-A/R4-B，fail-closed）。

    患者存在用药且本仓库没有权威中药-西药相互作用数据时，发射**恰好一条** HIGH
    兜底 issue，要求医师人工核对相互作用。这是覆盖率阻断而非虚构的相互作用结论；
    药物名称不进入 rule_source/suggestion。

    R4-B 修复：
    - 只要存在任意**非空白**报告条目即触发门禁——包括超长条目（无法在不越界
      扫描的前提下确认其内容）以及条目数超过上限的列表；
    - 只有空/纯空白条目的列表保持为空、不触发门禁；
    - 超长或非法首条目不会"遮蔽"后续合法用药（存在性判定逐条扫描全部条目）。
    """
    if _HAS_AUTHORITATIVE_HERB_DRUG_INTERACTION_DATA:
        return []
    medications = patient_info.current_medications
    if _medications_overflow(medications):
        return [_medication_interaction_coverage_issue()]
    if any(_has_non_whitespace_medication(entry) for entry in (medications or ())):
        return [_medication_interaction_coverage_issue()]
    return []


# ---------------------------------------------------------------------------
# 未知药名检查（保守假设：§1.1）
# ---------------------------------------------------------------------------


def _check_unknown_herbs(
    herbs: list[str],
    herb_records: dict[str, Herb],
    normalizer: HerbNormalizer,
) -> list[SafetyIssue]:
    """检查处方中是否存在知识库未收录的药名。

    无法在 ``herbs`` 表中查到的药材无法执行妊娠禁忌、配伍禁忌、
    剂量上限等关键检查，按保守假设生成 ``high`` 级别 ``caution``
    问题，阻断后续流程。
    """
    issues: list[SafetyIssue] = []
    seen: set[str] = set()

    for herb_name in herbs:
        std = normalizer.normalize(herb_name)
        if std in seen:
            continue
        seen.add(std)
        if std not in herb_records:
            issues.append(
                SafetyIssue(
                    type=SafetyIssueType.CAUTION,
                    severity=Severity.HIGH,
                    herbs=[std],
                    rule_source="知识库",
                    suggestion=(
                        f"「{std}」在中药知识库中未收录（未匹配到标准药名或别名）。"
                        "无法对其执行妊娠禁忌、配伍禁忌、剂量上限等安全检查。"
                        "请核实药名准确性，或补充知识库后再审。"
                    ),
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 去重与排序
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.BLOCKER: 0,
    Severity.HIGH: 1,
    Severity.WARNING: 2,
    Severity.INFO: 3,
}

_RULE_ORDER: dict[SafetyIssueType, int] = {
    SafetyIssueType.EIGHTEEN_INCOMPATIBILITIES: 0,
    SafetyIssueType.NINETEEN_FEARS: 1,
    SafetyIssueType.PREGNANCY: 2,
    SafetyIssueType.COMBINATION: 3,
    SafetyIssueType.DOSE_LIMIT: 4,
    SafetyIssueType.ALLERGY: 5,
    SafetyIssueType.UNIT_CONVERSION: 6,
    SafetyIssueType.CAUTION: 7,
    # R4-A 患者特定规则（追加在既有规则之后，保持既有相对顺序不变）。
    SafetyIssueType.HERB_CONTRAINDICATION: 8,
    SafetyIssueType.MEDICATION_INTERACTION_COVERAGE: 9,
    # R4-B fail-closed 覆盖率门禁（继续追加，保持既有相对顺序不变）。
    SafetyIssueType.HERB_CONTRAINDICATION_COVERAGE: 10,
    SafetyIssueType.PATIENT_CONTEXT_COVERAGE: 11,
}


def _issue_key(issue: SafetyIssue) -> tuple[str, frozenset[str]]:
    """去重键：(type, frozenset(sorted_herbs))。"""
    return (issue.type, frozenset(issue.herbs))


def _deduplicate(issues: list[SafetyIssue]) -> list[SafetyIssue]:
    """以 (type, sorted_herbs) 为唯一键去重，保留严重度较高的那条。"""
    best: dict[tuple[str, frozenset[str]], SafetyIssue] = {}
    for issue in issues:
        key = _issue_key(issue)
        if key not in best:
            best[key] = issue
        else:
            existing_sev = _SEVERITY_ORDER.get(best[key].severity, 99)
            new_sev = _SEVERITY_ORDER.get(issue.severity, 99)
            if new_sev < existing_sev:
                best[key] = issue
    return list(best.values())


def _sort_issues(issues: list[SafetyIssue]) -> list[SafetyIssue]:
    """排序：blocker > high > warning > info，同严重度按规则执行顺序，同规则按药名拼音。"""

    def sort_key(issue: SafetyIssue) -> tuple[int, int, str]:
        sev = _SEVERITY_ORDER.get(issue.severity, 99)
        rule = _RULE_ORDER.get(issue.type, 99)
        first_herb = issue.herbs[0] if issue.herbs else ""
        return (sev, rule, first_herb)

    return sorted(issues, key=sort_key)


def _determine_passed(issues: list[SafetyIssue]) -> bool:
    """无 blocker 且无 high 时为 true。"""
    return all(i.severity not in (Severity.BLOCKER, Severity.HIGH) for i in issues)


__all__ = ["SafetyRuleEngine"]
