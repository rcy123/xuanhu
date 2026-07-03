"""安全规则版本号。

每次规则变更（新增规则、修改严重度、修正换算系数）时按 §2.1 规范递增。
版本号写入 ``safety_rule_runs.rule_version``，便于复盘与回滚。
"""

from __future__ import annotations

SAFETY_RULE_VERSION = "v1.0.0"

__all__ = ["SAFETY_RULE_VERSION"]
