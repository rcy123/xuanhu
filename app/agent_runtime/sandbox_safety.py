"""Offline deterministic safety adapter for the L5 personal-learning sandbox."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import StrEnum
from typing import NoReturn, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

SANDBOX_SUBJECT_SCHEMA_VERSION = "sandbox-safety-subject.v1"
SANDBOX_RULE_BUNDLE_SCHEMA_VERSION = "sandbox-rule-bundle.v1"
SANDBOX_EVALUATION_SCHEMA_VERSION = "sandbox-safety-evaluation.v1"
SANDBOX_RESULT_SCHEMA_VERSION = "sandbox-safety-result.v1"
SANDBOX_ADAPTER_VERSION = "sandbox-safety-adapter.v1"

MAX_FORMULA_ITEMS = 64
MAX_ISSUES = 256
MAX_RULES = 256
MAX_CANONICAL_BYTES = 256 * 1024

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SandboxSafetyDecision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class SandboxSafetySeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SandboxSafetyFailureCode(StrEnum):
    SCHEMA_INVALID = "SANDBOX_SAFETY_SCHEMA_INVALID"
    VERSION_MISMATCH = "SANDBOX_SAFETY_VERSION_MISMATCH"
    DIGEST_MISMATCH = "SANDBOX_SAFETY_DIGEST_MISMATCH"
    LIMIT_EXCEEDED = "SANDBOX_SAFETY_LIMIT_EXCEEDED"
    EVALUATOR_FAILED = "SANDBOX_SAFETY_EVALUATOR_FAILED"
    EVALUATOR_RESULT_INVALID = "SANDBOX_SAFETY_EVALUATOR_RESULT_INVALID"
    EVALUATOR_NONDETERMINISTIC = "SANDBOX_SAFETY_EVALUATOR_NONDETERMINISTIC"
    INTERNAL_FAILURE = "SANDBOX_SAFETY_INTERNAL_FAILURE"


class SandboxSafetyAdapterError(ValueError):
    """A fixed, payload-free, fail-closed adapter error."""

    __slots__ = ("code",)

    def __init__(self, code: SandboxSafetyFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class SandboxFormulaItemV1(_StrictFrozenModel):
    item_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    component: str = Field(min_length=1, max_length=256)
    amount_milliunits: int = Field(ge=1, le=1_000_000_000)
    unit: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)


class SandboxProfileFactV1(_StrictFrozenModel):
    fact_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=4096)


class SandboxRuleParameterV1(_StrictFrozenModel):
    name: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    value: str = Field(min_length=1, max_length=4096)


class SandboxRuleV1(_StrictFrozenModel):
    rule_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_revision: int = Field(ge=1)
    parameters: tuple[SandboxRuleParameterV1, ...]

    @model_validator(mode="after")
    def parameters_are_canonical(self) -> SandboxRuleV1:
        names = tuple(parameter.name for parameter in self.parameters)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("rule parameters must be unique and sorted")
        return self


class SandboxRuleBundleV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(
        default=SANDBOX_RULE_BUNDLE_SCHEMA_VERSION,
        min_length=1,
        max_length=64,
    )
    adapter_version: str = Field(default=SANDBOX_ADAPTER_VERSION, min_length=1, max_length=64)
    rule_bundle_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    )
    rules: tuple[SandboxRuleV1, ...]
    rule_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def rules_are_canonical(self) -> SandboxRuleBundleV1:
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if rule_ids != tuple(sorted(rule_ids)) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rules must be unique and sorted")
        return self

    @classmethod
    def build(
        cls,
        *,
        rule_bundle_version: str,
        rules: Sequence[SandboxRuleV1],
    ) -> SandboxRuleBundleV1:
        canonical_rules = tuple(sorted(tuple(rules), key=lambda rule: rule.rule_id))
        body = _rule_bundle_body(
            sandbox_schema_version=SANDBOX_RULE_BUNDLE_SCHEMA_VERSION,
            adapter_version=SANDBOX_ADAPTER_VERSION,
            rule_bundle_version=rule_bundle_version,
            rules=canonical_rules,
        )
        return cls(
            rule_bundle_version=rule_bundle_version,
            rules=canonical_rules,
            rule_bundle_digest=_canonical_sha256(body),
        )


class SandboxSafetySubjectV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(
        default=SANDBOX_SUBJECT_SCHEMA_VERSION,
        min_length=1,
        max_length=64,
    )
    adapter_version: str = Field(default=SANDBOX_ADAPTER_VERSION, min_length=1, max_length=64)
    test_session_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    domain_state_version: int = Field(ge=1)
    formula_artifact_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    formula_revision: int = Field(ge=1)
    formula_items: tuple[SandboxFormulaItemV1, ...]
    formula_content_digest: str = Field(pattern=_DIGEST_PATTERN)
    profile_artifact_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    profile_revision: int = Field(ge=1)
    profile_facts: tuple[SandboxProfileFactV1, ...]
    profile_content_digest: str = Field(pattern=_DIGEST_PATTERN)
    graph_version: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_bundle_version: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    synthetic_dataset_name: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    synthetic_dataset_version: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    synthetic_dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    dataset_provenance: str = Field(pattern=r"^fixed_fictitious_manual$")
    dataset_usage_scope: str = Field(pattern=r"^sandbox_only$")
    dataset_label_status: str = Field(pattern=r"^not_clinically_adjudicated$")

    @model_validator(mode="after")
    def nested_inputs_are_canonical(self) -> SandboxSafetySubjectV1:
        item_ids = tuple(item.item_id for item in self.formula_items)
        fact_ids = tuple(fact.fact_id for fact in self.profile_facts)
        if item_ids != tuple(sorted(item_ids)) or len(item_ids) != len(set(item_ids)):
            raise ValueError("formula items must be unique and sorted")
        if fact_ids != tuple(sorted(fact_ids)) or len(fact_ids) != len(set(fact_ids)):
            raise ValueError("profile facts must be unique and sorted")
        return self

    @classmethod
    def build(
        cls,
        *,
        test_session_id: str,
        domain_state_version: int,
        formula_artifact_id: str,
        formula_revision: int,
        formula_items: Sequence[SandboxFormulaItemV1],
        profile_artifact_id: str,
        profile_revision: int,
        profile_facts: Sequence[SandboxProfileFactV1],
        graph_version: str,
        rule_bundle_version: str,
        rule_bundle_digest: str,
        synthetic_dataset_name: str,
        synthetic_dataset_version: str,
        dataset_provenance: str,
        dataset_usage_scope: str,
        dataset_label_status: str,
    ) -> SandboxSafetySubjectV1:
        canonical_items = tuple(sorted(tuple(formula_items), key=lambda item: item.item_id))
        canonical_facts = tuple(sorted(tuple(profile_facts), key=lambda fact: fact.fact_id))
        formula_digest = _canonical_sha256(canonical_items)
        profile_digest = _canonical_sha256(canonical_facts)
        dataset_digest = _canonical_sha256(
            {"formula_items": canonical_items, "profile_facts": canonical_facts}
        )
        return cls(
            test_session_id=test_session_id,
            domain_state_version=domain_state_version,
            formula_artifact_id=formula_artifact_id,
            formula_revision=formula_revision,
            formula_items=canonical_items,
            formula_content_digest=formula_digest,
            profile_artifact_id=profile_artifact_id,
            profile_revision=profile_revision,
            profile_facts=canonical_facts,
            profile_content_digest=profile_digest,
            graph_version=graph_version,
            rule_bundle_version=rule_bundle_version,
            rule_bundle_digest=rule_bundle_digest,
            synthetic_dataset_name=synthetic_dataset_name,
            synthetic_dataset_version=synthetic_dataset_version,
            synthetic_dataset_digest=dataset_digest,
            dataset_provenance=dataset_provenance,
            dataset_usage_scope=dataset_usage_scope,
            dataset_label_status=dataset_label_status,
        )


class SandboxSafetyIssueV1(_StrictFrozenModel):
    issue_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    severity: SandboxSafetySeverity
    execution_order: int = Field(ge=0)


class SandboxSafetyEvaluationV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(
        default=SANDBOX_EVALUATION_SCHEMA_VERSION,
        min_length=1,
        max_length=64,
    )
    decision: SandboxSafetyDecision
    issues: tuple[SandboxSafetyIssueV1, ...]


class SandboxSafetyResultV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(
        default=SANDBOX_RESULT_SCHEMA_VERSION,
        min_length=1,
        max_length=64,
    )
    adapter_version: str = Field(default=SANDBOX_ADAPTER_VERSION, min_length=1, max_length=64)
    decision_subject_digest: str = Field(pattern=_DIGEST_PATTERN)
    run_envelope_digest: str = Field(pattern=_DIGEST_PATTERN)
    decision: SandboxSafetyDecision
    issues: tuple[SandboxSafetyIssueV1, ...]
    result_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def result_is_canonical(self) -> SandboxSafetyResultV1:
        if self.issues != _canonical_issues(self.issues):
            raise ValueError("issues must be unique and sorted")
        if self.result_digest != _result_digest(
            sandbox_schema_version=self.sandbox_schema_version,
            adapter_version=self.adapter_version,
            decision_subject_digest=self.decision_subject_digest,
            decision=self.decision,
            issues=self.issues,
        ):
            raise ValueError("result digest mismatch")
        return self


class SandboxSafetyEvaluator(Protocol):
    def evaluate(
        self,
        subject: SandboxSafetySubjectV1,
        bundle: SandboxRuleBundleV1,
    ) -> SandboxSafetyEvaluationV1: ...


class _RunEnvelopeV1(_StrictFrozenModel):
    decision_subject_digest: str = Field(pattern=_DIGEST_PATTERN)
    command_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    run_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    trace_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize validated sandbox values with one stable JSON representation."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_result_bytes(result: SandboxSafetyResultV1) -> bytes:
    """Return the complete immutable result, including its run envelope digest."""

    return canonical_json_bytes(result)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _rule_bundle_body(
    *,
    sandbox_schema_version: str,
    adapter_version: str,
    rule_bundle_version: str,
    rules: tuple[SandboxRuleV1, ...],
) -> dict[str, object]:
    return {
        "adapter_version": adapter_version,
        "rule_bundle_version": rule_bundle_version,
        "rules": rules,
        "sandbox_schema_version": sandbox_schema_version,
    }


def _expected_bundle_digest(bundle: SandboxRuleBundleV1) -> str:
    return _canonical_sha256(
        _rule_bundle_body(
            sandbox_schema_version=bundle.sandbox_schema_version,
            adapter_version=bundle.adapter_version,
            rule_bundle_version=bundle.rule_bundle_version,
            rules=bundle.rules,
        )
    )


def _result_digest(
    *,
    sandbox_schema_version: str,
    adapter_version: str,
    decision_subject_digest: str,
    decision: SandboxSafetyDecision,
    issues: tuple[SandboxSafetyIssueV1, ...],
) -> str:
    return _canonical_sha256(
        {
            "adapter_version": adapter_version,
            "decision": decision,
            "decision_subject_digest": decision_subject_digest,
            "issues": issues,
            "sandbox_schema_version": sandbox_schema_version,
        }
    )


def _parse_model[ModelT: BaseModel](model_type: type[ModelT], value: object) -> ModelT | None:
    parsed: ModelT | None = None
    failed = False
    try:
        if isinstance(value, (str, bytes, bytearray)):
            parsed = model_type.model_validate_json(value, strict=True)
        else:
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="python")
            parsed = model_type.model_validate(value, strict=True)
    except Exception:
        failed = True
    if failed:
        return None
    return parsed


def _canonical_issues(
    issues: Sequence[SandboxSafetyIssueV1],
) -> tuple[SandboxSafetyIssueV1, ...]:
    canonical = tuple(sorted(tuple(issues), key=lambda issue: (issue.execution_order, issue.issue_id)))
    issue_ids = tuple(issue.issue_id for issue in canonical)
    execution_orders = tuple(issue.execution_order for issue in canonical)
    if len(issue_ids) != len(set(issue_ids)) or len(execution_orders) != len(set(execution_orders)):
        return ()
    return canonical


def _normalize_evaluation(
    evaluation: SandboxSafetyEvaluationV1,
    bundle: SandboxRuleBundleV1,
) -> SandboxSafetyEvaluationV1 | None:
    if evaluation.sandbox_schema_version != SANDBOX_EVALUATION_SCHEMA_VERSION:
        return None
    canonical_issues = _canonical_issues(evaluation.issues)
    if evaluation.issues and not canonical_issues:
        return None
    allowed_rule_ids = frozenset(rule.rule_id for rule in bundle.rules)
    if any(issue.rule_id not in allowed_rule_ids for issue in canonical_issues):
        return None
    parsed = _parse_model(
        SandboxSafetyEvaluationV1,
        SandboxSafetyEvaluationV1(
            decision=evaluation.decision,
            issues=canonical_issues,
        ),
    )
    return parsed


def _raise_error(code: SandboxSafetyFailureCode) -> NoReturn:
    raise SandboxSafetyAdapterError(code) from None


class SandboxSafetyRuleAdapter:
    """Validate exact immutable inputs and run one injected deterministic evaluator."""

    __slots__ = ("_evaluator",)

    def __init__(self, evaluator: SandboxSafetyEvaluator) -> None:
        self._evaluator = evaluator

    def evaluate(
        self,
        subject: object,
        bundle: object,
        *,
        command_id: str,
        run_id: str,
        trace_id: str,
    ) -> SandboxSafetyResultV1:
        unexpected_failure = False
        try:
            return self._evaluate(
                subject,
                bundle,
                command_id=command_id,
                run_id=run_id,
                trace_id=trace_id,
            )
        except SandboxSafetyAdapterError:
            raise
        except Exception:
            unexpected_failure = True
        if unexpected_failure:
            _raise_error(SandboxSafetyFailureCode.INTERNAL_FAILURE)
        raise AssertionError("unreachable")

    def _evaluate(
        self,
        subject_input: object,
        bundle_input: object,
        *,
        command_id: str,
        run_id: str,
        trace_id: str,
    ) -> SandboxSafetyResultV1:
        subject = _parse_model(SandboxSafetySubjectV1, subject_input)
        bundle = _parse_model(SandboxRuleBundleV1, bundle_input)
        if subject is None or bundle is None:
            _raise_error(SandboxSafetyFailureCode.SCHEMA_INVALID)

        if (
            subject.sandbox_schema_version != SANDBOX_SUBJECT_SCHEMA_VERSION
            or subject.adapter_version != SANDBOX_ADAPTER_VERSION
            or bundle.sandbox_schema_version != SANDBOX_RULE_BUNDLE_SCHEMA_VERSION
            or bundle.adapter_version != SANDBOX_ADAPTER_VERSION
        ):
            _raise_error(SandboxSafetyFailureCode.VERSION_MISMATCH)

        if (
            len(subject.formula_items) > MAX_FORMULA_ITEMS
            or len(bundle.rules) > MAX_RULES
            or len(canonical_json_bytes(subject)) > MAX_CANONICAL_BYTES
            or len(canonical_json_bytes(bundle)) > MAX_CANONICAL_BYTES
        ):
            _raise_error(SandboxSafetyFailureCode.LIMIT_EXCEEDED)

        expected_formula_digest = _canonical_sha256(subject.formula_items)
        expected_profile_digest = _canonical_sha256(subject.profile_facts)
        expected_dataset_digest = _canonical_sha256(
            {"formula_items": subject.formula_items, "profile_facts": subject.profile_facts}
        )
        if (
            subject.formula_content_digest != expected_formula_digest
            or subject.profile_content_digest != expected_profile_digest
            or subject.synthetic_dataset_digest != expected_dataset_digest
            or bundle.rule_bundle_digest != _expected_bundle_digest(bundle)
            or subject.rule_bundle_version != bundle.rule_bundle_version
            or subject.rule_bundle_digest != bundle.rule_bundle_digest
        ):
            _raise_error(SandboxSafetyFailureCode.DIGEST_MISMATCH)

        decision_subject_digest = _canonical_sha256(subject)
        run_envelope = _parse_model(
            _RunEnvelopeV1,
            {
                "decision_subject_digest": decision_subject_digest,
                "command_id": command_id,
                "run_id": run_id,
                "trace_id": trace_id,
            },
        )
        if run_envelope is None:
            _raise_error(SandboxSafetyFailureCode.SCHEMA_INVALID)
        run_envelope_digest = _canonical_sha256(run_envelope)

        first_raw, first_failed = self._call_evaluator(subject, bundle)
        if first_failed:
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_FAILED)
        first = _parse_model(SandboxSafetyEvaluationV1, first_raw)
        if first is None:
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_RESULT_INVALID)
        if len(first.issues) > MAX_ISSUES:
            _raise_error(SandboxSafetyFailureCode.LIMIT_EXCEEDED)
        first = _normalize_evaluation(first, bundle)
        if first is None:
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_RESULT_INVALID)

        second_raw, second_failed = self._call_evaluator(subject, bundle)
        if second_failed:
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_FAILED)
        second = _parse_model(SandboxSafetyEvaluationV1, second_raw)
        if second is None:
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_RESULT_INVALID)
        if len(second.issues) > MAX_ISSUES:
            _raise_error(SandboxSafetyFailureCode.LIMIT_EXCEEDED)
        second = _normalize_evaluation(second, bundle)
        if second is None:
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_RESULT_INVALID)
        if canonical_json_bytes(first) != canonical_json_bytes(second):
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_NONDETERMINISTIC)

        result = SandboxSafetyResultV1(
            decision_subject_digest=decision_subject_digest,
            run_envelope_digest=run_envelope_digest,
            decision=first.decision,
            issues=first.issues,
            result_digest=_result_digest(
                sandbox_schema_version=SANDBOX_RESULT_SCHEMA_VERSION,
                adapter_version=SANDBOX_ADAPTER_VERSION,
                decision_subject_digest=decision_subject_digest,
                decision=first.decision,
                issues=first.issues,
            ),
        )
        if len(canonical_result_bytes(result)) > MAX_CANONICAL_BYTES:
            _raise_error(SandboxSafetyFailureCode.LIMIT_EXCEEDED)
        return result

    def _call_evaluator(
        self,
        subject: SandboxSafetySubjectV1,
        bundle: SandboxRuleBundleV1,
    ) -> tuple[object | None, bool]:
        raw: object | None = None
        failed = False
        try:
            raw = self._evaluator.evaluate(subject, bundle)
        except Exception:
            failed = True
        return raw, failed
