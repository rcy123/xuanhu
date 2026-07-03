"""安全规则硬编码数据集。

包含十八反配伍表、十九畏配伍表、同类展开组、毒性/峻烈药材清单。
这些是中医固定知识，不依赖数据库导入质量，按 §4/§5/§11 定义。

所有表均为 ``frozenset`` / 不可变结构，避免运行期被误改。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 十八反配伍表（§4.2）
# ---------------------------------------------------------------------------

# 原始三元组：(drug_a, drug_b, source)
_EIGHTEEN_INCOMPATIBILITY_RAW: tuple[tuple[str, str, str], ...] = (
    # 甘草组：甘草反甘遂、大戟、海藻、芫花
    ("甘草", "甘遂", "《神农本草经》"),
    ("甘草", "大戟", "《神农本草经》"),
    ("甘草", "海藻", "《神农本草经》"),
    ("甘草", "芫花", "《神农本草经》"),
    # 乌头组：川乌/草乌/附子 反 半夏、瓜蒌、川贝母、白蔹、白及
    ("川乌", "半夏", "《神农本草经》"),
    ("川乌", "瓜蒌", "《神农本草经》"),
    ("川乌", "川贝母", "《神农本草经》"),
    ("川乌", "白蔹", "《神农本草经》"),
    ("川乌", "白及", "《神农本草经》"),
    ("草乌", "半夏", "《神农本草经》"),
    ("草乌", "瓜蒌", "《神农本草经》"),
    ("草乌", "川贝母", "《神农本草经》"),
    ("草乌", "白蔹", "《神农本草经》"),
    ("草乌", "白及", "《神农本草经》"),
    ("附子", "半夏", "《神农本草经》"),
    ("附子", "瓜蒌", "《神农本草经》"),
    ("附子", "川贝母", "《神农本草经》"),
    ("附子", "白蔹", "《神农本草经》"),
    ("附子", "白及", "《神农本草经》"),
    # 藜芦组：藜芦反人参、南沙参、丹参、玄参、细辛、芍药
    # 含党参（保守处理：藜芦反人参也反党参，§4.2 note）
    ("藜芦", "人参", "《神农本草经》"),
    ("藜芦", "党参", "《神农本草经》"),
    ("藜芦", "南沙参", "《神农本草经》"),
    ("藜芦", "丹参", "《神农本草经》"),
    ("藜芦", "玄参", "《神农本草经》"),
    ("藜芦", "细辛", "《神农本草经》"),
    ("藜芦", "赤芍", "《神农本草经》"),
)

# 双向匹配表：键为排序后的药对元组，值为 source
EIGHTEEN_INCOMPATIBILITY_TABLE: dict[tuple[str, str], str] = {}
for _a, _b, _src in _EIGHTEEN_INCOMPATIBILITY_RAW:
    EIGHTEEN_INCOMPATIBILITY_TABLE[(_a, _b)] = _src
    EIGHTEEN_INCOMPATIBILITY_TABLE[(_b, _a)] = _src

# ---------------------------------------------------------------------------
# 十九畏配伍表（§5.2）
# ---------------------------------------------------------------------------

_NINETEEN_FEARS_RAW: tuple[tuple[str, str, str], ...] = (
    ("硫黄", "朴硝", "《药性论》"),
    ("水银", "砒霜", "《药性论》"),
    ("狼毒", "密陀僧", "《药性论》"),
    ("巴豆", "牵牛子", "《药性论》"),
    ("丁香", "郁金", "《药性论》"),
    ("牙硝", "三棱", "《药性论》"),
    ("川乌", "犀角", "《药性论》"),
    ("草乌", "水牛角", "《药性论》"),
    ("人参", "五灵脂", "《药性论》"),
    ("官桂", "赤石脂", "《药性论》"),
)

NINETEEN_FEARS_TABLE: dict[tuple[str, str], str] = {}
for _a, _b, _src in _NINETEEN_FEARS_RAW:
    NINETEEN_FEARS_TABLE[(_a, _b)] = _src
    NINETEEN_FEARS_TABLE[(_b, _a)] = _src

# ---------------------------------------------------------------------------
# 同类展开组（§4.5）
# ---------------------------------------------------------------------------

ACONITUM_GROUP: frozenset[str] = frozenset({"川乌", "草乌", "附子", "天雄", "乌头"})
FRITILLARIA_GROUP: frozenset[str] = frozenset(
    {"川贝母", "浙贝母", "平贝母", "伊贝母", "湖北贝母", "贝母"}
)
TRICHOSANTHES_GROUP: frozenset[str] = frozenset({"瓜蒌", "瓜蒌皮", "瓜蒌子", "天花粉"})
ADENOPHORA_GROUP: frozenset[str] = frozenset({"南沙参", "北沙参", "沙参"})
PEONY_GROUP: frozenset[str] = frozenset({"赤芍", "白芍", "芍药"})
# 藜芦反人参，部分学派也反党参，MVP 保守处理：党参与人参同列为藜芦反药
CODONOPSIS_ALIAS: frozenset[str] = frozenset({"党参"})

# 十九畏中肉桂/官桂保守展开（§5.2 note）
CINNAMOM_GROUP: frozenset[str] = frozenset({"官桂", "肉桂", "桂枝"})
# 牙硝/朴硝/芒硝同源保守处理
NITRE_GROUP: frozenset[str] = frozenset({"朴硝", "牙硝", "芒硝", "玄明粉"})
# 巴豆保守展开（含巴豆霜）
CROTON_GROUP: frozenset[str] = frozenset({"巴豆", "巴豆霜"})
# 犀角代用水牛角（§5.2 已含草乌-水牛角；川乌-犀角保守视为与川乌-水牛角同类）
RHINO_SUBSTITUTE_GROUP: frozenset[str] = frozenset({"犀角", "水牛角"})

#: 同类展开映射：标准名 -> 该药所属的同类集合（用于命中检查时把组内其他药也并入候选）
_EXPANSION_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("aconitum", ACONITUM_GROUP),
    ("fritillaria", FRITILLARIA_GROUP),
    ("trichosanthes", TRICHOSANTHES_GROUP),
    ("adenophora", ADENOPHORA_GROUP),
    ("peony", PEONY_GROUP),
    ("codonopsis", CODONOPSIS_ALIAS),
    ("cinnamom", CINNAMOM_GROUP),
    ("nitre", NITRE_GROUP),
    ("croton", CROTON_GROUP),
    ("rhino", RHINO_SUBSTITUTE_GROUP),
)


def expand_group(herb_name: str) -> frozenset[str]:
    """返回 herb_name 所属的同类集合（含自身）。

    若 herb_name 不属于任何展开组，返回仅含自身的 frozenset。
    """
    for _label, group in _EXPANSION_GROUPS:
        if herb_name in group:
            return group
    return frozenset({herb_name})


# ---------------------------------------------------------------------------
# 毒性 / 峻烈药材清单（§7.4 / §11.4）
# ---------------------------------------------------------------------------

TOXIC_OR_STRONG_HERBS: frozenset[str] = frozenset(
    {
        "附子",
        "川乌",
        "草乌",
        "半夏",
        "天南星",
        "马钱子",
        "巴豆",
        "斑蝥",
        "砒霜",
        "水银",
        "甘遂",
        "大戟",
        "芫花",
        "商陆",
        "千金子",
        "细辛",
        "洋金花",
        "雷公藤",
        "全蝎",
        "蜈蚣",
        "朱砂",
        "雄黄",
        "巴豆霜",
    }
)


def is_toxic_or_strong_herb(herb_name: str, *, herb_metadata_toxicity: str | None = None) -> bool:
    """判断是否为毒性/峻烈药材。

    优先使用固定清单；如 herbs.metadata.toxicity 写入了毒性标签，也可作为补充依据
    （值为 ``toxic`` / ``strong`` / ``high`` 时视为毒性）。
    """
    if herb_name in TOXIC_OR_STRONG_HERBS:
        return True
    return herb_metadata_toxicity in ("toxic", "strong", "high")


__all__ = [
    "ACONITUM_GROUP",
    "ADENOPHORA_GROUP",
    "CINNAMOM_GROUP",
    "CODONOPSIS_ALIAS",
    "CROTON_GROUP",
    "EIGHTEEN_INCOMPATIBILITY_TABLE",
    "FRITILLARIA_GROUP",
    "NITRE_GROUP",
    "NINETEEN_FEARS_TABLE",
    "PEONY_GROUP",
    "RHINO_SUBSTITUTE_GROUP",
    "TOXIC_OR_STRONG_HERBS",
    "TRICHOSANTHES_GROUP",
    "expand_group",
    "is_toxic_or_strong_herb",
]
