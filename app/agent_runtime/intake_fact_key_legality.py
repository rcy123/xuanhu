"""L3-1 抽取产出 fact_key 合法性闸门（E1）。

抽取模型（intake_extraction）对同一临床语义会产出多种 fact_key，其中一部分是**越界畸键**——
既不落在任何 completeness canonical 维度的 ``fact_keys`` 内，也不在维度键桥 ``DIMENSION_KEYSETS``
的派生键集内。典型实测样例（trigger session d449735a）：

- ``symptom.cold_heat``（医生答"夜晚怕冷，微微发热"被抽取漂移成这条畸键，本该是
  ``present_illness.chills`` + ``present_illness.fever`` 两条）
- ``symptom``、``fever``（裸键，缺前缀归属，trigger session 282a985a / 67e04fc4 实测）
- ``present_illness.symptom.pain`` 等未在 keyset 内的子前缀漂移键

这类畸键喂不进任何 canonical 维度，也不命中 D1 派生覆盖 → 对应维度永远 missing →
gap_selector 永远选同一维度 → 命中写死模板 → 问诊死循环。键桥再厚也追不上一台会随机换键
名的模型；"把所有可能的畸键都枚举进 keyset"是错误方向（被抽取牵着鼻子走，且无法穷尽——
下一个 session 抽取可能吐 ``symptom.shiver``，键桥永远补不完）。

本模块在**抽取产出之后、落库之前**立一道确定性闸门：

- 合法 fact_key 集合 = ``COMPLETENESS_DIMENSION_RULES`` canonical ``fact_keys``
  + ``COMPLETENITY_AUXILIARY_FACT_DIMENSIONS`` 辅助键
  + ``COMPLETENITY_CONFLICT_RULES`` 冲突键
  + ``DIMENSION_KEYSETS`` 派生/子前缀键
  + 五个 ``safety.*_status`` 维度名（安全维度名本身是合法 observation 键，由 SafetyFactAssertion
    路径而非 extraction reducer 路径产生，这里只占位以防误拒）。
- **ADD** 越界键 → reject（这条 observation 不进 delta，医生该轮这条键丢失；下一轮若模型产出
  正确键仍能补救）。
- **CORRECT/RETRACT** 越界键 → 若其 ``target_observation_id`` 指向当前 active 事实，**降级为
  伪 RETRACT** 把那条历史畸键从 state 清掉（修复历史脏数据，这正是 d449735a 需要的——它的
  ``symptom.cold_heat`` 已落库为 active，下一轮抽取若产出 correction 就能被此分支清掉）；
  否则 reject。

**reject 不可静默吞掉**——``filter_legal_observations`` 返回 ``(kept, rejected)``，``rejected``
带 ``fact_key / operation / reason / target_observation_id``，调用方写进 claim
``intermediate_payload["extraction"]["rejected_observations"]`` 留痕，故障可观测（与方案 D5
"硬约束失败可观测"铁律一致）。

设计约束：

- **只在抽取产出之后加校验，不改抽取 prompt**：方案第 4 节明确不改 ``intake_extraction_v2.jinja2``
  （回归面太大）；本闸门是 deterministic 下游层，不动模型。
- **不动 reducer / schema 层**：``ObservationSchema.fact_key`` 的 pydantic pattern 与 DB 列约束
  保持不变；本闸门在它们之前拦截，reducers/repository 收到的都是合法键。
- **安全维度不走本闸门**：安全维度由 ``safety_profile.collection_status`` 决定，其 observation
  由 SafetyFactAssertion 路径产生，不经过 extraction reducer；这里把 ``safety.*_status`` 列入
  合法集仅作占位，实际不会被 extraction 产出命中（extraction 产出的是 ``patient_safety_delta``）。
- **合法集自动聚合，不手维护**：从 ``COMPLETENESS_DIMENSION_RULES`` / ``DIMENSION_KEYSETS`` 等
  真源表派生，与 D1 单一真源同步，避免脱钩。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.agent_runtime.completeness_policy import (
    COMPLETENESS_AUXILIARY_FACT_DIMENSIONS,
    COMPLETENESS_CONFLICT_RULES,
    COMPLETENESS_DIMENSION_RULES,
)
from app.agent_runtime.intake_dimension_mapping import (
    DIMENSION_KEYSETS,
    normalize_drifted_fact_key,
)
from app.schemas.intake import ObservationDelta, ObservationOperation

#: 五个安全维度的 observation fact_key 名称（占位，实际由 SafetyFactAssertion 路径产生）。
#: 列入合法集以防误拒 extraction 极少产出安全维度的边界情形。
_SAFETY_DIMENSION_FACT_KEYS: frozenset[str] = frozenset(
    {
        "safety.allergy_status",
        "safety.pregnancy_status",
        "safety.lactation_status",
        "safety.major_condition_status",
        "safety.medication_status",
    }
)


def _build_legal_fact_keys() -> frozenset[str]:
    """Aggregate the legal fact_key jurisdiction from all single-source truth tables.

    合法集 = canonical completeness rule fact_keys + auxiliary fact keys + conflict
    rule fact_keys + DIMENSION_KEYSETS union + safety dimension names. 与 D1 键桥同
    真源，不手维护。
    """

    legal: set[str] = set()
    for rule in COMPLETENESS_DIMENSION_RULES.values():
        legal.update(rule.fact_keys)
    for fact_key, _dimension in COMPLETENESS_AUXILIARY_FACT_DIMENSIONS:
        legal.add(fact_key)
    for conflict_rule in COMPLETENESS_CONFLICT_RULES:
        legal.update(conflict_rule.fact_keys)
    for keyset in DIMENSION_KEYSETS.values():
        legal.update(keyset)
    legal.update(_SAFETY_DIMENSION_FACT_KEYS)
    return frozenset(legal)


#: 全量合法 fact_key 集合（模块级常量，import 时一次性聚合）。
LEGAL_FACT_KEYS: frozenset[str] = _build_legal_fact_keys()

#: reject 原因枚举（写入 claim 留痕，便于排障区分）。
RejectionReason = Literal[
    "fact_key_outside_jurisdiction",  # 键不在合法集内
    "correct_target_missing",  # CORRECT 越界键但 target 不在 active，无法降级 retract
    "value_conflicts_active_fact",  # 2.8 ADD 与活跃事实同键不同值（模型重复提取漂移）
]


@dataclass(frozen=True, slots=True)
class RejectedObservation:
    """被 E1 闸门 reject 的 observation 留痕记录。"""

    fact_key: str
    operation: str
    reason: RejectionReason
    target_observation_id: str | None
    value_preview: str  # 截断 80 字符，仅供排障，不含 PII 处理（extraction 已脱敏）


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    """E1 闸门对越界畸键归一化（fact_key 改写）的留痕记录。

    抽取漂移出的越界 ADD 键若命中 :data:`DERIVED_KEY_NORMALIZATION`，E1 不 reject，而是把
    ``fact_key`` 改写为右侧 canonical/派生合法键后透传落库（见 trigger session b7bdf5ab：
    医生答「怕冷，微微发热」抽成 ``symptom.chills``/``symptom.fever`` 若直接 reject 则寒热维度
    永远采不到键 → 死循环；归一为 ``present_illness.chills``/``present_illness.fever`` 落库，
    D1 派生覆盖即认寒热维度 covered）。

    留痕字段与 :class:`RejectedObservation` 对称，加 ``normalized_fact_key`` 标记归一到哪：
    - ``fact_key``：抽取产出的原始越界键（漂移形态，审计用它回溯模型行为）。
    - ``normalized_fact_key``：归一后的合法 fact_key（实际落库键）。
    - 「归一只改键名、不改 value/evidence/operation」体现在：调用方据此重写 observation 的
      ``fact_key`` 字段，其余字段原样保留；故留痕不携带 value（reject 留痕带 value_preview 是为
      了排障「丢了什么」，归一留痕键名已足够知道「漂成什么 + 去到哪」，不重复截断 value）。
    """

    fact_key: str
    normalized_fact_key: str
    operation: str


@dataclass(frozen=True, slots=True)
class ObservationFilterResult:
    """``filter_legal_observations`` 的返回：保留的 observations + 被拒/归一的留痕。"""

    kept: tuple[ObservationDelta, ...]
    rejected: tuple[RejectedObservation, ...]
    #: 因 CORRECT/RETRACT 降级为伪 RETRACT 而生成的修正 observations（target 命中 active 畸键时）。
    #: 调用方应把 ``kept + downgraded`` 一起喂给 delta 构造。
    downgraded: tuple[ObservationDelta, ...]
    #: 因越界 ADD 键命中 :data:`DERIVED_KEY_NORMALIZATION` 而被「键名改写后透传」的留痕记录。
    #: 调用方据此把 ``kept`` 中被改写的 observation 写进 claim intermediate_payload（与 rejected
    #: 同管道，可观测「这条 observation 原本漂成什么、被归一到哪个 canonical 键落库」）。
    normalized: tuple[NormalizedObservation, ...]


def _value_preview(value: object) -> str:
    if value is None:
        return "<none>"
    text = value if isinstance(value, str) else repr(value)
    return text[:80]


def _is_legal(fact_key: str) -> bool:
    return fact_key in LEGAL_FACT_KEYS


def filter_legal_observations(
    observations: Sequence[ObservationDelta],
    *,
    active_observation_ids_by_fact_key: dict[str, frozenset[str]] | None = None,
) -> ObservationFilterResult:
    """Filter extraction-output observations by fact_key legality (E1 gate).

    在抽取产出之后、落库之前调用。返回 ``(kept, rejected, downgraded, normalized)``：

    - ``kept``：合法 ADD/CORRECT/RETRACT observations，原样透传给 delta 构造。其中越界 ADD 键
      若命中 :data:`DERIVED_KEY_NORMALIZATION`，会被**改写 fact_key 为canonical/派生合法键**
      再放进 ``kept``（归一只改键名，value/evidence/operation 原样保留），并在 ``normalized``
      里留痕。
    - ``rejected``：越界且无法归一/降级的 observations，留痕写 claim。
    - ``downgraded``：CORRECT/RETRACT 越界键、且其 ``target_observation_id`` 命中当前 active
      事实的——降级为伪 RETRACT（用原 target id、清空 value），用于清掉历史畸键脏数据。
      调用方应把 ``kept + downgraded`` 一起喂给 delta。
    - ``normalized``：越界 ADD 键命中归一表、被键名改写后透传的留痕（原始键 → 归一后键）。
      调用方据此写 claim intermediate_payload（与 rejected 同管道）。

    参数 ``active_observation_ids_by_fact_key``：``{fact_key -> frozenset(observation_id str)}``
    当前 state 里 active 事实的索引，用于判断 CORRECT/RETRACT 的 target 是否命中历史畸键。
    缺省 None 时降级分支不生效（CORRECT/RETRACT 越界键一律 reject）——留给无 state 的纯函数
    单测。
    """

    active_index: dict[str, frozenset[str]] = active_observation_ids_by_fact_key or {}
    kept: list[ObservationDelta] = []
    rejected: list[RejectedObservation] = []
    downgraded: list[ObservationDelta] = []
    normalized: list[NormalizedObservation] = []

    for item in observations:
        if _is_legal(item.fact_key):
            kept.append(item)
            continue

        # 越界键处置：
        # 1) ADD 命中归一表 → 改写 fact_key 为 canonical/派生合法键透传（不 reject 丢失，避免
        #    trigger session b7bdf5ab「畸键全 reject → 维度永采不到键 → 死循环」）。
        # 2) ADD 不命中归一表 → reject 留痕。
        # 3) CORRECT/RETRACT 越界 → 看 target 是否命中 active 畸键，命中则降级伪 RETRACT。
        operation = item.operation
        if operation is ObservationOperation.ADD:
            normalized_key = normalize_drifted_fact_key(item.fact_key)
            if normalized_key is not None:
                # 归一：只改键名，其余字段（value/normalized_value/source/confidence/operation）
                # 原样保留。归一目标键必然在 :data:`DIMENSION_KEYSETS` 内（设计边界），故此时
                # 落库键对下游 reducer/safety 仍是已知合法键。
                kept.append(
                    ObservationDelta(
                        fact_key=normalized_key,
                        value=item.value,
                        normalized_value=item.normalized_value,
                        source_message_id=item.source_message_id,
                        confidence=item.confidence,
                        operation=item.operation,
                        target_observation_id=item.target_observation_id,
                    )
                )
                normalized.append(
                    NormalizedObservation(
                        fact_key=item.fact_key,
                        normalized_fact_key=normalized_key,
                        operation=operation.value,
                    )
                )
            else:
                rejected.append(
                    RejectedObservation(
                        fact_key=item.fact_key,
                        operation=operation.value,
                        reason="fact_key_outside_jurisdiction",
                        target_observation_id=None,
                        value_preview=_value_preview(item.value),
                    )
                )
            continue

        # CORRECT / RETRACT 越界键：尝试降级为对历史畸键的伪 RETRACT。
        target_id = item.target_observation_id
        if target_id is None:
            # 无 target 的 correct/retract 本就非法（schema 层会再拦），这里先 reject 留痕。
            rejected.append(
                RejectedObservation(
                    fact_key=item.fact_key,
                    operation=operation.value,
                    reason="correct_target_missing",
                    target_observation_id=None,
                    value_preview=_value_preview(item.value),
                )
            )
            continue

        target_id_str = str(target_id)
        active_ids = active_index.get(item.fact_key, frozenset())
        if target_id_str in active_ids:
            # target 命中 active 畸键 → 降级为伪 RETRACT 清掉它。保留原 target id，清空 value。
            downgraded.append(
                ObservationDelta(
                    fact_key=item.fact_key,
                    value=None,
                    normalized_value=None,
                    source_message_id=item.source_message_id,
                    confidence=item.confidence,
                    operation=ObservationOperation.RETRACT,
                    target_observation_id=target_id,
                )
            )
        else:
            rejected.append(
                RejectedObservation(
                    fact_key=item.fact_key,
                    operation=operation.value,
                    reason="correct_target_missing",
                    target_observation_id=target_id_str,
                    value_preview=_value_preview(item.value),
                )
            )

    return ObservationFilterResult(
        kept=tuple(kept),
        rejected=tuple(rejected),
        downgraded=tuple(downgraded),
        normalized=tuple(normalized),
    )


def rejected_observations_to_payload(
    rejected: Iterable[RejectedObservation],
) -> list[dict[str, object]]:
    """Serialize rejected observations to a JSON-safe list for claim intermediate_payload.

    供 ``_save_intermediate`` 写入 ``intermediate_payload["extraction"]
    ["rejected_observations"]``。只含排障必需字段，不含原始 value 全量（PII 已在 extraction
    层脱敏，这里再截断一层）。
    """

    return [
        {
            "fact_key": item.fact_key,
            "operation": item.operation,
            "reason": item.reason,
            "target_observation_id": item.target_observation_id,
            "value_preview": item.value_preview,
        }
        for item in rejected
    ]


def normalized_observations_to_payload(
    normalized: Iterable[NormalizedObservation],
) -> list[dict[str, object]]:
    """Serialize normalized observations to a JSON-safe list for claim intermediate_payload.

    供 ``_save_intermediate`` 写入 ``intermediate_payload["extraction"]
    ["normalized_observations"]``——越界畸键被键名改写后透传的留痕（``fact_key`` 漂移形态 →
    ``normalized_fact_key`` canonical/派生键）。与 :func:`rejected_observations_to_payload`
    同管道，归一路径可观测（不静默改键，事后能回溯「模型吐了什么畸键、被纠正成什么合法键」）。
    record 不携带 value：reject 留痕带 ``value_preview`` 是为排障「丢了什么」，归一留痕键名已
    足够表达「漂成什么 + 去到哪」，重复截断 value 无信息增益。
    """

    return [
        {
            "fact_key": item.fact_key,
            "normalized_fact_key": item.normalized_fact_key,
            "operation": item.operation,
        }
        for item in normalized
    ]
