"""共享枚举类型。

这些枚举只定义跨 API、Agent、Safety 复用的稳定字面值，
不包含任何业务执行逻辑。

3c/3d: legacy `Stage`(10 态)已随 legacy 路径下线删除——统一编排模型为
langgraph 的 `command`(message/advance/review/recover)+ 粗 stage
(`session.current_stage`)+ 子图 gate/route 细态,见
`app/agent_runtime/commands.py`。
"""

from __future__ import annotations

from enum import StrEnum


class ReviewAction(StrEnum):
    """医师复核动作。"""

    CONFIRM = "confirm"
    MODIFY = "modify"
    REJECT = "reject"
    REQUEST_MORE_INFO = "request_more_info"


class Severity(StrEnum):
    """安全问题严重度。"""

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    BLOCKER = "blocker"


class PregnancyStatus(StrEnum):
    """妊娠/哺乳状态。

    `POSSIBLE` 必须按妊娠同等严格处理，供安全规则硬约束使用。
    """

    UNKNOWN = "unknown"
    NO = "no"
    PREGNANT = "pregnant"
    POSSIBLE = "possible"
    LACTATING = "lactating"


class Gender(StrEnum):
    """患者性别。"""

    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class RecoveryStatus(StrEnum):
    """会话恢复状态。"""

    NORMAL = "normal"
    RECOVERING = "recovering"
    MANUAL_REQUIRED = "manual_required"


class MenopauseStatus(StrEnum):
    """绝经状态。"""

    UNKNOWN = "unknown"
    NO = "no"
    YES = "yes"


class ModificationAction(StrEnum):
    """方药加减动作。"""

    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"
    ADJUST = "adjust"


class SafetyIssueType(StrEnum):
    """安全规则问题类型。"""

    EIGHTEEN_INCOMPATIBILITIES = "eighteen_incompatibilities"
    NINETEEN_FEARS = "nineteen_fears"
    PREGNANCY = "pregnancy"
    DOSE_LIMIT = "dose_limit"
    UNIT_CONVERSION = "unit_conversion"
    ALLERGY = "allergy"
    COMBINATION = "combination"
    CAUTION = "caution"
    # R4-A：患者条件与药材 contraindications 精确匹配（BLOCKER）。
    HERB_CONTRAINDICATION = "herb_contraindication"
    # R4-A：患者用药存在但知识库无权威相互作用数据时的覆盖率门禁（HIGH）。
    MEDICATION_INTERACTION_COVERAGE = "medication_interaction_coverage"
    # R4-B：患者条件（major + special）合并条目数超过有界上限时，无法完整审核
    # 药材禁忌的 fail-closed 覆盖率门禁（HIGH，固定文本，不含条件名）。
    PATIENT_CONTEXT_COVERAGE = "patient_context_coverage"
    # R4-B：单味药 contraindications 条目数超过有界上限时，无法确认是否存在禁忌
    # 命中的 fail-closed 覆盖率门禁（HIGH，固定文本，不含原始禁忌条目）。
    HERB_CONTRAINDICATION_COVERAGE = "herb_contraindication_coverage"


class RollbackTarget(StrEnum):
    """安全审核建议回退目标。"""

    PRESCRIPTION = "prescription"
    MODIFICATION = "modification"
    NONE = "none"


def is_pregnancy_risk_status(status: PregnancyStatus | str | None) -> bool:
    """判断是否应按妊娠禁忌硬规则处理。"""
    return status in {PregnancyStatus.PREGNANT, PregnancyStatus.POSSIBLE, "pregnant", "possible"}
