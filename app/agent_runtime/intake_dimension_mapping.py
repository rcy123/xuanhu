"""L3-3 维度覆盖与成熟度键集（D1 厚真源）。

本模块是「抽取层产出的 fact_key」与「完整性层 / 维度内成熟度层的维度判定」之间的**统一
键桥**。它解决同一类死循环根因——抽取模型对同一临床语义在多个 session 里产出多种形态
的 fact_key（如寒热可落 ``present_illness.chills``、``present_illness.fever`` 或
``present_illness.symptom.fever``；咳嗽可落 ``present_illness.cough``、
``present_illness.symptom.cough`` 甚至裸 ``symptom``），而完整性层每个维度只认一张窄键集。
键不命中 → 维度永远 missing → gap_selector 永远选同一维度 → 命中写死模板 → 问诊死循环
（见 trigger session 63e78741 寒热、d8ba36ae 现病变化）。

本模块给出两条互补的源，都按「维度」聚合，单一真源、并列维护，供 D1 覆盖与 D2 成熟度共
用同一份键集真源：

- :data:`DIMENSION_KEYSETS`：每个完整体维度 → 该维度临床语义下**所有**可能命中的 fact_key
  元组（既含 canonical 键如 ``ten_questions.cold_heat``，也含抽取层实际派生落下的
  ``present_illness.*`` 现性键、子前缀键 ``present_illness.symptom.*`` 等）。任一命中即视为
  该维度**已被覆盖**（D1 调用 :data:`derived_coverage_for_fact_keys`）。
- :data:`MATURITY_KEY_THRESHOLDS`：每个需要"维度内追问细节"的维度 → 成熟所需已采关键键条
  数下限。**已覆盖但 keyset 内已采数 < 下限 → 未成熟 → D2 闸门偎留同维度追问**（D2 调用
  :func:`dimension_acquired_key_count`）。无阈值的维度走「覆盖即成熟」默认（与现状一致）。

设计铁律（与完整方案 D1/D2 一致）：

- **单向覆盖、不反向删除**：派生只把现性键「兜上来」覆盖到对应完整体维度，不会让
  ``ten_questions.*`` 被解释成对 ``present_illness.*`` 维度的覆盖，也不会删除/corrected 任何
  canonical 事实。
- **保守键集**：键集只列临床语义确实构成该维度覆盖的键；安全维度（过敏/妊娠/用药/重大病
  史/哺乳）**不走派生**，仍由 ``safety_profile.collection_status`` 决定，不在此表内。
- **覆盖 ≠ 成熟**：覆盖派生（D1）回答"该维度有没有被医生答到过"，成熟度闸门（D2）回答
  "答到的是不是足够支撑辨证/下问"。前者口径宽（任一命中即覆盖），后者口径严（关键键齐全
  才成熟）。这正是防"放水悄悄漏临床语义"的双层结构。
- 已覆盖派生键集里**不**包含裸键（``symptom``、``fever``）与超义根键：裸键缺乏临床路径归
  属、与 ``chief_complaint.symptom`` 正交但语义模糊，派生只会带来误判；它们由抽取 verifer 的
  合法键集约束在源头拦（越界键本场不归键桥处理）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.schemas.completeness import InquiryDimension

#: 各完整体维度 → 该维度临床语义下所有可命中的 fact_key（canonical + 派生）。
#:
#: - canonical 键：``COMPLETENESS_DIMENSION_RULES[dimension].fact_keys`` 已认的那些键，这里
#:   重复列出只是为「单一真源」可读，覆盖判定对 canonical 键的命中由 ``_facts_by_dimension``
#:   负责（``fact_keys`` 命中→facts_by_dimension 分组→covered）。
#: - 派生键：抽取模型实际高频产出、但不属 canonical 键的事实键（多为 ``present_illness.*``
#:   与 ``ten_questions.<dim>.<sub>`` 子前缀）。派生键经 :data:`derived_coverage_for_fact_keys`
#:   兜上 covered。
#:
#: 键集依据：跨多 session 真实抽取产出事实驱动（见 trigger sessions 63e78741 / d8ba36ae /
#: 67e04fc4 / 1b89a179 实采 fact_key 面）。
DIMENSION_KEYSETS: Mapping[InquiryDimension, tuple[str, ...]] = {
    # --- 主诉 / 现病（canonical + 现性派生） ---
    InquiryDimension.CHIEF_COMPLAINT_SYMPTOM: ("chief_complaint.symptom",),
    InquiryDimension.BASIC_COURSE: (
        "chief_complaint.course",
        "chief_complaint.duration",
        "onset.duration",
    ),
    # 现病变化：canonical 是 present_illness.change/associated_symptom。
    # 抽取模型对"具体不适"常落到 present_illness.symptom.*（67e04fc4）或裸现性键
    # present_illness.cough（d8ba36ae）——这些表达"有现病新症状、但未明变化趋势"。它们覆盖
    # 到 change 维度（医生确实答了现病），但成熟度另有下限（见 MATURITY_KEY_THRESHOLDS），
    # 由 D2 闸门强制追问变化趋势细节，不会"覆盖即放过"。
    InquiryDimension.PRESENT_ILLNESS_CHANGE: (
        "present_illness.change",
        "present_illness.associated_symptom",
        # 抽取层产出的具体现病症状键（含子前缀 present_illness.symptom.* 子树）。
        "present_illness.cough",
        "present_illness.symptom.cough",
        "present_illness.sputum",
        "present_illness.symptom.sputum",
        "present_illness.rhinorrhea",
        "present_illness.symptom.rhinorrhea",
        "present_illness.sore_throat",
        "present_illness.symptom.sore_throat",
        "present_illness.nasal_congestion",
        "present_illness.shortness_of_breath",
    ),
    # --- 十问歌（canonical ten_questions.* + present_illness.* 现性派生） ---
    InquiryDimension.TEN_COLD_HEAT: (
        "ten_questions.cold_heat",
        "present_illness.chills",
        "present_illness.fever",
        "present_illness.aversion_cold",
        "present_illness.symptom.fever",
        "present_illness.symptom.chills",
    ),
    InquiryDimension.TEN_SWEAT: (
        "ten_questions.sweat",
        "present_illness.sweat",
        "present_illness.sweating",
        "present_illness.symptom.sweat",
    ),
    InquiryDimension.TEN_HEAD_BODY: (
        "ten_questions.head_body",
        "present_illness.head_body",
        "present_illness.body_ache",
        "present_illness.symptom.head_body",
    ),
    InquiryDimension.TEN_STOOL_URINE: (
        # canonical 多键（completeness rule 已认 stool_urine / stool / urine）。
        "ten_questions.stool_urine",
        "ten_questions.stool",
        "ten_questions.urine",
        # 抽取层漂移到 ten_questions.stool_urine.<sub> 子前缀（1b89a179 实测）。
        "ten_questions.stool_urine.stool",
        "ten_questions.stool_urine.urine",
        # 现性派生。
        "present_illness.stool",
        "present_illness.urine",
        "present_illness.symptom.stool",
        "present_illness.symptom.urine",
    ),
    InquiryDimension.TEN_DIET: (
        "ten_questions.diet",
        "ten_questions.appetite",
        "present_illness.appetite",
        "present_illness.diet",
        "present_illness.symptom.appetite",
    ),
    InquiryDimension.TEN_CHEST_ABDOMEN: (
        "ten_questions.chest_abdomen",
        "ten_questions.abdomen",
        "present_illness.chest",
        "present_illness.abdomen",
        "present_illness.symptom.chest",
        "present_illness.symptom.abdomen",
        "present_illness.distension",
    ),
    InquiryDimension.TEN_THIRST: (
        "ten_questions.thirst",
        "present_illness.thirst",
        "present_illness.symptom.thirst",
    ),
    InquiryDimension.TEN_SLEEP: (
        "ten_questions.sleep",
        "present_illness.sleep",
        "present_illness.insomnia",
        "present_illness.symptom.sleep",
    ),
    InquiryDimension.TEN_MENSES_LEUKORRHEA: (
        "ten_questions.menses_leukorrhea",
        "ten_questions.menses",
    ),
    InquiryDimension.TEN_PAIN: (
        "ten_questions.pain",
        "present_illness.pain",
        "present_illness.symptom.pain",
    ),
    InquiryDimension.TEN_RESPIRATORY: (
        "ten_questions.respiratory",
        "present_illness.respiratory",
        "present_illness.cough",
        "present_illness.symptom.cough",
        "present_illness.sputum",
        "present_illness.symptom.sputum",
        "present_illness.shortness_of_breath",
    ),
    # --- 患者人口学（canonical 已认，无派生） ---
    InquiryDimension.PATIENT_SEX: (
        "patient.sex",
        "patient.gender",
    ),
    InquiryDimension.PATIENT_AGE: ("patient.age",),
    InquiryDimension.MENOPAUSE_STATUS: (
        "patient.menopause",
        "patient.menopause_status",
    ),
    InquiryDimension.PREGNANCY_APPLICABILITY_FLAG: ("patient.pregnancy_applicable",),
    InquiryDimension.LACTATION_APPLICABILITY_FLAG: ("patient.lactation_applicable",),
    # 可选汇报维度（canonical 已认，不设派生）。
    InquiryDimension.PAST_HISTORY: ("past_history",),
    InquiryDimension.FOUR_DIAGNOSIS: (
        "four_diagnosis.inspection",
        "four_diagnosis.palpation",
    ),
    # 主诉类别仅冲突维度、无覆盖键（canonical COMPLETENESS_AUXILIARY_FACT_DIMENSIONS 处理）。
    InquiryDimension.CHIEF_COMPLAINT_CATEGORY: ("chief_complaint.category",),
    # 安全维度不在键桥内——由 safety_profile.collection_status 决定，留空占位以防误用派生。
    InquiryDimension.ALLERGY_STATUS: (),
    InquiryDimension.MEDICATION_STATUS: (),
    InquiryDimension.MAJOR_CONDITION_STATUS: (),
    InquiryDimension.PREGNANCY_STATUS: (),
    InquiryDimension.LACTATION_STATUS: (),
}

#: 维度 → 成熟所需"已采关键键条数"下限。
#:
#: 仅为那些「覆盖宽、成熟严」的维度设下限。combeplete 层先判 covered（任一 keyset 命中），
#: 至 D2 闸门再判 mature（keyset 内已 active 事实 ≥ 下限）。已覆盖但未达下限 → D2 见到
#: ``model_needs_more``/``key_threshold_met`` → D4 偎留同维度追问细节，不跳维度。
#:
#: - 不设下限的维度（不在此表）→ covered 即 mature，沿用现状「覆盖即过」语义，不影响。
#: - 设下限维度的"宽键集"里的派生键同样计入 acquired——因为医生确实答到了该维度语义，
#:   只是颗粒不足；D2 据此追问"补谁"。canonical 键与派生键在成熟度计数上等权。
MATURITY_KEY_THRESHOLDS: Mapping[InquiryDimension, int] = {
    # 寒热：{寒意,发热} 至少采到 2 条（怕冷+发热一般都要分别确认）。
    InquiryDimension.TEN_COLD_HEAT: 2,
    # 二便：大便、小便两条都要采（单证大便不补小便未成熟）。
    InquiryDimension.TEN_STOOL_URINE: 2,
    # 现病变化趋势：任一现病新症状可覆盖，但成熟需"叙述中带有变化/演进"语义键命中——
    # canonical present_illness.change / associated_symptom 表达趋势；纯具体症状键（cough 等）
    # 只覆盖不计趋势。故 mature 下限要求至少采集到 1 条趋势键（见 D2 用 :data:`MATURITY_
    # TREND_KEYS` 收口）。
    InquiryDimension.PRESENT_ILLNESS_CHANGE: 1,
}

#: 越界畸键 → canonical/派生合法 fact_key 的归一映射。
#:
#: 抽取模型对同一临床语义会在不同 session 漂出犀键（trigger session b7bdf5ab 实测：
#: 医生答「怕冷，微微发热」被抽成 ``symptom.chills`` / ``symptom.fever``——裸 ``symptom.*`` 前缀
#: 既不在 canonical ``COMPLETENESS_DIMENSION_RULES[dimension].fact_keys``，也不在本模块
#: :data:`DIMENSION_KEYSETS` 派生键集内）。这类畸键若被 E1 一律 reject（越界 ADD 直接丢），
#: 则该维度本轮**永远采不到键** → 永远 missing → gap_selector 锁死 → 命中写死模板 → 死循环
#: （b7bdf5ab 第 3 轮 symptom.chills/fever 全 reject → 寒热维度无落地键 → 同句重复）。
#:
#: E1 闸门对越界 **ADD** 键命中本表时，**先把 fact_key 改写为右侧 canonical/派生合法键透传落库**
#: （而非 reject 丢弃），让 D1 派生覆盖与 D2 成熟度闸门在 canonical 路径上认它。归一只改键名，
#: value/evidence/confidence/operation 原样保留；归一事件留痕回传调用方写 claim（与 reject 同管道，
#: 见 :data:`NormalizedObservation` 与 :func:`normalized_observations_to_payload`）。
#:
#: 设计边界：
#: - **只对 ADD 归一**：CORRECT/RETRACT 的 target 指向历史事实，改键名会破坏 target 绑定；
#:   它们仍走 E1 的 reject / 降级伪 retract 路径（d449735a 历史畸键自治愈路径）。
#: - **右侧键必须本身合法**：归一目标一律落在 :data:`DIMENSION_KEYSETS` 已登记的 canonical/
#:   派生键内，归一后的落库键对 reducer/safety 仍是已知合法键，不引入新越界。
#: - **不与 keyset 重复登记畸键**：畸键（如 ``symptom.chills``）只在本表登记「去哪」，
#:   不收进 :data:`DIMENSION_KEYSETS`（避免「同一信号被 D1 派生覆盖与 E1 归一两种方式重复算」，
#:   职责切分：keyset 认 canonical/派生合法键，归一把漂畸键拉回 canonical）。
#: - keyset 另补 :data:`present_illness.symptom.*` 这类「合法子前缀漂移但语义仍属该维度」、
#:   E1 因合法前缀**本就不会 reject** 的键（见 :data:`DIMENSION_KEYSETS` 内对应注释）——
#:   它们不归一、原样落库 + 被 D1 直接认。
#: - 表按「真实抽取产出事实驱动」维护（与 :data:`DIMENSION_KEYSETS` 同原则，见各 trigger
#:   session 实采漂移样例）：下一种新漂移形态出现时再补，不预判、不穷举。
DERIVED_KEY_NORMALIZATION: Mapping[str, str] = {
    # b7bdf5ab 实测：「怕冷，微微发热」→ symptom.chills / symptom.fever 裸前缀漂移，
    # 归一到 canonical 寒热现性键。
    "symptom.chills": "present_illness.chills",
    "symptom.fever": "present_illness.fever",
    "symptom.aversion_cold": "present_illness.aversion_cold",
    "symptom.sweat": "present_illness.sweat",
    "symptom.cough": "present_illness.cough",
    "symptom.sputum": "present_illness.sputum",
    "symptom.rhinorrhea": "present_illness.rhinorrhea",
    "symptom.sore_throat": "present_illness.sore_throat",
    "symptom.nasal_congestion": "present_illness.nasal_congestion",
    "symptom.shortness_of_breath": "present_illness.shortness_of_breath",
    "symptom.head_body": "present_illness.head_body",
    "symptom.body_ache": "present_illness.body_ache",
    "symptom.chest": "present_illness.chest",
    "symptom.abdomen": "present_illness.abdomen",
    "symptom.distension": "present_illness.distension",
    "symptom.thirst": "present_illness.thirst",
    "symptom.sleep": "present_illness.sleep",
    "symptom.insomnia": "present_illness.insomnia",
    "symptom.appetite": "present_illness.appetite",
    "symptom.diet": "present_illness.diet",
    "symptom.pain": "present_illness.pain",
    "symptom.stool": "present_illness.stool",
    "symptom.urine": "present_illness.urine",
    # 1f8240c7 真实端到端实跑实测：模型把"咳嗽三天伴白痰咽痒鼻塞流涕"一律漂成
    # symptoms.*（复数前缀 + 子点细分）。前缀归一到单数 symptom.* 后再走上面的现性键映射；
    # 子点细分（symptoms.cough.duration / symptoms.throat.itch 等）无对应 canonical 单键，
    # 归一到最接近的现性键，避免 chief_complaint.symptom 维度因越界键整轮丢失。
    "symptoms.cough": "present_illness.cough",
    "symptoms.cough.duration": "chief_complaint.course",
    "symptoms.cough.type": "present_illness.cough",
    "symptoms.cough.worsening": "present_illness.change",
    "symptoms.phlegm": "present_illness.sputum",
    "symptoms.sputum": "present_illness.sputum",
    "symptoms.throat.itch": "present_illness.sore_throat",
    "symptoms.sore_throat": "present_illness.sore_throat",
    "symptoms.nasal.congestion": "present_illness.nasal_congestion",
    "symptoms.nasal_congestion": "present_illness.nasal_congestion",
    "symptoms.rhinorrhea": "present_illness.rhinorrhea",
    "symptoms.sleep.disturbance": "present_illness.sleep",
    "symptoms.sleep": "present_illness.sleep",
    "symptoms.shortness_of_breath": "present_illness.shortness_of_breath",
    "symptoms.chills": "present_illness.chills",
    "symptoms.fever": "present_illness.fever",
    "symptoms.sweat": "present_illness.sweat",
    "symptoms.head_body": "present_illness.head_body",
    "symptoms.body_ache": "present_illness.body_ache",
    "symptoms.chest": "present_illness.chest",
    "symptoms.abdomen": "present_illness.abdomen",
    "symptoms.distension": "present_illness.distension",
    "symptoms.thirst": "present_illness.thirst",
    "symptoms.appetite": "present_illness.appetite",
    "symptoms.diet": "present_illness.diet",
    "symptoms.pain": "present_illness.pain",
    "symptoms.stool": "present_illness.stool",
    "symptoms.urine": "present_illness.urine",
}


def normalize_drifted_fact_key(fact_key: str) -> str | None:
    """Return the canonical/derived legal fact_key a drifted key should map to.

    供 E1 闸门使用：越界 ADD 键若命中 :data:`DERIVED_KEY_NORMALIZATION`，返回右侧合法键
    （调用方据此改写 observation 的 fact_key 后透传落库）；否则返回 None（键无处可归，走
    reject / 降级 retract 原路径）。**只查表、不改输入**。
    """

    return DERIVED_KEY_NORMALIZATION.get(fact_key)


#: 趋势语义键——仅用于 PRESENT_ILLNESS_CHANGE 成熟度闸门：判断"已采事实里是否包含变化趋
#: 势"语义（与之等价的 canonical 趋势键）。具体症状键（cough/fever 等）覆盖该维度但不构成
#: 趋势，D2 据此追问"加重/减轻/稳定"。
#:
#: 设计：与 :data:`MATURITY_KEY_THRESHOLDS[PRESENT_ILLNESS_CHANGE]` 配合——成熟判 = 已采到
#: 至少 1 条趋势键（既覆盖又回答了"变化"语义），任一具体症状键只能覆盖。
MATURITY_TREND_KEYS: Mapping[InquiryDimension, frozenset[str]] = {
    InquiryDimension.PRESENT_ILLNESS_CHANGE: frozenset(
        ("present_illness.change", "present_illness.associated_symptom")
    ),
}


def derived_coverage_for_fact_keys(
    active_fact_keys: frozenset[str],
) -> dict[InquiryDimension, tuple[str, ...]]:
    """Return per-dimension derived fact_keys actually present in ``active_fact_keys``.

    供完整性层 ``_covered_dimensions`` 使用：给定当前已 active 事实的 fact_key 集合，返回命中
    各维度 keyset 的（维度 → 命中的 keyset 键元组）。**任一命中即该维度 covered**。

    说明：返回的是「命中了 keyset 里的某个键的维度」全集——调用方逐个 ``covered.add``。
    单向覆盖、不改写输入：仅消费 set 中的键名。
    """

    derived: dict[InquiryDimension, tuple[str, ...]] = {}
    for dimension, keyset in DIMENSION_KEYSETS.items():
        hit = tuple(key for key in keyset if key in active_fact_keys)
        if hit:
            derived[dimension] = hit
    return derived


def dimension_acquired_key_count(
    dimension: InquiryDimension,
    active_fact_keys: frozenset[str],
) -> int:
    """Count distinct keyset keys of ``dimension`` actually acquired (active) in facts.

    供 D2 成熟度闸门①使用：某维度 keyset 内「已被 active 事实命中」的 key 个数。canonical
    键与派生键等权计入 acquired（医生答到的都算"采到该条关键键"）。
    """

    keyset = DIMENSION_KEYSETS.get(dimension, ())
    return sum(1 for key in keyset if key in active_fact_keys)


def dimension_has_trend_key(
    dimension: InquiryDimension,
    active_fact_keys: frozenset[str],
) -> bool:
    """Return True when ``dimension`` maturity requires a trend key and one is acquired.

    专供 :data:`MATURITY_TREND_KEYS` 收了条目的维度使用（当前仅 PRESENT_ILLNESS_CHANGE）：
    判断已采事实里是否含至少一条趋势语义键。无趋势键条目的维度恒返回 True（无趋势要求）。
    """

    trend_keys = MATURITY_TREND_KEYS.get(dimension)
    if trend_keys is None:
        return True
    return any(key in active_fact_keys for key in trend_keys)


def slot_threshold_for(dimension: InquiryDimension) -> int:
    """2a: 粗槽位阈值(决策 12)——采到 N 项语义即齐。

    单一真源 :data:`MATURITY_KEY_THRESHOLDS`(寒热≥2/二便≥2/现病变化≥1);
    无阈值维度默认 1(覆盖即齐,与现状「一键即过」下限对齐,阶段 2c 起
    covered 判定从「认键」迁移为「认槽位齐」时保持判定不变——改容器不改判定)。
    """

    return MATURITY_KEY_THRESHOLDS.get(dimension, 1)


def dimension_slot_satisfied(
    dimension: InquiryDimension,
    slot_count: int,
    *,
    llm_signal: str | None = None,
) -> bool:
    """2a: 槽位齐判定(确定性闸门,铁律 10 + 决策 25)。

    - llm_signal=complete: LLM 判齐,代码按粗槽位阈值复核(LLM 不能输出"放过"突破下限);
    - llm_signal=partial: LLM 主导——语义缺口存在,即使槽位数达标也继续追问(决策 25);
    - llm_signal 缺失/unknown: 代码兜底,退回粗槽位阈值判定(采到 N 项即齐)。
    """

    threshold = slot_threshold_for(dimension)
    if llm_signal == "partial":
        return False
    if llm_signal == "complete":
        return slot_count >= threshold
    return slot_count >= threshold


def derive_dimension_slots(
    active_facts: tuple[Any, ...],
    *,
    dimensions: frozenset[InquiryDimension],
    max_slots_per_dimension: int = 8,
) -> tuple[dict[str, Any], ...]:
    """2b: 从已验证 observations 派生粗槽位快照(JSON-safe dict,可入 Graph State)。

    设计(决策 12「改容器不改判定」+ 2.5a 灰度):
    - 不做模型契约变更——槽位对象由确定性代码从 E1/D1 已验证的 observations
      按 ``DIMENSION_KEYSETS`` 归属派生,落库单元仍是裸键(过渡期,E1/D1 保留);
    - 每个维度一个快照:slots = 该维度 keyset 内已采到的 (fact_key, value) 语义项,
      completeness 按粗槽位阈值判定(complete / partial),missing_slots 列阈值缺口;
    - 灰度关闭时 covered 判定仍认键(现状);开启时认槽位齐(阶段 2c 接入),
      两个口径的判定阈值同源(``MATURITY_KEY_THRESHOLDS``),迁移不改变判定结果。
    """

    from app.schemas.intake import DimensionSlotSnapshot, DimensionSlotValue, SlotCompleteness

    by_dimension: dict[InquiryDimension, list[Any]] = {d: [] for d in dimensions}
    for fact in active_facts:
        key = getattr(fact, "fact_key", None)
        if not isinstance(key, str):
            continue
        for dimension in dimensions:
            if key in DIMENSION_KEYSETS.get(dimension, ()):
                by_dimension[dimension].append(fact)
                break

    snapshots: list[dict[str, Any]] = []
    for dimension in sorted(dimensions, key=lambda item: item.value):
        facts = by_dimension[dimension][:max_slots_per_dimension]
        slot_count = len(facts)
        satisfied = dimension_slot_satisfied(dimension, slot_count)
        threshold = slot_threshold_for(dimension)
        missing = ()
        if not satisfied and slot_count < threshold:
            missing = (f"还需采集 {threshold - slot_count} 项该维度语义",)
        snapshots.append(
            DimensionSlotSnapshot(
                dimension=dimension.value,
                slots=tuple(
                    DimensionSlotValue(
                        slot_name=fact.fact_key,
                        value=(fact.normalized_value if fact.normalized_value is not None else fact.value),
                        source_message_id=fact.source_message_id,
                        confidence=fact.confidence if fact.confidence is not None else 0.9,
                    )
                    for fact in facts
                ),
                completeness=(SlotCompleteness.COMPLETE if satisfied else SlotCompleteness.PARTIAL),
                missing_slots=missing,
            ).model_dump(mode="json")
        )
    return tuple(snapshots)


def derive_slot_context_rows(
    observations: tuple[Any, ...],
    *,
    dimensions: frozenset[InquiryDimension],
    state_version: int,
    session_id: Any,
    max_rows: int = 32,
) -> tuple[dict[str, Any], ...]:
    """3a: 槽位投影行(每维度一行,JSON-safe dict,供下游 context_observations)。

    灰度开启时下游辨证/开方 prompt 看到规整的维度槽位对象,而非裸 fact_key
    列表(问题 22)。行结构对齐 SyndromeObservationContext 可映射形状:
    - fact_key = 维度枚举值(程序定义,无漂移键)
    - value = 槽位快照(JSON-safe:dimension/slots/completeness/missing_slots)
    - 无槽位(空提取)的维度不产行;observation_id 用稳定 uuid5(session_id, dimension)。

    适配说明(review nit):入参 observations 为 DomainState 的 ObservationSchema
    (有 value/normalized_value/source_message_id/confidence);若从 completeness
    snapshot 传入 CompletenessObservationFact 需先做字段适配。
    """
    from uuid import NAMESPACE_URL, uuid5

    from app.schemas.domain import ObservationStatus

    active = tuple(item for item in observations if getattr(item, "status", None) is ObservationStatus.ACTIVE)
    snapshots = derive_dimension_slots(active, dimensions=dimensions)
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not snapshot.get("slots"):
            continue
        rows.append(
            {
                "observation_id": str(uuid5(NAMESPACE_URL, f"xuanhu:slot:{session_id}:{snapshot['dimension']}")),
                "session_id": str(session_id),
                "state_version": state_version,
                "fact_key": snapshot["dimension"],
                "value": snapshot,
                "normalized_value": None,
                "status": "active",
            }
        )
        if len(rows) >= max_rows:
            break
    return tuple(rows)
