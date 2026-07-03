"""安全规则引擎包 —— 确定性规则检查，不依赖 LLM。"""

from app.safety.engine import SafetyRuleEngine
from app.safety.rule_version import SAFETY_RULE_VERSION

__all__ = [
    "SafetyRuleEngine",
    "SAFETY_RULE_VERSION",
]
