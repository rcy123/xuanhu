"""ORM 模型 — 业务表与知识库表。

导入此模块即注册所有模型到 ``Base.metadata``，
Alembic 在 ``env.py`` 中依赖此行为。
"""

from app.models.agent import AgentEvidence, AgentRun
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    ArtifactRevision,
    ArtifactRevisionPayload,
    DomainCommandCommit,
    GateResult,
    GraphRun,
    GraphRunStep,
    Observation,
    OutboxEvent,
    SafetyProfile,
)
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
    "Acupoint",
    "AgentEvidence",
    "AgentRun",
    "AuditEvent",
    "ConsultMessage",
    "ConsultSession",
    "DomainCommandCommit",
    "Observation",
    "OutboxEvent",
    "SafetyProfile",
    "ArtifactRevision",
    "ArtifactRevisionPayload",
    "GateResult",
    "GraphRun",
    "GraphRunStep",
    "DoctorReview",
    "DosageUnit",
    "Formula",
    "Herb",
    "KnowledgeChunk",
    "KnowledgeSource",
    "MedicalRecord",
    "SafetyRuleRun",
    "TheoryCase",
]
