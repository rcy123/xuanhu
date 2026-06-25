"""ORM 模型 — 业务表。

各模块:
- consult.py  : 会话表、消息表
- agent.py    : Agent 运行表、证据表
- safety.py   : 安全规则运行表
- review.py   : 医师确认表、病历表
- audit.py    : 审计事件表
- knowledge.py: 知识库来源、方剂、中药、剂量单位、穴位、理论/医案、chunk 表

所有模型继承自 ``app.db.base.Base``，使用 ``UUIDPrimaryKeyMixin``
和 ``TimestampMixin`` 提供通用字段。
"""

from app.models.agent import AgentEvidence, AgentRun
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.knowledge import (
    Acupoint,
    DosageUnit,
    Formula,
    Herb,
    KnowledgeChunk,
    KnowledgeSource,
    TheoryCase,
)
from app.models.review import DoctorReview, MedicalRecord
from app.models.safety import SafetyRuleRun

__all__ = [
    "ConsultSession",
    "ConsultMessage",
    "AgentRun",
    "AgentEvidence",
    "SafetyRuleRun",
    "DoctorReview",
    "MedicalRecord",
    "AuditEvent",
    "KnowledgeSource",
    "Formula",
    "Herb",
    "DosageUnit",
    "Acupoint",
    "TheoryCase",
    "KnowledgeChunk",
]
