"""共享枚举类型。

这些枚举只定义跨 API、Agent、Supervisor、Safety 复用的稳定字面值，
不包含任何业务执行逻辑。
"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """问诊主流程阶段。"""

    INQUIRY = "inquiry"
    SUFFICIENCY = "sufficiency"
    SYNDROME = "syndrome"
    PRESCRIPTION = "prescription"
    MODIFICATION = "modification"
    SAFETY = "safety"
    REVIEW = "review"
    RECORD = "record"
    DONE = "done"
    BLOCKED = "blocked"


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


class RollbackTarget(StrEnum):
    """安全审核建议回退目标。"""

    PRESCRIPTION = "prescription"
    MODIFICATION = "modification"
    NONE = "none"


def is_pregnancy_risk_status(status: PregnancyStatus | str | None) -> bool:
    """判断是否应按妊娠禁忌硬规则处理。"""
    return status in {PregnancyStatus.PREGNANT, PregnancyStatus.POSSIBLE, "pregnant", "possible"}
