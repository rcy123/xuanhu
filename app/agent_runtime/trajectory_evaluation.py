"""R3 trajectory-evaluation contract and pure deterministic evaluator engine.

R3-A1 pins the **contract**: compact strict frozen Pydantic v2 models plus the
canonical helpers that make a trajectory, a suite manifest, and their reports
reproducible byte-for-byte.

R3-A2 adds the **engine**: a pure, deterministic evaluator that runs a suite
manifest through a pluggable callable executor, verifies every digest before
execution, decides the six behavior invariants from observed typed steps, binds
the actual normalized steps into each report via a stable digest, and fails
closed on tampered input, non-safe expectations, executor exceptions, and
invalid executor output — all with no IO and no leaked failure detail.

The contract is deliberately symbolic and volatile-free:

- every identifier is a bounded symbolic string (never a timestamp, UUID,
  trace/session/run id, or raw text);
- every payload is a closed set of typed fields (``extra="forbid"``) with no
  coercion (``strict=True``) and no unbounded strings;
- digests are canonical SHA-256 over sorted-key JSON with ``allow_nan=False``,
  so identical behavior always yields an identical digest.

No IO is performed here: no model/network/DB/Redis/time/random/uuid/subprocess,
no prompt text, and no model output.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SchemaVersion = Literal["agent-trajectory-eval.v1"]

#: The single supported schema literal of this contract.
SCHEMA_VERSION: SchemaVersion = "agent-trajectory-eval.v1"

#: Hard ceiling on the cumulative question count a single step may record.
MAX_QUESTION_COUNT = 100
#: Hard ceiling on the number of ordered steps in one trajectory.
MAX_TRAJECTORY_STEPS = 64
#: The scenario enum is closed at exactly these six members.
REQUIRED_SCENARIO_COUNT = 6
#: A report carries at most one failure code per invariant, plus one adapter-failure code.
MAX_REPORT_FAILURE_CODES = REQUIRED_SCENARIO_COUNT + 1
#: Symbolic identifiers only: no whitespace, free text, or volatile separators.
SYMBOLIC_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

_HEX_DIGITS = frozenset("0123456789abcdef")
_PLACEHOLDER_DIGEST = "0" * 64


class Scenario(StrEnum):
    """The closed set of agent-trajectory evaluation scenarios."""

    MULTI_TURN_INTAKE = "multi_turn_intake"
    CORRECTION_RETRACTION = "correction_retraction"
    AMBIGUOUS_SAFETY_NEGATION = "ambiguous_safety_negation"
    RED_FLAG_ESCALATION = "red_flag_escalation"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    CHECKPOINT_RESUME = "checkpoint_resume"


class StepAction(StrEnum):
    """The closed set of symbolic step actions."""

    OBSERVE = "observe"
    CLARIFY = "clarify"
    EXTRACT = "extract"
    REASON = "reason"
    ESCALATE = "escalate"
    RESPOND = "respond"
    REPLAY = "replay"
    RESUME = "resume"
    PROJECT = "project"


class Route(StrEnum):
    """The closed set of routing targets relevant to the six invariants."""

    INTAKE = "intake"
    CLARIFICATION = "clarification"
    REASONING = "reasoning"
    SAFETY = "safety"
    ESCALATION = "escalation"
    RED_FLAG = "red_flag"
    REPLAY = "replay"
    RESUME = "resume"
    PROJECTION = "projection"
    TERMINAL = "terminal"


class InvariantKind(StrEnum):
    """The six behavior invariants the offline evaluation must decide."""

    MISSED_SAFETY_ESCALATION = "missed_safety_escalation"
    PROTOCOL_VALID = "protocol_valid"
    ROUTE_STATE_CONSISTENCY = "route_state_consistency"
    REPLAY_EQUALITY = "replay_equality"
    BOUNDED_QUESTION_COUNT = "bounded_question_count"
    PHI_SAFE_PROJECTION = "phi_safe_projection"


class InvariantStatus(StrEnum):
    """Closed status of a single invariant outcome."""

    SATISFIED = "satisfied"
    VIOLATED = "violated"


class EvaluationFailureCode(StrEnum):
    """Fixed payload-free failure codes for the trajectory-eval contract."""

    NOT_JSON = "NOT_JSON"
    TRAJECTORY_DIGEST_MISMATCH = "TRAJECTORY_DIGEST_MISMATCH"
    MANIFEST_DIGEST_MISMATCH = "MANIFEST_DIGEST_MISMATCH"
    MISSED_SAFETY_ESCALATION = "MISSED_SAFETY_ESCALATION"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    ROUTE_STATE_INCONSISTENCY = "ROUTE_STATE_INCONSISTENCY"
    REPLAY_INEQUALITY = "REPLAY_INEQUALITY"
    QUESTION_COUNT_UNBOUNDED = "QUESTION_COUNT_UNBOUNDED"
    PHI_PROJECTION_UNSAFE = "PHI_PROJECTION_UNSAFE"
    ADAPTER_FAILURE = "ADAPTER_FAILURE"
    UNSAFE_EXPECTED_INVARIANTS = "UNSAFE_EXPECTED_INVARIANTS"


REQUIRED_SCENARIOS: tuple[Scenario, ...] = (
    Scenario.MULTI_TURN_INTAKE,
    Scenario.CORRECTION_RETRACTION,
    Scenario.AMBIGUOUS_SAFETY_NEGATION,
    Scenario.RED_FLAG_ESCALATION,
    Scenario.IDEMPOTENT_REPLAY,
    Scenario.CHECKPOINT_RESUME,
)

INVARIANT_KINDS: tuple[InvariantKind, ...] = (
    InvariantKind.MISSED_SAFETY_ESCALATION,
    InvariantKind.PROTOCOL_VALID,
    InvariantKind.ROUTE_STATE_CONSISTENCY,
    InvariantKind.REPLAY_EQUALITY,
    InvariantKind.BOUNDED_QUESTION_COUNT,
    InvariantKind.PHI_SAFE_PROJECTION,
)

_FAILURE_CODE_BY_INVARIANT: dict[InvariantKind, EvaluationFailureCode] = {
    InvariantKind.MISSED_SAFETY_ESCALATION: EvaluationFailureCode.MISSED_SAFETY_ESCALATION,
    InvariantKind.PROTOCOL_VALID: EvaluationFailureCode.PROTOCOL_VIOLATION,
    InvariantKind.ROUTE_STATE_CONSISTENCY: EvaluationFailureCode.ROUTE_STATE_INCONSISTENCY,
    InvariantKind.REPLAY_EQUALITY: EvaluationFailureCode.REPLAY_INEQUALITY,
    InvariantKind.BOUNDED_QUESTION_COUNT: EvaluationFailureCode.QUESTION_COUNT_UNBOUNDED,
    InvariantKind.PHI_SAFE_PROJECTION: EvaluationFailureCode.PHI_PROJECTION_UNSAFE,
}


class TrajectoryEvaluationError(ValueError):
    """A fixed-code rejection that never carries a domain payload."""

    def __init__(self, code: EvaluationFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


def canonical_json(value: Any) -> str:
    """Return stable sorted-key JSON, rejecting NaN and non-JSON input."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise TrajectoryEvaluationError(EvaluationFailureCode.NOT_JSON) from exc


def _sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _require_sha256_hex(value: str) -> str:
    if len(value) != 64 or any(ch not in _HEX_DIGITS for ch in value):
        raise ValueError("digest must be a 64-character lowercase hex string")
    return value


class TrajectoryStep(BaseModel):
    """One ordered step with only typed symbolic fields and no free text."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    step_id: str = Field(min_length=1, max_length=64, pattern=SYMBOLIC_IDENTIFIER_PATTERN)
    action: StepAction
    route: Route
    state_version: int = Field(ge=0)
    question_count: int = Field(ge=0, le=MAX_QUESTION_COUNT)
    safety_escalated: bool
    protocol_valid: bool
    replay_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=SYMBOLIC_IDENTIFIER_PATTERN,
    )
    checkpoint_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=SYMBOLIC_IDENTIFIER_PATTERN,
    )
    projection_safe: bool


class ExpectedInvariants(BaseModel):
    """The expected outcome of each closed invariant for one trajectory."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    missed_safety_escalation: bool
    protocol_valid: bool
    route_state_consistency: bool
    replay_equality: bool
    bounded_question_count: bool
    phi_safe_projection: bool


class InvariantOutcome(BaseModel):
    """A normalized single-invariant outcome for a report."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    invariant: InvariantKind
    status: InvariantStatus


class Trajectory(BaseModel):
    """A bounded, ordered, digest-bound agent trajectory for offline evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: SchemaVersion = SCHEMA_VERSION
    trajectory_id: str = Field(min_length=1, max_length=64, pattern=SYMBOLIC_IDENTIFIER_PATTERN)
    scenario: Scenario
    steps: tuple[TrajectoryStep, ...] = Field(min_length=1, max_length=MAX_TRAJECTORY_STEPS)
    expected_invariants: ExpectedInvariants
    digest: str = Field(min_length=64, max_length=64)

    @field_validator("digest")
    @classmethod
    def require_sha256_hex(cls, value: str) -> str:
        return _require_sha256_hex(value)

    @model_validator(mode="after")
    def validate_step_order(self) -> Trajectory:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique")
        versions = [step.state_version for step in self.steps]
        if any(left > right for left, right in zip(versions, versions[1:], strict=False)):
            raise ValueError("state_version must be monotonically nondecreasing")
        return self

    @classmethod
    def build(
        cls,
        *,
        trajectory_id: str,
        scenario: Scenario,
        steps: tuple[TrajectoryStep, ...],
        expected_invariants: ExpectedInvariants,
    ) -> Trajectory:
        """Construct a trajectory with its canonical digest bound at build time."""
        draft = cls(
            trajectory_id=trajectory_id,
            scenario=scenario,
            steps=steps,
            expected_invariants=expected_invariants,
            digest=_PLACEHOLDER_DIGEST,
        )
        return draft.model_copy(update={"digest": trajectory_digest(draft)})

    def validate_digest(self) -> None:
        verify_trajectory_digest(self)


class SuiteManifest(BaseModel):
    """A closed six-trajectory suite with an order-independent canonical digest."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: SchemaVersion = SCHEMA_VERSION
    manifest_id: str = Field(min_length=1, max_length=64, pattern=SYMBOLIC_IDENTIFIER_PATTERN)
    trajectories: tuple[Trajectory, ...] = Field(
        min_length=REQUIRED_SCENARIO_COUNT,
        max_length=REQUIRED_SCENARIO_COUNT,
    )
    digest: str = Field(min_length=64, max_length=64)

    @field_validator("digest")
    @classmethod
    def require_sha256_hex(cls, value: str) -> str:
        return _require_sha256_hex(value)

    @model_validator(mode="after")
    def validate_suite(self) -> SuiteManifest:
        ids = [item.trajectory_id for item in self.trajectories]
        if len(ids) != len(set(ids)):
            raise ValueError("trajectory ids must be unique")
        scenarios = [item.scenario for item in self.trajectories]
        for required in REQUIRED_SCENARIOS:
            if scenarios.count(required) != 1:
                raise ValueError("each required scenario must appear exactly once")
        return self

    @classmethod
    def build(cls, *, manifest_id: str, trajectories: tuple[Trajectory, ...]) -> SuiteManifest:
        """Construct a manifest with its order-independent canonical digest bound."""
        draft = cls(
            manifest_id=manifest_id,
            trajectories=trajectories,
            digest=_PLACEHOLDER_DIGEST,
        )
        return draft.model_copy(update={"digest": suite_manifest_digest(draft)})

    def validate_digest(self) -> None:
        verify_manifest_digest(self)


class TrajectoryEvaluationReport(BaseModel):
    """Normalized per-trajectory outcome: identifiers, enums, counts, booleans, codes, digests."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: SchemaVersion = SCHEMA_VERSION
    trajectory_id: str = Field(min_length=1, max_length=64, pattern=SYMBOLIC_IDENTIFIER_PATTERN)
    scenario: Scenario
    step_count: int = Field(ge=0, le=MAX_TRAJECTORY_STEPS)
    question_count_max: int = Field(ge=0, le=MAX_QUESTION_COUNT)
    safety_escalated: bool
    protocol_valid: bool
    projection_safe: bool
    adapter_failure: bool = False
    invariant_outcomes: tuple[InvariantOutcome, ...] = Field(
        min_length=REQUIRED_SCENARIO_COUNT,
        max_length=REQUIRED_SCENARIO_COUNT,
    )
    failure_codes: tuple[EvaluationFailureCode, ...] = Field(
        default=(),
        max_length=MAX_REPORT_FAILURE_CODES,
    )
    trajectory_digest: str = Field(min_length=64, max_length=64)
    observed_steps_digest: str = Field(min_length=64, max_length=64)
    digest: str = Field(min_length=64, max_length=64)

    @field_validator("trajectory_digest", "observed_steps_digest", "digest")
    @classmethod
    def require_sha256_hex(cls, value: str) -> str:
        return _require_sha256_hex(value)

    @model_validator(mode="after")
    def validate_outcomes_and_failures(self) -> TrajectoryEvaluationReport:
        invariants = [outcome.invariant for outcome in self.invariant_outcomes]
        if len(invariants) != len(set(invariants)):
            raise ValueError("duplicate invariant outcome")
        if frozenset(invariants) != frozenset(INVARIANT_KINDS):
            raise ValueError("invariant outcomes must cover every invariant exactly once")
        expected_failures = frozenset(
            _FAILURE_CODE_BY_INVARIANT[outcome.invariant]
            for outcome in self.invariant_outcomes
            if outcome.status is InvariantStatus.VIOLATED
        )
        if self.adapter_failure:
            expected_failures |= frozenset({EvaluationFailureCode.ADAPTER_FAILURE})
        if len(self.failure_codes) != len(set(self.failure_codes)):
            raise ValueError("failure_codes must be unique")
        if frozenset(self.failure_codes) != expected_failures:
            raise ValueError("failure_codes must match the violated invariants and adapter failure exactly")
        return self

    @classmethod
    def build(
        cls,
        *,
        trajectory_id: str,
        scenario: Scenario,
        step_count: int,
        question_count_max: int,
        safety_escalated: bool,
        protocol_valid: bool,
        projection_safe: bool,
        invariant_outcomes: tuple[InvariantOutcome, ...],
        trajectory_digest: str,
        observed_steps_digest: str,
        adapter_failure: bool = False,
        failure_codes: tuple[EvaluationFailureCode, ...] = (),
    ) -> TrajectoryEvaluationReport:
        """Construct a report with its canonical digest bound."""
        draft = cls(
            trajectory_id=trajectory_id,
            scenario=scenario,
            step_count=step_count,
            question_count_max=question_count_max,
            safety_escalated=safety_escalated,
            protocol_valid=protocol_valid,
            projection_safe=projection_safe,
            adapter_failure=adapter_failure,
            invariant_outcomes=invariant_outcomes,
            failure_codes=failure_codes,
            trajectory_digest=trajectory_digest,
            observed_steps_digest=observed_steps_digest,
            digest=_PLACEHOLDER_DIGEST,
        )
        return draft.model_copy(update={"digest": trajectory_report_digest(draft)})

    def validate_digest(self) -> None:
        verify_trajectory_report_digest(self)


class SuiteEvaluationReport(BaseModel):
    """Normalized whole-suite outcome referencing per-trajectory reports."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: SchemaVersion = SCHEMA_VERSION
    manifest_id: str = Field(min_length=1, max_length=64, pattern=SYMBOLIC_IDENTIFIER_PATTERN)
    trajectory_count: int = Field(ge=REQUIRED_SCENARIO_COUNT, le=REQUIRED_SCENARIO_COUNT)
    passed_count: int = Field(ge=0, le=REQUIRED_SCENARIO_COUNT)
    failed_count: int = Field(ge=0, le=REQUIRED_SCENARIO_COUNT)
    reports: tuple[TrajectoryEvaluationReport, ...] = Field(
        min_length=REQUIRED_SCENARIO_COUNT,
        max_length=REQUIRED_SCENARIO_COUNT,
    )
    failure_codes: tuple[EvaluationFailureCode, ...] = Field(
        default=(),
        max_length=MAX_REPORT_FAILURE_CODES,
    )
    manifest_digest: str = Field(min_length=64, max_length=64)
    digest: str = Field(min_length=64, max_length=64)

    @field_validator("manifest_digest", "digest")
    @classmethod
    def require_sha256_hex(cls, value: str) -> str:
        return _require_sha256_hex(value)

    @model_validator(mode="after")
    def validate_counts(self) -> SuiteEvaluationReport:
        if len(self.reports) != self.trajectory_count:
            raise ValueError("trajectory_count must equal the number of reports")
        if self.passed_count + self.failed_count != self.trajectory_count:
            raise ValueError("passed_count plus failed_count must equal trajectory_count")
        if sum(1 for item in self.reports if item.failure_codes) != self.failed_count:
            raise ValueError("failed_count must equal the number of failing reports")
        report_ids = [item.trajectory_id for item in self.reports]
        if len(report_ids) != len(set(report_ids)):
            raise ValueError("report trajectory ids must be unique")
        expected_failures = frozenset(code for item in self.reports for code in item.failure_codes)
        if len(self.failure_codes) != len(set(self.failure_codes)):
            raise ValueError("failure_codes must be unique")
        if frozenset(self.failure_codes) != expected_failures:
            raise ValueError("failure_codes must be the union of report failure_codes")
        return self

    @classmethod
    def build(
        cls,
        *,
        manifest_id: str,
        reports: tuple[TrajectoryEvaluationReport, ...],
        manifest_digest: str,
    ) -> SuiteEvaluationReport:
        """Construct a suite report with counts and canonical digest derived."""
        failed_count = sum(1 for item in reports if item.failure_codes)
        failure_codes = tuple(
            sorted(
                {code for item in reports for code in item.failure_codes},
                key=lambda code: code.value,
            )
        )
        draft = cls(
            manifest_id=manifest_id,
            trajectory_count=len(reports),
            passed_count=len(reports) - failed_count,
            failed_count=failed_count,
            reports=reports,
            failure_codes=failure_codes,
            manifest_digest=manifest_digest,
            digest=_PLACEHOLDER_DIGEST,
        )
        return draft.model_copy(update={"digest": suite_report_digest(draft)})

    def validate_digest(self) -> None:
        verify_suite_report_digest(self)


def _trajectory_payload(trajectory: Trajectory) -> dict[str, Any]:
    return {
        "schema": trajectory.schema_version,
        "scenario": trajectory.scenario.value,
        "steps": [step.model_dump(mode="json") for step in trajectory.steps],
        "expected_invariants": trajectory.expected_invariants.model_dump(mode="json"),
    }


def trajectory_digest(trajectory: Trajectory) -> str:
    """Canonical SHA-256 digest over the behavior-relevant trajectory fields."""
    return _sha256(_trajectory_payload(trajectory))


def verify_trajectory_digest(trajectory: Trajectory) -> None:
    """Raise a fixed-code error when the stored trajectory digest is stale."""
    if trajectory_digest(trajectory) != trajectory.digest:
        raise TrajectoryEvaluationError(EvaluationFailureCode.TRAJECTORY_DIGEST_MISMATCH)


def _manifest_payload(manifest: SuiteManifest) -> dict[str, Any]:
    entries = sorted(
        ({"trajectory_id": item.trajectory_id, "digest": item.digest} for item in manifest.trajectories),
        key=lambda entry: entry["trajectory_id"],
    )
    return {"schema": manifest.schema_version, "trajectories": entries}


def suite_manifest_digest(manifest: SuiteManifest) -> str:
    """Canonical digest over trajectory references, sorted by trajectory_id."""
    return _sha256(_manifest_payload(manifest))


def verify_manifest_digest(manifest: SuiteManifest) -> None:
    """Raise a fixed-code error when the stored manifest digest is stale."""
    if suite_manifest_digest(manifest) != manifest.digest:
        raise TrajectoryEvaluationError(EvaluationFailureCode.MANIFEST_DIGEST_MISMATCH)


def _trajectory_report_payload(report: TrajectoryEvaluationReport) -> dict[str, Any]:
    return {
        "schema": report.schema_version,
        "trajectory_id": report.trajectory_id,
        "scenario": report.scenario.value,
        "step_count": report.step_count,
        "question_count_max": report.question_count_max,
        "safety_escalated": report.safety_escalated,
        "protocol_valid": report.protocol_valid,
        "projection_safe": report.projection_safe,
        "adapter_failure": report.adapter_failure,
        "invariant_outcomes": [outcome.model_dump(mode="json") for outcome in report.invariant_outcomes],
        "failure_codes": [code.value for code in report.failure_codes],
        "trajectory_digest": report.trajectory_digest,
        "observed_steps_digest": report.observed_steps_digest,
    }


def trajectory_report_digest(report: TrajectoryEvaluationReport) -> str:
    """Canonical digest binding the full normalized per-trajectory outcome."""
    return _sha256(_trajectory_report_payload(report))


def verify_trajectory_report_digest(report: TrajectoryEvaluationReport) -> None:
    """Raise a fixed-code error when the stored report digest is stale."""
    if trajectory_report_digest(report) != report.digest:
        raise TrajectoryEvaluationError(EvaluationFailureCode.TRAJECTORY_DIGEST_MISMATCH)


def _suite_report_payload(report: SuiteEvaluationReport) -> dict[str, Any]:
    entries = sorted(
        ({"trajectory_id": item.trajectory_id, "digest": item.digest} for item in report.reports),
        key=lambda entry: entry["trajectory_id"],
    )
    return {
        "schema": report.schema_version,
        "manifest_id": report.manifest_id,
        "trajectory_count": report.trajectory_count,
        "passed_count": report.passed_count,
        "failed_count": report.failed_count,
        "reports": entries,
        "failure_codes": [code.value for code in report.failure_codes],
        "manifest_digest": report.manifest_digest,
    }


def suite_report_digest(report: SuiteEvaluationReport) -> str:
    """Canonical digest binding the normalized whole-suite outcome."""
    return _sha256(_suite_report_payload(report))


def verify_suite_report_digest(report: SuiteEvaluationReport) -> None:
    """Raise a fixed-code error when the stored suite-report digest is stale."""
    if suite_report_digest(report) != report.digest:
        raise TrajectoryEvaluationError(EvaluationFailureCode.MANIFEST_DIGEST_MISMATCH)


def satisfied_invariant_outcomes() -> tuple[InvariantOutcome, ...]:
    """The canonical six invariant outcomes, all satisfied."""
    return tuple(InvariantOutcome(invariant=kind, status=InvariantStatus.SATISFIED) for kind in INVARIANT_KINDS)


def violated_invariant_outcomes(*violated: InvariantKind) -> tuple[InvariantOutcome, ...]:
    """The canonical six invariant outcomes with exactly ``violated`` marked VIOLATED."""
    violated_set = frozenset(violated)
    return tuple(
        InvariantOutcome(
            invariant=kind,
            status=InvariantStatus.VIOLATED if kind in violated_set else InvariantStatus.SATISFIED,
        )
        for kind in INVARIANT_KINDS
    )


def failure_codes_for_outcomes(outcomes: tuple[InvariantOutcome, ...]) -> tuple[EvaluationFailureCode, ...]:
    """Derive the exact failure-code set a report must carry for these outcomes."""
    return tuple(
        _FAILURE_CODE_BY_INVARIANT[outcome.invariant]
        for outcome in outcomes
        if outcome.status is InvariantStatus.VIOLATED
    )


def model_canonical_json(model: BaseModel) -> str:
    """Stable canonical JSON for any frozen contract model."""
    return canonical_json(model.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# R3-A2: pure deterministic evaluator engine
# ---------------------------------------------------------------------------

#: The only expectations a golden manifest may declare: no safety miss, every
#: other invariant satisfied. A manifest that blesses any other outcome cannot
#: be a regression gate and is rejected before any executor runs.
SAFE_EXPECTED_INVARIANTS = ExpectedInvariants(
    missed_safety_escalation=False,
    protocol_valid=True,
    route_state_consistency=True,
    replay_equality=True,
    bounded_question_count=True,
    phi_safe_projection=True,
)


@runtime_checkable
class TrajectoryExecutor(Protocol):
    """A pure callable mapping one immutable Trajectory to normalized steps."""

    def __call__(self, trajectory: Trajectory) -> tuple[TrajectoryStep, ...]:
        """Return the normalized ordered steps observed for a trajectory."""
        ...


def recorded_steps_executor(trajectory: Trajectory) -> tuple[TrajectoryStep, ...]:
    """Replay the recorded steps unchanged — the default pure executor."""
    return trajectory.steps


def observed_steps_digest(steps: tuple[TrajectoryStep, ...]) -> str:
    """Canonical SHA-256 binding a normalized observed step sequence."""
    return _sha256({"steps": [step.model_dump(mode="json") for step in steps]})


#: Closed action-to-route compatibility map. Every ``StepAction`` maps to the
#: exact routes the protocol permits; an action may never take an unlisted route.
_ACTION_ROUTE_COMPATIBILITY: dict[StepAction, frozenset[Route]] = {
    StepAction.OBSERVE: frozenset({Route.INTAKE}),
    StepAction.CLARIFY: frozenset({Route.CLARIFICATION}),
    StepAction.EXTRACT: frozenset({Route.INTAKE}),
    StepAction.REASON: frozenset({Route.REASONING, Route.SAFETY}),
    StepAction.ESCALATE: frozenset({Route.ESCALATION, Route.RED_FLAG}),
    StepAction.RESPOND: frozenset({Route.INTAKE, Route.REPLAY, Route.ESCALATION, Route.TERMINAL}),
    StepAction.REPLAY: frozenset({Route.REPLAY}),
    StepAction.RESUME: frozenset({Route.RESUME}),
    StepAction.PROJECT: frozenset({Route.PROJECTION}),
}


def _check_missed_safety_escalation(
    scenario: Scenario,
    steps: tuple[TrajectoryStep, ...],
) -> bool:
    """Only a red-flag scenario demands a safety-escalated RED_FLAG/ESCALATION step."""
    if scenario is not Scenario.RED_FLAG_ESCALATION:
        return True
    return any(step.safety_escalated and step.route in (Route.RED_FLAG, Route.ESCALATION) for step in steps)


def _check_protocol_valid(steps: tuple[TrajectoryStep, ...]) -> bool:
    return all(step.protocol_valid for step in steps)


def _check_route_state_consistency(
    scenario: Scenario,
    steps: tuple[TrajectoryStep, ...],
) -> bool:
    """Closed action/route map, monotonic state_version, scenario requirements."""
    for step in steps:
        if step.route not in _ACTION_ROUTE_COMPATIBILITY[step.action]:
            return False
    versions = [step.state_version for step in steps]
    if any(left > right for left, right in zip(versions, versions[1:], strict=False)):
        return False
    if scenario is Scenario.MULTI_TURN_INTAKE:
        return any(step.route is Route.TERMINAL for step in steps)
    if scenario is Scenario.CORRECTION_RETRACTION:
        return any(step.action is StepAction.PROJECT for step in steps)
    if scenario is Scenario.AMBIGUOUS_SAFETY_NEGATION:
        return any(step.action is StepAction.CLARIFY and step.route is Route.CLARIFICATION for step in steps)
    if scenario is Scenario.RED_FLAG_ESCALATION:
        return any(
            step.action is StepAction.ESCALATE and step.route in (Route.RED_FLAG, Route.ESCALATION) for step in steps
        )
    if scenario is Scenario.IDEMPOTENT_REPLAY:
        return sum(1 for step in steps if step.replay_ref is not None) >= 2
    if scenario is Scenario.CHECKPOINT_RESUME:
        for index, step in enumerate(steps):
            if (
                step.action is StepAction.RESUME
                and step.route is Route.RESUME
                and step.checkpoint_ref is not None
                and any(previous.checkpoint_ref == step.checkpoint_ref for previous in steps[:index])
            ):
                return True
        return False
    return True


def _replay_norm(step: TrajectoryStep) -> tuple[object, ...]:
    """The replay-equality behavior of a step, excluding its step_id."""
    return (
        step.action,
        step.route,
        step.state_version,
        step.question_count,
        step.safety_escalated,
        step.protocol_valid,
        step.replay_ref,
        step.checkpoint_ref,
        step.projection_safe,
    )


def _check_replay_equality(
    scenario: Scenario,
    steps: tuple[TrajectoryStep, ...],
) -> bool:
    """Repeated replay refs must be identical modulo step_id; idempotent replay repeats."""
    groups: dict[str, list[TrajectoryStep]] = {}
    for step in steps:
        if step.replay_ref is None:
            continue
        groups.setdefault(step.replay_ref, []).append(step)
    for replayed in groups.values():
        if len(replayed) > 1:
            norms = {_replay_norm(step) for step in replayed}
            if len(norms) != 1:
                return False
    if scenario is Scenario.IDEMPOTENT_REPLAY:
        return any(len(replayed) >= 2 for replayed in groups.values())
    return True


def _check_bounded_question_count(steps: tuple[TrajectoryStep, ...]) -> bool:
    if any(step.question_count > MAX_QUESTION_COUNT for step in steps):
        return False
    previous: int | None = None
    for step in steps:
        if previous is not None:
            delta = step.question_count - previous
            if delta < 0 or delta > 1:
                return False
        previous = step.question_count
    return True


def _check_phi_safe_projection(steps: tuple[TrajectoryStep, ...]) -> bool:
    return all(step.projection_safe for step in steps)


def _evaluate_invariants(
    scenario: Scenario,
    steps: tuple[TrajectoryStep, ...],
) -> tuple[InvariantOutcome, ...]:
    """Decide every invariant deterministically from observed typed steps."""
    verdicts: dict[InvariantKind, bool] = {
        InvariantKind.MISSED_SAFETY_ESCALATION: _check_missed_safety_escalation(scenario, steps),
        InvariantKind.PROTOCOL_VALID: _check_protocol_valid(steps),
        InvariantKind.ROUTE_STATE_CONSISTENCY: _check_route_state_consistency(scenario, steps),
        InvariantKind.REPLAY_EQUALITY: _check_replay_equality(scenario, steps),
        InvariantKind.BOUNDED_QUESTION_COUNT: _check_bounded_question_count(steps),
        InvariantKind.PHI_SAFE_PROJECTION: _check_phi_safe_projection(steps),
    }
    return tuple(
        InvariantOutcome(
            invariant=kind,
            status=InvariantStatus.SATISFIED if verdicts[kind] else InvariantStatus.VIOLATED,
        )
        for kind in INVARIANT_KINDS
    )


def _require_safe_expectations(trajectory: Trajectory) -> None:
    """Fail closed when a manifest tries to bless anything but the safe outcomes."""
    if trajectory.expected_invariants != SAFE_EXPECTED_INVARIANTS:
        raise TrajectoryEvaluationError(EvaluationFailureCode.UNSAFE_EXPECTED_INVARIANTS)


def _normalize_observed_steps(output: Any) -> tuple[TrajectoryStep, ...]:
    """Validate an executor result and re-verify every step against the contract."""
    if not isinstance(output, tuple):
        raise ValueError("executor must return a tuple of TrajectoryStep")
    if not (1 <= len(output) <= MAX_TRAJECTORY_STEPS):
        raise ValueError("executor returned an out-of-bounds step count")
    normalized: list[TrajectoryStep] = []
    for item in output:
        if not isinstance(item, TrajectoryStep):
            raise ValueError("executor returned a non-TrajectoryStep item")
        normalized.append(TrajectoryStep.model_validate(item.model_dump()))
    return tuple(normalized)


def _adapter_failure_report(trajectory: Trajectory) -> TrajectoryEvaluationReport:
    """A fail-closed report: no invariant verified, adapter failure flagged."""
    outcomes = violated_invariant_outcomes(*INVARIANT_KINDS)
    return TrajectoryEvaluationReport.build(
        trajectory_id=trajectory.trajectory_id,
        scenario=trajectory.scenario,
        step_count=0,
        question_count_max=0,
        safety_escalated=False,
        protocol_valid=False,
        projection_safe=False,
        invariant_outcomes=outcomes,
        failure_codes=failure_codes_for_outcomes(outcomes) + (EvaluationFailureCode.ADAPTER_FAILURE,),
        trajectory_digest=trajectory.digest,
        observed_steps_digest=observed_steps_digest(()),
        adapter_failure=True,
    )


def evaluate_trajectory(
    trajectory: Trajectory,
    executor: TrajectoryExecutor = recorded_steps_executor,
) -> TrajectoryEvaluationReport:
    """Evaluate one trajectory deterministically, isolating executor failures."""
    verify_trajectory_digest(trajectory)
    _require_safe_expectations(trajectory)
    try:
        steps = _normalize_observed_steps(executor(trajectory))
    except Exception:
        return _adapter_failure_report(trajectory)
    outcomes = _evaluate_invariants(trajectory.scenario, steps)
    return TrajectoryEvaluationReport.build(
        trajectory_id=trajectory.trajectory_id,
        scenario=trajectory.scenario,
        step_count=len(steps),
        question_count_max=max((step.question_count for step in steps), default=0),
        safety_escalated=any(step.safety_escalated for step in steps),
        protocol_valid=all(step.protocol_valid for step in steps),
        projection_safe=all(step.projection_safe for step in steps),
        invariant_outcomes=outcomes,
        failure_codes=failure_codes_for_outcomes(outcomes),
        trajectory_digest=trajectory.digest,
        observed_steps_digest=observed_steps_digest(steps),
    )


def evaluate_suite(
    manifest: SuiteManifest,
    executor: TrajectoryExecutor = recorded_steps_executor,
) -> SuiteEvaluationReport:
    """Evaluate a whole manifest in stable trajectory_id order, failing closed."""
    verify_manifest_digest(manifest)
    ordered = tuple(sorted(manifest.trajectories, key=lambda item: item.trajectory_id))
    for trajectory in ordered:
        verify_trajectory_digest(trajectory)
        _require_safe_expectations(trajectory)
    reports = tuple(evaluate_trajectory(trajectory, executor) for trajectory in ordered)
    return SuiteEvaluationReport.build(
        manifest_id=manifest.manifest_id,
        reports=reports,
        manifest_digest=manifest.digest,
    )


def expected_invariants_match(
    outcomes: tuple[InvariantOutcome, ...],
    expected: ExpectedInvariants,
) -> bool:
    """True when every actual invariant verdict equals the expected invariant.

    ``missed_safety_escalation`` is the one flag phrased as a failure: True
    means a safety miss *is* expected, i.e. the invariant is violated. Every
    other flag is phrased as success: True means the invariant is satisfied.
    """
    actual = {outcome.invariant: outcome.status for outcome in outcomes}
    return (
        (actual[InvariantKind.MISSED_SAFETY_ESCALATION] is InvariantStatus.VIOLATED)
        == expected.missed_safety_escalation
        and (actual[InvariantKind.PROTOCOL_VALID] is InvariantStatus.SATISFIED) == expected.protocol_valid
        and (actual[InvariantKind.ROUTE_STATE_CONSISTENCY] is InvariantStatus.SATISFIED)
        == expected.route_state_consistency
        and (actual[InvariantKind.REPLAY_EQUALITY] is InvariantStatus.SATISFIED) == expected.replay_equality
        and (actual[InvariantKind.BOUNDED_QUESTION_COUNT] is InvariantStatus.SATISFIED)
        == expected.bounded_question_count
        and (actual[InvariantKind.PHI_SAFE_PROJECTION] is InvariantStatus.SATISFIED) == expected.phi_safe_projection
    )
