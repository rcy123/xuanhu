"""Offline deterministic safety adapter for the L5 personal-learning sandbox."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, model_validator

SANDBOX_SUBJECT_SCHEMA_VERSION = "sandbox-safety-subject.v1"
SANDBOX_RULE_BUNDLE_SCHEMA_VERSION = "sandbox-rule-bundle.v1"
SANDBOX_EVALUATION_SCHEMA_VERSION = "sandbox-safety-evaluation.v1"
SANDBOX_EVALUATION_CASE_SCHEMA_VERSION = "sandbox-evaluation-case.v1"
SANDBOX_AUTHORITY_SCHEMA_VERSION = "sandbox-evaluator-authority.v1"
SANDBOX_MANIFEST_SCHEMA_VERSION = "sandbox-synthetic-manifest.v2"
SANDBOX_IDENTIFIER_SCAN_SCHEMA_VERSION = "sandbox-identifier-scan.v2"
SANDBOX_RESULT_SCHEMA_VERSION = "sandbox-safety-result.v1"
SANDBOX_ADAPTER_VERSION = "sandbox-safety-adapter.v2"

MAX_FORMULA_ITEMS = 64
MAX_PROFILE_FACTS = 64
MAX_ISSUES = 256
MAX_RULES = 256
MAX_RULE_PARAMETERS = 256
MAX_TOTAL_RULE_PARAMETERS = 1024
MAX_EVALUATION_CASES = 64
MAX_TOTAL_ISSUES = 1024
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
    PROHIBITED_IDENTIFIER = "SANDBOX_SAFETY_PROHIBITED_IDENTIFIER"
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
    parameters: tuple[SandboxRuleParameterV1, ...] = Field(
        max_length=MAX_RULE_PARAMETERS
    )

    @model_validator(mode="after")
    def parameters_are_canonical(self) -> SandboxRuleV1:
        names = tuple(parameter.name for parameter in self.parameters)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("rule parameters must be unique and sorted")
        return self


class SandboxIdentifierScanV1(_StrictFrozenModel):
    schema_version: Literal["sandbox-identifier-scan.v2"]
    tool: Literal["sandbox_static_identifier_scan"]
    tool_version: Literal["2.0.0"]
    ruleset_version: Literal["sandbox-prohibited-identifiers.v2"]
    scanned_at: Literal["2000-01-01T00:00:00Z"]
    result: Literal["passed_no_configured_identifier_pattern_matches"]

    @classmethod
    def passed(cls) -> SandboxIdentifierScanV1:
        return cls(
            schema_version="sandbox-identifier-scan.v2",
            tool="sandbox_static_identifier_scan",
            tool_version="2.0.0",
            ruleset_version="sandbox-prohibited-identifiers.v2",
            scanned_at="2000-01-01T00:00:00Z",
            result="passed_no_configured_identifier_pattern_matches",
        )


class SandboxSyntheticManifestV1(_StrictFrozenModel):
    schema_version: Literal["sandbox-synthetic-manifest.v2"]
    dataset_name: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    dataset_version: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    admission_scope: Literal["personal_learning_synthetic_only"]
    provenance_type: Literal["constructed_fixture"]
    fixture_provenance: Literal["fixed_fictitious_manual"]
    usage_scope: Literal["sandbox_only"]
    source_statement: Literal[
        "not_from_real_medical_records_personal_records_production_logs_chat_records_or_external_datasets"
    ]
    generator_path: Literal["not_applicable"]
    generator_version: Literal["not_applicable"]
    generator_digest: Literal["not_applicable"]
    seed: Literal["not_applicable"]
    construction_evidence: Literal["manually_constructed_fixed_fictitious_technical_fixture"]
    case_count: Literal[1]
    content_sha256: str = Field(pattern=_DIGEST_PATTERN)
    created_at: Literal["2000-01-01T00:00:00Z"]
    created_by_test_role: Literal["sandbox_fixture_author"]
    prohibited_identifier_scan: SandboxIdentifierScanV1
    label_status: Literal["not_clinically_adjudicated"]

    @classmethod
    def build(
        cls,
        *,
        dataset_name: str,
        dataset_version: str,
        formula_items: Sequence[SandboxFormulaItemV1],
        profile_facts: Sequence[SandboxProfileFactV1],
    ) -> SandboxSyntheticManifestV1:
        canonical_items = tuple(
            sorted(
                _bounded_tuple(formula_items, MAX_FORMULA_ITEMS),
                key=lambda item: item.item_id,
            )
        )
        canonical_facts = tuple(
            sorted(
                _bounded_tuple(profile_facts, MAX_PROFILE_FACTS),
                key=lambda fact: fact.fact_id,
            )
        )
        if _fixture_has_configured_identifier_pattern(
            canonical_items,
            canonical_facts,
            metadata_values=(dataset_name, dataset_version),
        ):
            raise ValueError("sandbox fixture contains a prohibited identifier") from None
        return cls(
            schema_version="sandbox-synthetic-manifest.v2",
            dataset_name=dataset_name,
            dataset_version=dataset_version,
            admission_scope="personal_learning_synthetic_only",
            provenance_type="constructed_fixture",
            fixture_provenance="fixed_fictitious_manual",
            usage_scope="sandbox_only",
            source_statement=(
                "not_from_real_medical_records_personal_records_production_logs_chat_records_or_external_datasets"
            ),
            generator_path="not_applicable",
            generator_version="not_applicable",
            generator_digest="not_applicable",
            seed="not_applicable",
            construction_evidence="manually_constructed_fixed_fictitious_technical_fixture",
            case_count=1,
            content_sha256=_fixture_content_digest(canonical_items, canonical_facts),
            created_at="2000-01-01T00:00:00Z",
            created_by_test_role="sandbox_fixture_author",
            prohibited_identifier_scan=SandboxIdentifierScanV1.passed(),
            label_status="not_clinically_adjudicated",
        )


class SandboxSafetyIssueV1(_StrictFrozenModel):
    issue_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    rule_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    severity: SandboxSafetySeverity
    execution_order: int = Field(ge=0)


def _canonical_issues(
    issues: Sequence[SandboxSafetyIssueV1],
) -> tuple[SandboxSafetyIssueV1, ...]:
    canonical = tuple(
        sorted(tuple(issues), key=lambda issue: (issue.execution_order, issue.issue_id))
    )
    issue_ids = tuple(issue.issue_id for issue in canonical)
    execution_orders = tuple(issue.execution_order for issue in canonical)
    if len(issue_ids) != len(set(issue_ids)) or len(execution_orders) != len(
        set(execution_orders)
    ):
        return ()
    return canonical


class SandboxSafetyEvaluationV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(
        default=SANDBOX_EVALUATION_SCHEMA_VERSION,
        min_length=1,
        max_length=64,
    )
    decision: SandboxSafetyDecision
    issues: tuple[SandboxSafetyIssueV1, ...]

    @model_validator(mode="after")
    def issues_are_canonical(self) -> SandboxSafetyEvaluationV1:
        if self.issues != _canonical_issues(self.issues):
            raise ValueError("issues must be unique and sorted")
        return self


class SandboxEvaluationCaseV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(
        default=SANDBOX_EVALUATION_CASE_SCHEMA_VERSION,
        min_length=1,
        max_length=64,
    )
    case_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    formula_content_digest: str = Field(pattern=_DIGEST_PATTERN)
    profile_content_digest: str = Field(pattern=_DIGEST_PATTERN)
    synthetic_dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    evaluation: SandboxSafetyEvaluationV1

    @classmethod
    def build(
        cls,
        *,
        case_id: str,
        formula_items: Sequence[SandboxFormulaItemV1],
        profile_facts: Sequence[SandboxProfileFactV1],
        manifest: SandboxSyntheticManifestV1,
        evaluation: SandboxSafetyEvaluationV1,
    ) -> SandboxEvaluationCaseV1:
        canonical_items = tuple(
            sorted(
                _bounded_tuple(formula_items, MAX_FORMULA_ITEMS),
                key=lambda item: item.item_id,
            )
        )
        canonical_facts = tuple(
            sorted(
                _bounded_tuple(profile_facts, MAX_PROFILE_FACTS),
                key=lambda fact: fact.fact_id,
            )
        )
        return cls(
            case_id=case_id,
            formula_content_digest=_canonical_sha256(canonical_items),
            profile_content_digest=_canonical_sha256(canonical_facts),
            synthetic_dataset_digest=_synthetic_dataset_digest(
                canonical_items,
                canonical_facts,
                manifest,
            ),
            evaluation=evaluation,
        )


class SandboxEvaluatorAuthorityV1(_StrictFrozenModel):
    sandbox_schema_version: str = Field(
        default=SANDBOX_AUTHORITY_SCHEMA_VERSION,
        min_length=1,
        max_length=64,
    )
    cases: tuple[SandboxEvaluationCaseV1, ...]
    authority_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def cases_are_canonical(self) -> SandboxEvaluatorAuthorityV1:
        case_ids = tuple(case.case_id for case in self.cases)
        bindings = tuple(
            (
                case.formula_content_digest,
                case.profile_content_digest,
                case.synthetic_dataset_digest,
            )
            for case in self.cases
        )
        if case_ids != tuple(sorted(case_ids)) or len(case_ids) != len(set(case_ids)):
            raise ValueError("authority cases must be unique and sorted")
        if len(bindings) != len(set(bindings)):
            raise ValueError("authority subject bindings must be unique")
        if (
            sum(len(case.evaluation.issues) for case in self.cases)
            > MAX_TOTAL_ISSUES
        ):
            raise ValueError("authority contains too many total issues")
        return self

    @classmethod
    def build(
        cls,
        *,
        cases: Sequence[SandboxEvaluationCaseV1],
    ) -> SandboxEvaluatorAuthorityV1:
        canonical_cases = tuple(
            sorted(
                _bounded_tuple(cases, MAX_EVALUATION_CASES),
                key=lambda case: case.case_id,
            )
        )
        if (
            sum(len(case.evaluation.issues) for case in canonical_cases)
            > MAX_TOTAL_ISSUES
        ):
            raise ValueError("authority contains too many total issues")
        return cls(
            cases=canonical_cases,
            authority_digest=_canonical_sha256(
                _authority_body(SANDBOX_AUTHORITY_SCHEMA_VERSION, canonical_cases)
            ),
        )


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
    evaluator_authority: SandboxEvaluatorAuthorityV1
    rule_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def contents_are_canonical(self) -> SandboxRuleBundleV1:
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if rule_ids != tuple(sorted(rule_ids)) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rules must be unique and sorted")
        allowed_rule_ids = frozenset(rule_ids)
        if any(
            issue.rule_id not in allowed_rule_ids
            for case in self.evaluator_authority.cases
            for issue in case.evaluation.issues
        ):
            raise ValueError("authority issue references unknown rule")
        if (
            sum(len(rule.parameters) for rule in self.rules)
            > MAX_TOTAL_RULE_PARAMETERS
        ):
            raise ValueError("rule bundle contains too many total parameters")
        return self

    @classmethod
    def build(
        cls,
        *,
        rule_bundle_version: str,
        rules: Sequence[SandboxRuleV1],
        evaluator_authority: SandboxEvaluatorAuthorityV1,
    ) -> SandboxRuleBundleV1:
        canonical_rules = tuple(
            sorted(
                _bounded_tuple(rules, MAX_RULES),
                key=lambda rule: rule.rule_id,
            )
        )
        if (
            sum(len(rule.parameters) for rule in canonical_rules)
            > MAX_TOTAL_RULE_PARAMETERS
        ):
            raise ValueError("rule bundle contains too many total parameters")
        body = _rule_bundle_body(
            sandbox_schema_version=SANDBOX_RULE_BUNDLE_SCHEMA_VERSION,
            adapter_version=SANDBOX_ADAPTER_VERSION,
            rule_bundle_version=rule_bundle_version,
            rules=canonical_rules,
            evaluator_authority=evaluator_authority,
        )
        return cls(
            rule_bundle_version=rule_bundle_version,
            rules=canonical_rules,
            evaluator_authority=evaluator_authority,
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
    evaluator_authority_digest: str = Field(pattern=_DIGEST_PATTERN)
    synthetic_dataset_name: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    synthetic_dataset_version: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    synthetic_dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    synthetic_manifest: SandboxSyntheticManifestV1
    synthetic_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)

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
        evaluator_authority_digest: str,
        synthetic_manifest: SandboxSyntheticManifestV1,
    ) -> SandboxSafetySubjectV1:
        canonical_items = tuple(
            sorted(
                _bounded_tuple(formula_items, MAX_FORMULA_ITEMS),
                key=lambda item: item.item_id,
            )
        )
        canonical_facts = tuple(
            sorted(
                _bounded_tuple(profile_facts, MAX_PROFILE_FACTS),
                key=lambda fact: fact.fact_id,
            )
        )
        return cls(
            test_session_id=test_session_id,
            domain_state_version=domain_state_version,
            formula_artifact_id=formula_artifact_id,
            formula_revision=formula_revision,
            formula_items=canonical_items,
            formula_content_digest=_canonical_sha256(canonical_items),
            profile_artifact_id=profile_artifact_id,
            profile_revision=profile_revision,
            profile_facts=canonical_facts,
            profile_content_digest=_canonical_sha256(canonical_facts),
            graph_version=graph_version,
            rule_bundle_version=rule_bundle_version,
            rule_bundle_digest=rule_bundle_digest,
            evaluator_authority_digest=evaluator_authority_digest,
            synthetic_dataset_name=synthetic_manifest.dataset_name,
            synthetic_dataset_version=synthetic_manifest.dataset_version,
            synthetic_dataset_digest=_synthetic_dataset_digest(
                canonical_items,
                canonical_facts,
                synthetic_manifest,
            ),
            synthetic_manifest=synthetic_manifest,
            synthetic_manifest_digest=_canonical_sha256(synthetic_manifest),
        )


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
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    return value


def _fixture_content(
    formula_items: tuple[SandboxFormulaItemV1, ...],
    profile_facts: tuple[SandboxProfileFactV1, ...],
) -> dict[str, object]:
    return {
        "cases": (
            {
                "formula_items": formula_items,
                "profile_facts": profile_facts,
            },
        )
    }


def _fixture_content_digest(
    formula_items: tuple[SandboxFormulaItemV1, ...],
    profile_facts: tuple[SandboxProfileFactV1, ...],
) -> str:
    return _canonical_sha256(_fixture_content(formula_items, profile_facts))


_DIRECT_IDENTIFIER_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?86[ .-]?)?1[3-9]\d(?:[ .-]?\d){8}(?!\d)"),
    re.compile(r"(?<![\dXx])\d(?:[ .-]?\d){16}[ .-]?[\dXx](?![\dXx])"),
    re.compile(r"(?<!\d)0\d{2,3}[ .-]?\d{7,8}(?!\d)"),
)
_IDENTITY_KEY_FORMS = frozenset(
    {
        "address",
        "contactdetails",
        "contactnumber",
        "contactphone",
        "email",
        "encounterno",
        "encounternumber",
        "familyname",
        "firstname",
        "fullname",
        "givenname",
        "hospitalno",
        "hospitalnumber",
        "homeaddress",
        "idcard",
        "idnumber",
        "identitycard",
        "identitynumber",
        "inpatientno",
        "inpatientnumber",
        "lastname",
        "medicalrecordno",
        "medicalrecordnumber",
        "mobile",
        "mobileno",
        "mobilenumber",
        "mrn",
        "name",
        "nationalid",
        "outpatientno",
        "outpatientnumber",
        "patientname",
        "phone",
        "phoneno",
        "phonenumber",
        "surname",
        "telephone",
        "visitno",
        "visitnumber",
    }
)
_IDENTITY_KEY_FORMS_WITHOUT_GENERIC_NAME = _IDENTITY_KEY_FORMS - {"name"}
_IDENTITY_KEY_CJK_MARKERS = (
    "住址",
    "地址",
    "家庭住址",
    "姓名",
    "名字",
    "全名",
    "电话",
    "手机",
    "手机号",
    "手机号码",
    "身份证",
    "身份证号",
    "证件号",
    "病历号",
    "就诊号",
    "挂号号",
    "门诊号",
    "住院号",
    "联系电话",
    "联系方式",
    "邮箱",
)
_IDENTITY_KEY_ENGLISH_ANCHORS = (
    "address",
    "contact",
    "email",
    "encounter",
    "family",
    "first",
    "full",
    "given",
    "hospital",
    "id",
    "identity",
    "inpatient",
    "last",
    "medical",
    "mobile",
    "mrn",
    "name",
    "national",
    "outpatient",
    "patient",
    "phone",
    "record",
    "surname",
    "telephone",
    "visit",
)
_EMAIL_LOCAL_PUNCTUATION = frozenset(".!#$%&'*+/=?^_`{|}~-")


def _is_identifier_ignorable(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character) == "Cf"
        or codepoint == 0x034F
        or 0x115F <= codepoint <= 0x1160
        or 0x17B4 <= codepoint <= 0x17B5
        or 0x180B <= codepoint <= 0x180F
        or codepoint == 0x3164
        or 0xFE00 <= codepoint <= 0xFE0F
        or codepoint == 0xFFA0
        or 0xFFF0 <= codepoint <= 0xFFF8
        or 0xE0100 <= codepoint <= 0xE01EF
    )


def _normalized_identifier_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not _is_identifier_ignorable(character)
    )


def _normalized_contains_identity_key(normalized: str) -> bool:
    normalized = normalized.casefold()
    if normalized in _IDENTITY_KEY_CJK_MARKERS:
        return True
    if not any(anchor in normalized for anchor in _IDENTITY_KEY_ENGLISH_ANCHORS):
        return False
    tokens = tuple(
        token
        for token in re.split(
            r"[^a-z0-9]+",
            normalized,
        )
        if token
    )
    if tokens == ("name",):
        return True
    return "".join(tokens) in _IDENTITY_KEY_FORMS_WITHOUT_GENERIC_NAME


def _normalized_contains_direct_identifier(normalized: str) -> bool:
    return (
        any(
            pattern.search(normalized) is not None
            for pattern in _DIRECT_IDENTIFIER_PATTERNS
        )
        or _contains_email_identifier(normalized)
    )


def _contains_identity_key(value: str) -> bool:
    return _normalized_contains_identity_key(_normalized_identifier_text(value))


def _contains_direct_identifier(value: str) -> bool:
    return _normalized_contains_direct_identifier(_normalized_identifier_text(value))


def _contains_email_identifier(value: str) -> bool:
    if "@" not in value:
        return False
    segments = value.split("@")
    for local_segment, domain_segment in zip(segments, segments[1:], strict=False):
        local_start = len(local_segment)
        while local_start > 0:
            character = local_segment[local_start - 1]
            if not (
                character.isalnum()
                or character in _EMAIL_LOCAL_PUNCTUATION
            ):
                break
            local_start -= 1
        domain_end = 0
        while domain_end < len(domain_segment):
            character = domain_segment[domain_end]
            if not (character.isalnum() or character in ".-"):
                break
            domain_end += 1
        local = local_segment[local_start:].strip(".")
        domain = domain_segment[:domain_end].strip(".")
        labels = domain.split(".")
        if (
            local
            and len(labels) >= 2
            and all(
                label
                and label[0].isalnum()
                and label[-1].isalnum()
                and all(
                    character.isalnum() or character == "-"
                    for character in label
                )
                for label in labels
            )
        ):
            return True
    return False


def _bounded_tuple[T](values: Sequence[T], limit: int) -> tuple[T, ...]:
    bounded: list[T] = []
    for value in values:
        if len(bounded) >= limit:
            raise ValueError("sandbox fixture exceeds configured item limit")
        bounded.append(value)
    return tuple(bounded)


def _fixture_has_configured_identifier_pattern(
    formula_items: tuple[SandboxFormulaItemV1, ...],
    profile_facts: tuple[SandboxProfileFactV1, ...],
    *,
    metadata_values: tuple[str, ...] = (),
    identity_key_values: tuple[str, ...] = (),
) -> bool:
    formula_values = tuple(
        value
        for item in formula_items
        for value in (item.item_id, item.component, item.unit)
    )
    profile_values = tuple(
        value
        for fact in profile_facts
        for value in (fact.fact_id, fact.name, fact.value)
    )
    structured_identity_keys = (
        *formula_values,
        *(
            value
            for fact in profile_facts
            for value in (fact.fact_id, fact.name)
        ),
        *identity_key_values,
    )
    return (
        any(
            _contains_direct_identifier(value)
            for value in (*formula_values, *profile_values, *metadata_values)
        )
        or any(
            _contains_identity_key(value)
            for value in structured_identity_keys
        )
    )


def _package_has_configured_identifier_pattern(
    subject: SandboxSafetySubjectV1,
    bundle: SandboxRuleBundleV1,
) -> bool:
    metadata_values = (
        subject.test_session_id,
        subject.formula_artifact_id,
        subject.profile_artifact_id,
        subject.graph_version,
        subject.rule_bundle_version,
        subject.synthetic_dataset_name,
        subject.synthetic_dataset_version,
        bundle.rule_bundle_version,
        *(
            value
            for rule in bundle.rules
            for value in (
                rule.rule_id,
                *(
                    text
                    for parameter in rule.parameters
                    for text in (parameter.name, parameter.value)
                ),
            )
        ),
        *(
            value
            for case in bundle.evaluator_authority.cases
            for value in (
                case.case_id,
                *(
                    text
                    for issue in case.evaluation.issues
                    for text in (issue.issue_id, issue.rule_id)
                ),
            )
        ),
    )
    identity_key_values = tuple(
        parameter.name
        for rule in bundle.rules
        for parameter in rule.parameters
    )
    return _fixture_has_configured_identifier_pattern(
        subject.formula_items,
        subject.profile_facts,
        metadata_values=metadata_values,
        identity_key_values=identity_key_values,
    )


def _synthetic_dataset_digest(
    formula_items: tuple[SandboxFormulaItemV1, ...],
    profile_facts: tuple[SandboxProfileFactV1, ...],
    manifest: SandboxSyntheticManifestV1,
) -> str:
    return _canonical_sha256(
        {
            "fixture_content": _fixture_content(formula_items, profile_facts),
            "manifest": manifest,
        }
    )


def _authority_body(
    sandbox_schema_version: str,
    cases: tuple[SandboxEvaluationCaseV1, ...],
) -> dict[str, object]:
    return {
        "cases": cases,
        "sandbox_schema_version": sandbox_schema_version,
    }


def _expected_authority_digest(authority: SandboxEvaluatorAuthorityV1) -> str:
    return _canonical_sha256(_authority_body(authority.sandbox_schema_version, authority.cases))


def _rule_bundle_body(
    *,
    sandbox_schema_version: str,
    adapter_version: str,
    rule_bundle_version: str,
    rules: tuple[SandboxRuleV1, ...],
    evaluator_authority: SandboxEvaluatorAuthorityV1,
) -> dict[str, object]:
    return {
        "adapter_version": adapter_version,
        "evaluator_authority": evaluator_authority,
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
            evaluator_authority=bundle.evaluator_authority,
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
        if isinstance(value, str | bytes | bytearray):
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


def _input_exceeds_resource_budget(value: object) -> bool:
    """Bound untrusted wire/model graphs before Pydantic parsing or dumping."""

    budget = MAX_CANONICAL_BYTES
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, BaseModel):
            fields = type(current).model_fields
            stack.extend(getattr(current, field_name) for field_name in fields)
            budget -= 64 + 8 * len(fields)
        elif isinstance(current, dict):
            budget -= 16 * len(current)
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list | tuple):
            budget -= 8 * len(current)
            stack.extend(current)
        elif isinstance(current, str):
            if len(current) > budget:
                return True
            budget -= len(current.encode("utf-8")) + 2
        elif isinstance(current, bytes | bytearray):
            budget -= len(current)
        else:
            budget -= 32
        if budget < 0:
            return True
    return False


def _raise_error(code: SandboxSafetyFailureCode) -> NoReturn:
    raise SandboxSafetyAdapterError(code) from None


class SandboxSafetyRuleAdapter:
    """Interpret one immutable, digest-bound declarative evaluator authority."""

    __slots__ = ()

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
        if _input_exceeds_resource_budget(
            subject_input
        ) or _input_exceeds_resource_budget(bundle_input):
            _raise_error(SandboxSafetyFailureCode.LIMIT_EXCEEDED)
        subject = _parse_model(SandboxSafetySubjectV1, subject_input)
        bundle = _parse_model(SandboxRuleBundleV1, bundle_input)
        if subject is None or bundle is None:
            _raise_error(SandboxSafetyFailureCode.SCHEMA_INVALID)

        manifest = subject.synthetic_manifest
        scan = manifest.prohibited_identifier_scan
        authority = bundle.evaluator_authority
        if (
            subject.sandbox_schema_version != SANDBOX_SUBJECT_SCHEMA_VERSION
            or subject.adapter_version != SANDBOX_ADAPTER_VERSION
            or bundle.sandbox_schema_version != SANDBOX_RULE_BUNDLE_SCHEMA_VERSION
            or bundle.adapter_version != SANDBOX_ADAPTER_VERSION
            or manifest.schema_version != SANDBOX_MANIFEST_SCHEMA_VERSION
            or scan.schema_version != SANDBOX_IDENTIFIER_SCAN_SCHEMA_VERSION
            or authority.sandbox_schema_version != SANDBOX_AUTHORITY_SCHEMA_VERSION
            or any(
                case.sandbox_schema_version != SANDBOX_EVALUATION_CASE_SCHEMA_VERSION
                or case.evaluation.sandbox_schema_version != SANDBOX_EVALUATION_SCHEMA_VERSION
                for case in authority.cases
            )
        ):
            _raise_error(SandboxSafetyFailureCode.VERSION_MISMATCH)

        if (
            len(subject.formula_items) > MAX_FORMULA_ITEMS
            or len(subject.profile_facts) > MAX_PROFILE_FACTS
            or len(bundle.rules) > MAX_RULES
            or len(authority.cases) > MAX_EVALUATION_CASES
            or any(
                len(rule.parameters) > MAX_RULE_PARAMETERS
                for rule in bundle.rules
            )
            or sum(len(rule.parameters) for rule in bundle.rules)
            > MAX_TOTAL_RULE_PARAMETERS
            or any(len(case.evaluation.issues) > MAX_ISSUES for case in authority.cases)
            or sum(len(case.evaluation.issues) for case in authority.cases)
            > MAX_TOTAL_ISSUES
            or len(canonical_json_bytes(subject)) > MAX_CANONICAL_BYTES
            or len(canonical_json_bytes(bundle)) > MAX_CANONICAL_BYTES
        ):
            _raise_error(SandboxSafetyFailureCode.LIMIT_EXCEEDED)
        if _package_has_configured_identifier_pattern(subject, bundle):
            _raise_error(SandboxSafetyFailureCode.PROHIBITED_IDENTIFIER)

        expected_formula_digest = _canonical_sha256(subject.formula_items)
        expected_profile_digest = _canonical_sha256(subject.profile_facts)
        expected_content_digest = _fixture_content_digest(
            subject.formula_items,
            subject.profile_facts,
        )
        expected_manifest_digest = _canonical_sha256(manifest)
        expected_dataset_digest = _synthetic_dataset_digest(
            subject.formula_items,
            subject.profile_facts,
            manifest,
        )
        if (
            subject.formula_content_digest != expected_formula_digest
            or subject.profile_content_digest != expected_profile_digest
            or manifest.dataset_name != subject.synthetic_dataset_name
            or manifest.dataset_version != subject.synthetic_dataset_version
            or manifest.content_sha256 != expected_content_digest
            or manifest.case_count != 1
            or subject.synthetic_manifest_digest != expected_manifest_digest
            or subject.synthetic_dataset_digest != expected_dataset_digest
            or authority.authority_digest != _expected_authority_digest(authority)
            or bundle.rule_bundle_digest != _expected_bundle_digest(bundle)
            or subject.rule_bundle_version != bundle.rule_bundle_version
            or subject.rule_bundle_digest != bundle.rule_bundle_digest
            or subject.evaluator_authority_digest != authority.authority_digest
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
        if any(
            _contains_direct_identifier(value)
            for value in (
                run_envelope.command_id,
                run_envelope.run_id,
                run_envelope.trace_id,
            )
        ):
            _raise_error(SandboxSafetyFailureCode.PROHIBITED_IDENTIFIER)
        run_envelope_digest = _canonical_sha256(run_envelope)

        matching_cases = tuple(
            case
            for case in authority.cases
            if case.formula_content_digest == subject.formula_content_digest
            and case.profile_content_digest == subject.profile_content_digest
            and case.synthetic_dataset_digest == subject.synthetic_dataset_digest
        )
        if len(matching_cases) != 1:
            _raise_error(SandboxSafetyFailureCode.EVALUATOR_RESULT_INVALID)
        evaluation = matching_cases[0].evaluation

        result = SandboxSafetyResultV1(
            decision_subject_digest=decision_subject_digest,
            run_envelope_digest=run_envelope_digest,
            decision=evaluation.decision,
            issues=evaluation.issues,
            result_digest=_result_digest(
                sandbox_schema_version=SANDBOX_RESULT_SCHEMA_VERSION,
                adapter_version=SANDBOX_ADAPTER_VERSION,
                decision_subject_digest=decision_subject_digest,
                decision=evaluation.decision,
                issues=evaluation.issues,
            ),
        )
        if len(canonical_result_bytes(result)) > MAX_CANONICAL_BYTES:
            _raise_error(SandboxSafetyFailureCode.LIMIT_EXCEEDED)
        return result


def reproduce_sandbox_safety_result(
    subject: object,
    bundle: object,
    *,
    command_id: str,
    run_id: str,
    trace_id: str,
) -> SandboxSafetyResultV1:
    """Replay the pure evaluator for downstream authority validation."""

    unexpected_failure = False
    try:
        return SandboxSafetyRuleAdapter()._evaluate(
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
