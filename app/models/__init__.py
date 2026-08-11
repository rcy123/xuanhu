"""ORM 模型 — 业务表与知识库表。

导入此模块即注册所有模型到 ``Base.metadata``，
Alembic 在 ``env.py`` 中依赖此行为。
"""

from app.models.agent import AgentEvidence, AgentRun
from app.models.async_command import ASYNC_COMMAND_OPERATIONS, AsyncCommand
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
    SafetyFactAssertion,
    SafetyFactTransition,
    SafetyProfile,
)
from app.models.http_command import HttpCommandClaim
from app.models.knowledge import (
    Acupoint,
    DosageUnit,
    Formula,
    Herb,
    KnowledgeChunk,
    KnowledgeSource,
    TheoryCase,
)
from app.models.model_run_audit import ModelRunAudit
from app.models.review import DoctorReview, MedicalRecord
from app.models.safety import SafetyRuleRun

__all__ = [
    "Acupoint",
    "AgentEvidence",
    "AgentRun",
    "AsyncCommand",
    "ASYNC_COMMAND_OPERATIONS",
    "AuditEvent",
    "ConsultMessage",
    "ConsultSession",
    "DomainCommandCommit",
    "Observation",
    "OutboxEvent",
    "SafetyFactAssertion",
    "SafetyFactTransition",
    "SafetyProfile",
    "ArtifactRevision",
    "ArtifactRevisionPayload",
    "GateResult",
    "GraphRun",
    "GraphRunStep",
    "HttpCommandClaim",
    "DoctorReview",
    "DosageUnit",
    "Formula",
    "Herb",
    "KnowledgeChunk",
    "KnowledgeSource",
    "MedicalRecord",
    "ModelRunAudit",
    "SafetyRuleRun",
    "TheoryCase",
]
