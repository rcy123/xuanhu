"""Agent 基础设施包。"""

from app.agents.base import AgentResult, BaseAgent, BaseAgentImpl
from app.agents.errors import AgentRunError
from app.agents.inquiry import InquiryAgent, merge_inquiry_output_to_state
from app.agents.intake_extraction import (
    IntakeBoundaryFailureCode,
    IntakeExecutionResult,
    IntakeExecutionStatus,
    build_intake_agent_spec,
    build_intake_context,
    execute_intake_extraction,
)
from app.agents.modification import (
    ModificationAgent,
    merge_modified_formula_result_to_state,
)
from app.agents.prescription import PrescriptionAgent, merge_formula_result_to_state
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
from app.agents.registry import AgentRegistry
from app.agents.safety import SafetyAgent
from app.agents.sufficiency import SufficiencyAgent, merge_sufficiency_report_to_state
from app.agents.supervisor import Supervisor, SupervisorResult
from app.agents.syndrome import SyndromeAgent, merge_syndrome_result_to_state

__all__ = [
    "AgentRegistry",
    "AgentResult",
    "AgentRunError",
    "BaseAgent",
    "BaseAgentImpl",
    "InquiryAgent",
    "IntakeBoundaryFailureCode",
    "IntakeExecutionResult",
    "IntakeExecutionStatus",
    "build_intake_agent_spec",
    "build_intake_context",
    "execute_intake_extraction",
    "merge_formula_result_to_state",
    "merge_inquiry_output_to_state",
    "merge_modified_formula_result_to_state",
    "merge_sufficiency_report_to_state",
    "merge_syndrome_result_to_state",
    "ModificationAgent",
    "PrescriptionAgent",
    "PromptLoader",
    "PromptTemplate",
    "QUESTION_COMPOSER_FAILURE_POLICY",
    "QUESTION_COMPOSER_TOOL_PERMISSIONS",
    "QUESTION_COMPOSER_VERIFIER_CHAIN",
    "QUESTION_TEMPLATES",
    "FrozenQuestionTemplateRegistry",
    "QuestionTemplate",
    "build_question_composer_agent_spec",
    "build_question_context",
    "compose_question",
    "validate_single_question_text",
    "SafetyAgent",
    "SufficiencyAgent",
    "Supervisor",
    "SupervisorResult",
    "SyndromeAgent",
]
