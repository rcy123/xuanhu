"""日志 PHI 脱敏过滤器 — 阶段 2（T2.5）。

方案见 ``docs/04_生产环境加固/02-PHI访问控制与日志脱敏.md`` §3.2：

1. **过滤器层（纵深防御）**：挂在 logging 上的 ``PHIRedactingFilter``，对
   每条日志 message 做字段名正则替换（``session_id=...`` /
   ``patient_*=...`` 等 → ``[REDACTED]``）。防止新增日志漏改。
2. **调用点层（真正可靠）**：业务代码只记摘要，不把 ``model_dump()`` /
   业务对象 str 化入参（既有约定，见各 service 审计事件走 AuditEvent 表）。

关键词/模式表以字段名驱动，避免对自由文本（如症状原文）做不可靠的
关键词匹配造成误伤。匹配是**替换而非仅标记**——正式态直接阻断 PHI 出域。
"""

from __future__ import annotations

import logging
import re

# ---------------------------------------------------------------------------
# PHI 模式表：已知敏感字段的 key=value / key:value / 'key': 'value' 形态
# ---------------------------------------------------------------------------

# UUID 形态的 session_id（带/不带引号分隔）
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# 每个模式组：(正则, 替换)。捕获组 1 保留字段名前缀，值整体替换。
_PHI_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # session_id：UUID 或任意短值（容忍 'session_id': '...' 引号形态）
    (re.compile(r"['\"]?session_id['\"]?\s*[=:]\s*['\"]?" + _UUID), r"session_id=[REDACTED]"),
    (re.compile(r"['\"]?session_id['\"]?\s*[=:]\s*['\"]?[^,\s'\"]{1,128}"), r"session_id=[REDACTED]"),
    # 患者身份 / 问诊内容字段
    (re.compile(r"(patient_ref\s*[=:]\s*['\"]?)[^,\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"(patient_info\s*[=:]\s*['\"]?)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(patient_name\s*[=:]\s*['\"]?)[^,\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"(chief_complaint\s*[=:]\s*['\"]?)[^,\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"(症状原文\s*[=:：]\s*)[^\s,，]+"), r"\1[REDACTED]"),
    (re.compile(r"(姓名\s*[=:：]\s*)[^\s,，]+"), r"\1[REDACTED]"),
    # 手机号 / 身份证号（15/18 位）
    (re.compile(r"1[3-9]\d{9}"), "[REDACTED]"),
    (re.compile(r"\d{17}[\dXx]"), "[REDACTED]"),
    # JSON 形态：'field': 'value'
    (re.compile(r"(['\"])(patient_ref|patient_info|patient_name|chief_complaint)['\"]\s*:\s*['\"][^'\"]{1,256}['\"]"),
     r"\1\2\1: \"[REDACTED]\""),
]


def redact_phi(message: str) -> str:
    """对单条日志消息做 PHI 正则替换；无命中则原样返回。"""
    redacted = message
    for pattern, replacement in _PHI_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class PHIRedactingFilter(logging.Filter):
    """挂在 logger 上的 PHI 脱敏过滤器。

    命中时把 ``record.msg`` 替换为脱敏后的消息并清空 args，保证后续所有
    handler（文件、控制台、测试 caplog）都只看到脱敏文本。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = redact_phi(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_phi_redaction(*, root: bool = True) -> None:
    """把 PHIRedactingFilter 挂到 ``xuanhu`` 命名空间（与 root，可选）。

    幂等：同一进程重复调用只挂一个实例。``root=True`` 同时挂 root logger，
    覆盖 uvicorn 等第三方日志路径。
    """
    names = ["xuanhu"]
    if root:
        names.append("root")
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, PHIRedactingFilter) for f in logger.filters):
            logger.addFilter(PHIRedactingFilter())


__all__ = ["PHIRedactingFilter", "install_phi_redaction", "redact_phi"]
