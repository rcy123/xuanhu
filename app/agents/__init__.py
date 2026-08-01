"""Agent 基础设施包。

3d: legacy agents(Supervisor/registry/inquiry/sufficiency/syndrome/safety/
modification/prescription/record_agent/base)已随 legacy 路径下线删除;
本包仅保留统一后端(langgraph)使用的抽取/分类/措辞/辨证/开方 agent。
"""

from app.agents.complaint_classifier import (
    COMPLAINT_CLASSIFIER_AGENT_VERSION,
    COMPLAINT_CLASSIFIER_PROMPT_VERSION,
    ComplaintClassificationOutput,
    build_complaint_classifier_agent_spec,
    build_complaint_classifier_context,
    execute_complaint_classification,
)
from app.agents.errors import AgentRunError
from app.agents.intake_extraction import (
    IntakeBoundaryFailureCode,
    IntakeExecutionResult,
    IntakeExecutionStatus,
    build_intake_agent_spec,
    build_intake_context,
    execute_intake_extraction,
)
from app.agents.prompt_loader import PromptLoader, PromptTemplate
from app.agents.question_composer import (
    QUESTION_COMPOSER_FAILURE_POLICY,
    QUESTION_COMPOSER_TOOL_PERMISSIONS,
    QUESTION_COMPOSER_VERIFIER_CHAIN,
    QUESTION_TEMPLATES,
    FrozenQuestionTemplateRegistry,
    QuestionTemplate,
    build_question_composer_agent_spec,
    build_question_context,
    compose_question,
    validate_single_question_text,
)

__all__ = [
    "AgentRunError",
    "COMPLAINT_CLASSIFIER_AGENT_VERSION",
    "COMPLAINT_CLASSIFIER_PROMPT_VERSION",
    "ComplaintClassificationOutput",
    "FrozenQuestionTemplateRegistry",
    "IntakeBoundaryFailureCode",
    "IntakeExecutionResult",
    "IntakeExecutionStatus",
    "PromptLoader",
    "PromptTemplate",
    "QUESTION_COMPOSER_FAILURE_POLICY",
    "QUESTION_COMPOSER_TOOL_PERMISSIONS",
    "QUESTION_COMPOSER_VERIFIER_CHAIN",
    "QUESTION_TEMPLATES",
    "QuestionTemplate",
    "build_complaint_classifier_agent_spec",
    "build_complaint_classifier_context",
    "build_intake_agent_spec",
    "build_intake_context",
    "build_question_composer_agent_spec",
    "build_question_context",
    "compose_question",
    "execute_complaint_classification",
    "execute_intake_extraction",
    "validate_single_question_text",
]
