"""R3-A1 trajectory-evaluation contract tests.

These tests pin the offline evaluation *contract* only: strict frozen models,
bounded typed fields, canonical digests, and the closed scenario/enum sets.
Six synthetic symbolic trajectories are built in code; there is no fixture
JSON and no evaluator engine yet.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from app.agent_runtime.trajectory_evaluation import (
    MAX_QUESTION_COUNT,
    MAX_TRAJECTORY_STEPS,
    REQUIRED_SCENARIOS,
    SAFE_EXPECTED_INVARIANTS,
    SCHEMA_VERSION,
    EvaluationFailureCode,
    ExpectedInvariants,
    InvariantKind,
    InvariantOutcome,
    InvariantStatus,
    Route,
    Scenario,
    StepAction,
    SuiteEvaluationReport,
    SuiteManifest,
    Trajectory,
    TrajectoryEvaluationError,
    TrajectoryEvaluationReport,
    TrajectoryExecutor,
    TrajectoryStep,
    canonical_json,
    evaluate_suite,
    evaluate_trajectory,
    expected_invariants_match,
    failure_codes_for_outcomes,
    model_canonical_json,
    observed_steps_digest,
    recorded_steps_executor,
    satisfied_invariant_outcomes,
    suite_manifest_digest,
    trajectory_digest,
    violated_invariant_outcomes,
)

FORBIDDEN_FIELD_SUBSTRINGS = (
    "text",
    "prompt",
    "output",
    "timestamp",
    "uuid",
    "trace",
    "session",
    "run_id",
    "payload",
    "message",
    "content",
    "raw",
    "volatile",
    "data",
    "value",
)

ALL_MODELS: tuple[type[BaseModel], ...] = (
    TrajectoryStep,
    ExpectedInvariants,
    InvariantOutcome,
    Trajectory,
    SuiteManifest,
    TrajectoryEvaluationReport,
    SuiteEvaluationReport,
)


def _step(
    step_id: str,
    *,
    action: StepAction = StepAction.OBSERVE,
    route: Route = Route.INTAKE,
    state_version: int = 0,
    question_count: int = 0,
    safety_escalated: bool = False,
    protocol_valid: bool = True,
    replay_ref: str | None = None,
    checkpoint_ref: str | None = None,
    projection_safe: bool = True,
) -> TrajectoryStep:
    return TrajectoryStep(
        step_id=step_id,
        action=action,
        route=route,
        state_version=state_version,
        question_count=question_count,
        safety_escalated=safety_escalated,
        protocol_valid=protocol_valid,
        replay_ref=replay_ref,
        checkpoint_ref=checkpoint_ref,
        projection_safe=projection_safe,
    )


def _expected(**flags: bool) -> ExpectedInvariants:
    defaults: dict[str, bool] = {
        "missed_safety_escalation": False,
        "protocol_valid": True,
        "route_state_consistency": True,
        "replay_equality": True,
        "bounded_question_count": True,
        "phi_safe_projection": True,
    }
    defaults.update(flags)
    return ExpectedInvariants(**defaults)


def _trajectory(
    trajectory_id: str,
    scenario: Scenario,
    steps: tuple[TrajectoryStep, ...],
    **flags: bool,
) -> Trajectory:
    return Trajectory.build(
        trajectory_id=trajectory_id,
        scenario=scenario,
        steps=steps,
        expected_invariants=_expected(**flags),
    )


def _six_trajectories() -> tuple[Trajectory, ...]:
    return (
        _trajectory(
            "traj.multi_turn_intake",
            Scenario.MULTI_TURN_INTAKE,
            (
                _step("s1", action=StepAction.CLARIFY, route=Route.CLARIFICATION, state_version=0, question_count=1),
                _step("s2", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=1, question_count=2),
                _step("s3", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=2, question_count=3),
                _step("s4", action=StepAction.RESPOND, route=Route.TERMINAL, state_version=3, question_count=3),
            ),
        ),
        _trajectory(
            "traj.correction_retraction",
            Scenario.CORRECTION_RETRACTION,
            (
                _step("s1", action=StepAction.OBSERVE, route=Route.INTAKE, state_version=0, question_count=1),
                _step("s2", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=1, question_count=1),
                _step("s3", action=StepAction.OBSERVE, route=Route.INTAKE, state_version=2, question_count=1),
                _step("s4", action=StepAction.PROJECT, route=Route.PROJECTION, state_version=3, question_count=1),
            ),
        ),
        _trajectory(
            "traj.ambiguous_safety_negation",
            Scenario.AMBIGUOUS_SAFETY_NEGATION,
            (
                _step("s1", action=StepAction.REASON, route=Route.SAFETY, state_version=0, question_count=0),
                _step("s2", action=StepAction.CLARIFY, route=Route.CLARIFICATION, state_version=1, question_count=1),
                _step("s3", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=2, question_count=2),
            ),
        ),
        _trajectory(
            "traj.red_flag_escalation",
            Scenario.RED_FLAG_ESCALATION,
            (
                _step("s1", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=0, question_count=1),
                _step(
                    "s2",
                    action=StepAction.ESCALATE,
                    route=Route.RED_FLAG,
                    state_version=1,
                    question_count=1,
                    safety_escalated=True,
                ),
            ),
        ),
        _trajectory(
            "traj.idempotent_replay",
            Scenario.IDEMPOTENT_REPLAY,
            (
                _step(
                    "s1",
                    action=StepAction.RESPOND,
                    route=Route.REPLAY,
                    state_version=1,
                    question_count=0,
                    replay_ref="replay.1",
                ),
                _step(
                    "s2",
                    action=StepAction.RESPOND,
                    route=Route.REPLAY,
                    state_version=1,
                    question_count=0,
                    replay_ref="replay.1",
                ),
            ),
        ),
        _trajectory(
            "traj.checkpoint_resume",
            Scenario.CHECKPOINT_RESUME,
            (
                _step(
                    "s1",
                    action=StepAction.RESPOND,
                    route=Route.INTAKE,
                    state_version=0,
                    question_count=2,
                    checkpoint_ref="ckpt.1",
                ),
                _step(
                    "s2",
                    action=StepAction.RESUME,
                    route=Route.RESUME,
                    state_version=1,
                    question_count=2,
                    replay_ref="replay.1",
                    checkpoint_ref="ckpt.1",
                ),
            ),
        ),
    )


def _report(
    *,
    trajectory_id: str = "traj.multi_turn_intake",
    scenario: Scenario = Scenario.MULTI_TURN_INTAKE,
    step_count: int = 3,
    question_count_max: int = 3,
    safety_escalated: bool = False,
    protocol_valid: bool = True,
    projection_safe: bool = True,
    invariant_outcomes: tuple[InvariantOutcome, ...] | None = None,
    failure_codes: tuple[EvaluationFailureCode, ...] | None = None,
    trajectory_digest: str = "0" * 64,
    observed_digest: str = "0" * 64,
    adapter_failure: bool = False,
) -> TrajectoryEvaluationReport:
    outcomes = invariant_outcomes if invariant_outcomes is not None else satisfied_invariant_outcomes()
    codes = failure_codes if failure_codes is not None else failure_codes_for_outcomes(outcomes)
    return TrajectoryEvaluationReport.build(
        trajectory_id=trajectory_id,
        scenario=scenario,
        step_count=step_count,
        question_count_max=question_count_max,
        safety_escalated=safety_escalated,
        protocol_valid=protocol_valid,
        projection_safe=projection_safe,
        invariant_outcomes=outcomes,
        failure_codes=codes,
        trajectory_digest=trajectory_digest,
        observed_steps_digest=observed_digest,
        adapter_failure=adapter_failure,
    )


def _six_reports() -> tuple[TrajectoryEvaluationReport, ...]:
    return tuple(_report(trajectory_id=f"traj.{scenario.value}", scenario=scenario) for scenario in Scenario)


# ---------------------------------------------------------------------------
# schema literal and closed scenario set
# ---------------------------------------------------------------------------


def test_schema_literal_is_closed() -> None:
    assert SCHEMA_VERSION == "agent-trajectory-eval.v1"
    with pytest.raises(ValidationError):
        Trajectory(
            schema_version="agent-trajectory-eval.v2",  # type: ignore[arg-type]
            trajectory_id="t",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=(_step("s1"),),
            expected_invariants=_expected(),
            digest="0" * 64,
        )


def test_exactly_six_scenarios() -> None:
    assert len(Scenario) == 6
    assert {member.value for member in Scenario} == {
        "multi_turn_intake",
        "correction_retraction",
        "ambiguous_safety_negation",
        "red_flag_escalation",
        "idempotent_replay",
        "checkpoint_resume",
    }
    assert set(Scenario) == set(REQUIRED_SCENARIOS)


def test_six_synthetic_trajectories_are_distinct_and_valid() -> None:
    six = _six_trajectories()
    assert len(six) == 6
    assert len({item.trajectory_id for item in six}) == 6
    assert {item.scenario for item in six} == set(Scenario)
    for item in six:
        assert item.steps
        item.validate_digest()


# ---------------------------------------------------------------------------
# strictness and coercion
# ---------------------------------------------------------------------------


def test_strict_no_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TrajectoryStep(
            step_id="s1",
            action=StepAction.OBSERVE,
            route=Route.INTAKE,
            state_version=0,
            question_count=0,
            safety_escalated=False,
            protocol_valid=True,
            projection_safe=True,
            extra_field="forbidden",  # type: ignore[call-arg]
        )
    schema = TrajectoryStep.model_json_schema()
    assert schema.get("additionalProperties") is False
    assert set(schema["properties"]) == {
        "step_id",
        "action",
        "route",
        "state_version",
        "question_count",
        "safety_escalated",
        "protocol_valid",
        "replay_ref",
        "checkpoint_ref",
        "projection_safe",
    }


def test_no_coercion_anywhere() -> None:
    with pytest.raises(ValidationError):
        _step("s1", state_version="0")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _step("s1", question_count=True)
    with pytest.raises(ValidationError):
        TrajectoryStep(
            step_id=123,  # type: ignore[arg-type]
            action=StepAction.OBSERVE,
            route=Route.INTAKE,
            state_version=0,
            question_count=0,
            safety_escalated=False,
            protocol_valid=True,
            projection_safe=True,
        )
    with pytest.raises(ValidationError):
        # An enum must be a member, never a raw string value.
        TrajectoryStep(
            step_id="s1",
            action="observe",  # type: ignore[arg-type]
            route=Route.INTAKE,
            state_version=0,
            question_count=0,
            safety_escalated=False,
            protocol_valid=True,
            projection_safe=True,
        )
    with pytest.raises(ValidationError):
        # A list would silently coerce into the tuple; strict mode forbids it.
        Trajectory(
            trajectory_id="t",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=[_step("s1")],  # type: ignore[arg-type]
            expected_invariants=_expected(),
            digest="0" * 64,
        )
    with pytest.raises(ValidationError):
        _expected(missed_safety_escalation=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


def test_step_identifier_bounds() -> None:
    with pytest.raises(ValidationError):
        _step("")
    with pytest.raises(ValidationError):
        _step("s" * 65)
    with pytest.raises(ValidationError):
        _step("has space")
    assert _step("s1", replay_ref=None).replay_ref is None
    with pytest.raises(ValidationError):
        _step("s1", replay_ref="")
    with pytest.raises(ValidationError):
        _step("s1", checkpoint_ref="c" * 65)


def test_step_numeric_bounds() -> None:
    with pytest.raises(ValidationError):
        _step("s1", state_version=-1)
    with pytest.raises(ValidationError):
        _step("s1", question_count=-1)
    with pytest.raises(ValidationError):
        _step("s1", question_count=MAX_QUESTION_COUNT + 1)
    assert _step("s1", question_count=MAX_QUESTION_COUNT).question_count == MAX_QUESTION_COUNT


def test_trajectory_step_tuple_bounds() -> None:
    with pytest.raises(ValidationError):
        Trajectory.build(
            trajectory_id="t",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=(),
            expected_invariants=_expected(),
        )
    too_many = tuple(_step(f"s{i}") for i in range(MAX_TRAJECTORY_STEPS + 1))
    with pytest.raises(ValidationError):
        Trajectory.build(
            trajectory_id="t",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=too_many,
            expected_invariants=_expected(),
        )


def test_report_numeric_bounds() -> None:
    with pytest.raises(ValidationError):
        _report(step_count=-1)
    with pytest.raises(ValidationError):
        _report(step_count=MAX_TRAJECTORY_STEPS + 1)
    with pytest.raises(ValidationError):
        _report(question_count_max=MAX_QUESTION_COUNT + 1)


def test_identifier_length_bounds() -> None:
    with pytest.raises(ValidationError):
        Trajectory.build(
            trajectory_id="",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=(_step("s1"),),
            expected_invariants=_expected(),
        )
    with pytest.raises(ValidationError):
        SuiteManifest.build(manifest_id="m" * 65, trajectories=_six_trajectories())


# ---------------------------------------------------------------------------
# digests
# ---------------------------------------------------------------------------


def test_digest_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        Trajectory(
            trajectory_id="t",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=(_step("s1"),),
            expected_invariants=_expected(),
            digest="z" * 64,
        )
    with pytest.raises(ValidationError):
        Trajectory(
            trajectory_id="t",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=(_step("s1"),),
            expected_invariants=_expected(),
            digest="0" * 63,
        )


def test_digest_binds_behavior_not_identifier() -> None:
    traj = _six_trajectories()[0]
    renamed = traj.model_copy(update={"trajectory_id": "another.id"})
    assert trajectory_digest(renamed) == trajectory_digest(traj)
    assert renamed.digest == traj.digest
    renamed.validate_digest()


def test_digest_tamper_detected() -> None:
    traj = _six_trajectories()[0]
    traj.validate_digest()
    tampered = traj.model_copy(
        update={
            "steps": tuple(
                step.model_copy(update={"question_count": step.question_count + 1}) if step.step_id == "s1" else step
                for step in traj.steps
            )
        }
    )
    assert trajectory_digest(tampered) != tampered.digest
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        tampered.validate_digest()
    assert exc_info.value.code is EvaluationFailureCode.TRAJECTORY_DIGEST_MISMATCH


def test_manifest_digest_tamper_detected() -> None:
    six = _six_trajectories()
    manifest = SuiteManifest.build(manifest_id="suite.v1", trajectories=six)
    manifest.validate_digest()
    tampered_trajectory = six[0].model_copy(update={"digest": "1" * 64})
    tampered = SuiteManifest(
        manifest_id="suite.v1",
        trajectories=tuple([tampered_trajectory] + list(six[1:])),
        digest=manifest.digest,
    )
    assert suite_manifest_digest(tampered) != tampered.digest
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        tampered.validate_digest()
    assert exc_info.value.code is EvaluationFailureCode.MANIFEST_DIGEST_MISMATCH


def test_report_digest_tamper_detected() -> None:
    report = _report()
    report.validate_digest()
    tampered = report.model_copy(update={"step_count": report.step_count + 1})
    with pytest.raises(TrajectoryEvaluationError):
        tampered.validate_digest()


# ---------------------------------------------------------------------------
# structural validators
# ---------------------------------------------------------------------------


def test_duplicate_step_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        Trajectory.build(
            trajectory_id="t",
            scenario=Scenario.MULTI_TURN_INTAKE,
            steps=(_step("s1"), _step("s1")),
            expected_invariants=_expected(),
        )


def test_state_version_monotonic_nondecreasing() -> None:
    # Equal versions are legal (idempotent replay keeps the same version)...
    Trajectory.build(
        trajectory_id="t",
        scenario=Scenario.IDEMPOTENT_REPLAY,
        steps=(_step("s1", state_version=1), _step("s2", state_version=1)),
        expected_invariants=_expected(),
    )
    # ...but a decreasing version is rejected.
    with pytest.raises(ValidationError):
        Trajectory.build(
            trajectory_id="t",
            scenario=Scenario.IDEMPOTENT_REPLAY,
            steps=(_step("s1", state_version=1), _step("s2", state_version=0)),
            expected_invariants=_expected(),
        )


def test_manifest_duplicate_trajectory_ids_rejected() -> None:
    six = _six_trajectories()
    replaced = list(six)
    # Same scenario coverage, but trajectory[0] now reuses trajectory[1]'s id.
    replaced[1] = six[0].model_copy(update={"trajectory_id": six[1].trajectory_id})
    with pytest.raises(ValidationError):
        SuiteManifest.build(manifest_id="suite.v1", trajectories=tuple(replaced))


def test_manifest_requires_exactly_one_of_each_scenario() -> None:
    six = _six_trajectories()
    SuiteManifest.build(manifest_id="suite.v1", trajectories=six)
    with pytest.raises(ValidationError):
        SuiteManifest.build(
            manifest_id="suite.v1",
            trajectories=(six[0], six[0], six[1], six[2], six[3], six[4]),
        )
    with pytest.raises(ValidationError):
        SuiteManifest.build(
            manifest_id="suite.v1",
            trajectories=(six[1], six[2], six[3], six[4], six[5], six[5]),
        )
    with pytest.raises(ValidationError):
        SuiteManifest.build(manifest_id="suite.v1", trajectories=six[:5])
    with pytest.raises(ValidationError):
        SuiteManifest.build(manifest_id="suite.v1", trajectories=(six * 2)[:7])


def test_report_failure_codes_must_match_violations() -> None:
    outcomes = violated_invariant_outcomes(
        InvariantKind.PROTOCOL_VALID,
        InvariantKind.PHI_SAFE_PROJECTION,
    )
    codes = failure_codes_for_outcomes(outcomes)
    assert set(codes) == {
        EvaluationFailureCode.PROTOCOL_VIOLATION,
        EvaluationFailureCode.PHI_PROJECTION_UNSAFE,
    }
    _report(
        protocol_valid=False,
        projection_safe=False,
        invariant_outcomes=outcomes,
        failure_codes=codes,
    )
    with pytest.raises(ValidationError):
        _report(
            protocol_valid=False,
            projection_safe=False,
            invariant_outcomes=outcomes,
            failure_codes=(EvaluationFailureCode.PROTOCOL_VIOLATION,),
        )
    with pytest.raises(ValidationError):
        _report(
            protocol_valid=False,
            projection_safe=False,
            invariant_outcomes=outcomes,
            failure_codes=codes + (EvaluationFailureCode.PROTOCOL_VIOLATION,),
        )
    with pytest.raises(ValidationError):
        _report(invariant_outcomes=(outcomes[0], outcomes[0], outcomes[2], outcomes[3], outcomes[4], outcomes[5]))


def test_suite_report_counts_validated() -> None:
    reports = _six_reports()
    suite_report = SuiteEvaluationReport.build(
        manifest_id="suite.v1",
        reports=reports,
        manifest_digest="0" * 64,
    )
    assert suite_report.trajectory_count == 6
    assert suite_report.passed_count == 6
    assert suite_report.failed_count == 0
    suite_report.validate_digest()
    with pytest.raises(ValidationError):
        SuiteEvaluationReport(
            manifest_id="suite.v1",
            trajectory_count=6,
            passed_count=4,
            failed_count=3,
            reports=reports,
            failure_codes=(),
            manifest_digest="0" * 64,
            digest="0" * 64,
        )
    with pytest.raises(ValidationError):
        SuiteEvaluationReport(
            manifest_id="suite.v1",
            trajectory_count=6,
            passed_count=6,
            failed_count=0,
            reports=reports[:5] + (reports[0],),
            failure_codes=(),
            manifest_digest="0" * 64,
            digest="0" * 64,
        )


def test_suite_report_failure_codes_must_be_unique() -> None:
    outcomes = violated_invariant_outcomes(InvariantKind.PROTOCOL_VALID)
    codes = failure_codes_for_outcomes(outcomes)
    failing = _report(
        protocol_valid=False,
        invariant_outcomes=outcomes,
        failure_codes=codes,
    )
    reports = tuple(failing if item.trajectory_id == failing.trajectory_id else item for item in _six_reports())
    SuiteEvaluationReport.build(
        manifest_id="suite.v1",
        reports=reports,
        manifest_digest="0" * 64,
    )
    duplicate_codes = (
        EvaluationFailureCode.PROTOCOL_VIOLATION,
        EvaluationFailureCode.PROTOCOL_VIOLATION,
    )
    with pytest.raises(ValidationError):
        SuiteEvaluationReport(
            manifest_id="suite.v1",
            trajectory_count=6,
            passed_count=5,
            failed_count=1,
            reports=reports,
            failure_codes=duplicate_codes,
            manifest_digest="0" * 64,
            digest="0" * 64,
        )


# ---------------------------------------------------------------------------
# canonical JSON and serialization determinism
# ---------------------------------------------------------------------------


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        canonical_json({"x": float("nan")})
    assert exc_info.value.code is EvaluationFailureCode.NOT_JSON


def test_canonical_json_rejects_non_json() -> None:
    with pytest.raises(TrajectoryEvaluationError):
        canonical_json({"x": {"a", "b"}})


def test_canonical_json_is_stable() -> None:
    first = {"b": 2, "a": 1, "nested": {"z": [3, 1, 2], "y": "str"}}
    second = {"nested": {"y": "str", "z": [3, 1, 2]}, "a": 1, "b": 2}
    assert canonical_json(first) == canonical_json(second)
    assert canonical_json(first) == canonical_json(first)


def test_repeated_serialization_determinism() -> None:
    traj = _six_trajectories()[0]
    assert model_canonical_json(traj) == model_canonical_json(traj)
    rebuilt = Trajectory.build(
        trajectory_id=traj.trajectory_id,
        scenario=traj.scenario,
        steps=traj.steps,
        expected_invariants=traj.expected_invariants,
    )
    assert model_canonical_json(rebuilt) == model_canonical_json(traj)
    manifest = SuiteManifest.build(manifest_id="suite.v1", trajectories=_six_trajectories())
    assert model_canonical_json(manifest) == model_canonical_json(manifest)
    report = _report()
    assert model_canonical_json(report) == model_canonical_json(report)
    suite_report = SuiteEvaluationReport.build(
        manifest_id="suite.v1",
        reports=_six_reports(),
        manifest_digest="0" * 64,
    )
    assert model_canonical_json(suite_report) == model_canonical_json(suite_report)


def test_manifest_digest_is_order_independent() -> None:
    six = _six_trajectories()
    forward = SuiteManifest.build(manifest_id="suite.v1", trajectories=six)
    reversed_ = SuiteManifest.build(manifest_id="suite.v1", trajectories=tuple(reversed(six)))
    assert forward.digest == reversed_.digest
    # The stored order differs, so the manifest bodies differ...
    assert model_canonical_json(forward) != model_canonical_json(reversed_)
    # ...but both digests validate.
    forward.validate_digest()
    reversed_.validate_digest()


# ---------------------------------------------------------------------------
# forbidden raw/volatile fields
# ---------------------------------------------------------------------------


def test_step_model_fields_are_exactly_the_typed_symbolic_set() -> None:
    step = _step("s1", action=StepAction.REPLAY, route=Route.REPLAY, replay_ref="replay.1")
    payload = step.model_dump(mode="json")
    assert set(payload) == {
        "step_id",
        "action",
        "route",
        "state_version",
        "question_count",
        "safety_escalated",
        "protocol_valid",
        "replay_ref",
        "checkpoint_ref",
        "projection_safe",
    }
    assert payload["action"] == "replay"
    assert payload["route"] == "replay"
    assert payload["replay_ref"] == "replay.1"
    assert payload["checkpoint_ref"] is None
    assert all(isinstance(value, str | int | bool) or value is None for value in payload.values())


def test_forbidden_fields_absent_from_model_schemas() -> None:
    for model in ALL_MODELS:
        schema = model.model_json_schema()
        assert schema.get("additionalProperties") is False, model.__name__
        for field_name in schema["properties"]:
            assert not any(forbidden in field_name for forbidden in FORBIDDEN_FIELD_SUBSTRINGS), (
                f"{model.__name__}.{field_name}"
            )


def test_forbidden_fields_absent_from_report_json() -> None:
    report = _report()
    payload = report.model_dump(mode="json")
    assert set(payload) == {
        "schema_version",
        "trajectory_id",
        "scenario",
        "step_count",
        "question_count_max",
        "safety_escalated",
        "protocol_valid",
        "projection_safe",
        "adapter_failure",
        "invariant_outcomes",
        "failure_codes",
        "trajectory_digest",
        "observed_steps_digest",
        "digest",
    }
    suite_report = SuiteEvaluationReport.build(
        manifest_id="suite.v1",
        reports=_six_reports(),
        manifest_digest="0" * 64,
    )
    suite_payload = suite_report.model_dump(mode="json")
    assert set(suite_payload) == {
        "schema_version",
        "manifest_id",
        "trajectory_count",
        "passed_count",
        "failed_count",
        "reports",
        "failure_codes",
        "manifest_digest",
        "digest",
    }
    assert not any(
        any(forbidden in key for forbidden in FORBIDDEN_FIELD_SUBSTRINGS) for key in (*payload, *suite_payload)
    )


# ---------------------------------------------------------------------------
# R3-A2: evaluator engine
# ---------------------------------------------------------------------------


class _RecordingExecutor:
    """An executor that records calls and can raise for selected trajectories."""

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        exception: Exception | None = None,
    ) -> None:
        self._fail_on = frozenset(fail_on or ())
        self._exception = exception
        self.calls: list[str] = []
        self.observed: list[tuple[TrajectoryStep, ...]] = []

    def __call__(self, trajectory: Trajectory) -> tuple[TrajectoryStep, ...]:
        self.calls.append(trajectory.trajectory_id)
        if trajectory.trajectory_id in self._fail_on:
            if self._exception is not None:
                raise self._exception
            raise RuntimeError("adapter-blew-up")
        steps = trajectory.steps
        self.observed.append(steps)
        return steps


class _FixedStepsExecutor:
    """An executor that returns a fixed observed step sequence regardless of input."""

    def __init__(self, steps: tuple[TrajectoryStep, ...]) -> None:
        self._steps = steps

    def __call__(self, trajectory: Trajectory) -> tuple[TrajectoryStep, ...]:
        return self._steps


def _missed_safety_escalation_trajectory() -> Trajectory:
    return _trajectory(
        "traj.red_flag_missed",
        Scenario.RED_FLAG_ESCALATION,
        (
            _step("s1", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=0, question_count=1),
            _step(
                "s2",
                action=StepAction.ESCALATE,
                route=Route.RED_FLAG,
                state_version=1,
                question_count=1,
                safety_escalated=False,
            ),
        ),
    )


def _protocol_violation_trajectory() -> Trajectory:
    return _trajectory(
        "traj.protocol_violation",
        Scenario.MULTI_TURN_INTAKE,
        (
            _step("s1", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=0, question_count=1),
            _step("s2", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=1, question_count=2),
            _step(
                "s3",
                action=StepAction.RESPOND,
                route=Route.TERMINAL,
                state_version=2,
                question_count=2,
                protocol_valid=False,
            ),
        ),
    )


def _route_state_violation_trajectory() -> Trajectory:
    return _trajectory(
        "traj.route_state_violation",
        Scenario.MULTI_TURN_INTAKE,
        (
            _step("s1", action=StepAction.OBSERVE, route=Route.INTAKE, state_version=0, question_count=1),
            _step("s2", action=StepAction.RESPOND, route=Route.TERMINAL, state_version=1, question_count=1),
            _step("s3", action=StepAction.EXTRACT, route=Route.REASONING, state_version=2, question_count=1),
        ),
    )


def _replay_inequality_trajectory() -> Trajectory:
    return _trajectory(
        "traj.replay_inequality",
        Scenario.IDEMPOTENT_REPLAY,
        (
            _step(
                "s1",
                action=StepAction.RESPOND,
                route=Route.REPLAY,
                state_version=1,
                question_count=0,
                replay_ref="replay.1",
            ),
            _step(
                "s2",
                action=StepAction.RESPOND,
                route=Route.REPLAY,
                state_version=1,
                question_count=1,
                replay_ref="replay.1",
            ),
        ),
    )


def _bounded_question_count_violation_trajectory() -> Trajectory:
    return _trajectory(
        "traj.question_count_jump",
        Scenario.MULTI_TURN_INTAKE,
        (
            _step("s1", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=0, question_count=1),
            _step("s2", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=1, question_count=3),
            _step("s3", action=StepAction.RESPOND, route=Route.TERMINAL, state_version=2, question_count=3),
        ),
    )


def _phi_projection_violation_trajectory() -> Trajectory:
    return _trajectory(
        "traj.phi_projection_violation",
        Scenario.CORRECTION_RETRACTION,
        (
            _step("s1", action=StepAction.OBSERVE, route=Route.INTAKE, state_version=0, question_count=1),
            _step(
                "s2",
                action=StepAction.PROJECT,
                route=Route.PROJECTION,
                state_version=1,
                question_count=1,
                projection_safe=False,
            ),
        ),
    )


def _returns_list(trajectory: Trajectory) -> object:
    return list(trajectory.steps)


def _returns_wrong_member(trajectory: Trajectory) -> object:
    return (trajectory.steps[0], "not-a-step")


def _returns_empty(trajectory: Trajectory) -> object:
    return ()


def _returns_oversized(trajectory: Trajectory) -> object:
    return tuple(_step(f"x{i}") for i in range(MAX_TRAJECTORY_STEPS + 1))


def test_recorded_steps_executor_is_a_trajectory_executor() -> None:
    assert isinstance(recorded_steps_executor, TrajectoryExecutor)
    for traj in _six_trajectories():
        assert recorded_steps_executor(traj) is traj.steps


def test_default_executor_succeeds_for_all_six_scenarios() -> None:
    for traj in _six_trajectories():
        report = evaluate_trajectory(traj, recorded_steps_executor)
        assert report.adapter_failure is False
        assert report.failure_codes == ()
        assert report.trajectory_id == traj.trajectory_id
        assert report.scenario is traj.scenario
        assert report.step_count == len(traj.steps)
        assert report.question_count_max == max((step.question_count for step in traj.steps), default=0)
        assert report.safety_escalated == any(step.safety_escalated for step in traj.steps)
        assert report.protocol_valid is True
        assert report.projection_safe is True
        assert report.trajectory_digest == traj.digest
        assert report.observed_steps_digest == observed_steps_digest(traj.steps)
        assert expected_invariants_match(report.invariant_outcomes, SAFE_EXPECTED_INVARIANTS)
        for outcome in report.invariant_outcomes:
            assert outcome.status is InvariantStatus.SATISFIED
        report.validate_digest()


def test_expected_invariants_match_semantics() -> None:
    for traj in _six_trajectories():
        report = evaluate_trajectory(traj, recorded_steps_executor)
        assert expected_invariants_match(report.invariant_outcomes, SAFE_EXPECTED_INVARIANTS)
    violated = evaluate_trajectory(_protocol_violation_trajectory(), recorded_steps_executor)
    assert not expected_invariants_match(violated.invariant_outcomes, SAFE_EXPECTED_INVARIANTS)
    assert not expected_invariants_match(
        satisfied_invariant_outcomes(),
        _expected(missed_safety_escalation=True),
    )


def test_default_executor_suite_passes_all_six() -> None:
    manifest = SuiteManifest.build(manifest_id="suite.v1", trajectories=_six_trajectories())
    suite_report = evaluate_suite(manifest, recorded_steps_executor)
    assert suite_report.trajectory_count == 6
    assert suite_report.passed_count == 6
    assert suite_report.failed_count == 0
    assert suite_report.failure_codes == ()
    assert suite_report.manifest_digest == manifest.digest
    for report in suite_report.reports:
        assert report.failure_codes == ()
        report.validate_digest()
    suite_report.validate_digest()


def test_suite_report_stable_across_runs_and_reversed_manifest_order() -> None:
    six = _six_trajectories()
    forward = SuiteManifest.build(manifest_id="suite.v1", trajectories=six)
    reversed_ = SuiteManifest.build(manifest_id="suite.v1", trajectories=tuple(reversed(six)))
    first = evaluate_suite(forward, recorded_steps_executor)
    second = evaluate_suite(forward, recorded_steps_executor)
    assert first == second
    assert model_canonical_json(first) == model_canonical_json(second)
    flipped = evaluate_suite(reversed_, recorded_steps_executor)
    assert first == flipped
    assert model_canonical_json(first) == model_canonical_json(flipped)
    assert first.digest == flipped.digest
    first.validate_digest()
    flipped.validate_digest()


def test_unsafe_expected_invariants_rejected_before_executor() -> None:
    base = _six_trajectories()[0]
    unsafe_flags: tuple[dict[str, bool], ...] = (
        {"missed_safety_escalation": True},
        {"protocol_valid": False},
        {"route_state_consistency": False},
        {"replay_equality": False},
        {"bounded_question_count": False},
        {"phi_safe_projection": False},
    )
    for flags in unsafe_flags:
        unsafe = Trajectory.build(
            trajectory_id=f"{base.trajectory_id}.unsafe",
            scenario=base.scenario,
            steps=base.steps,
            expected_invariants=_expected(**flags),
        )
        with pytest.raises(TrajectoryEvaluationError) as exc_info:
            evaluate_trajectory(unsafe, recorded_steps_executor)
        assert exc_info.value.code is EvaluationFailureCode.UNSAFE_EXPECTED_INVARIANTS


def test_suite_rejects_unsafe_expectations_before_executor() -> None:
    six = _six_trajectories()
    base = six[0]
    unsafe = Trajectory.build(
        trajectory_id=base.trajectory_id,
        scenario=base.scenario,
        steps=base.steps,
        expected_invariants=_expected(protocol_valid=False),
    )
    manifest = SuiteManifest.build(
        manifest_id="suite.v1",
        trajectories=tuple(unsafe if item is base else item for item in six),
    )
    counted = _RecordingExecutor()
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        evaluate_suite(manifest, counted)
    assert exc_info.value.code is EvaluationFailureCode.UNSAFE_EXPECTED_INVARIANTS
    assert counted.calls == []


def test_tampered_manifest_digest_rejected_before_executor() -> None:
    six = _six_trajectories()
    manifest = SuiteManifest.build(manifest_id="suite.v1", trajectories=six)
    tampered = manifest.model_copy(
        update={
            "trajectories": tuple(
                item.model_copy(update={"digest": "1" * 64}) if item.trajectory_id == six[0].trajectory_id else item
                for item in manifest.trajectories
            )
        }
    )
    counted = _RecordingExecutor()
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        evaluate_suite(tampered, counted)
    assert exc_info.value.code is EvaluationFailureCode.MANIFEST_DIGEST_MISMATCH
    assert counted.calls == []


def test_tampered_trajectory_digest_rejected_before_executor() -> None:
    six = _six_trajectories()
    base = six[0]
    stale = Trajectory.build(
        trajectory_id=base.trajectory_id,
        scenario=base.scenario,
        steps=tuple(
            step.model_copy(update={"question_count": step.question_count + 1}) if step.step_id == "s1" else step
            for step in base.steps
        ),
        expected_invariants=base.expected_invariants,
    ).model_copy(update={"digest": "0" * 64})
    assert trajectory_digest(stale) != stale.digest
    manifest = SuiteManifest.build(
        manifest_id="suite.v1",
        trajectories=tuple(stale if item.trajectory_id == base.trajectory_id else item for item in six),
    )
    counted = _RecordingExecutor()
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        evaluate_suite(manifest, counted)
    assert exc_info.value.code is EvaluationFailureCode.TRAJECTORY_DIGEST_MISMATCH
    assert counted.calls == []

    counted = _RecordingExecutor()
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        evaluate_trajectory(stale, counted)
    assert exc_info.value.code is EvaluationFailureCode.TRAJECTORY_DIGEST_MISMATCH
    assert counted.calls == []


@pytest.mark.parametrize(
    ("trajectory", "invariant", "code"),
    [
        (
            _missed_safety_escalation_trajectory(),
            InvariantKind.MISSED_SAFETY_ESCALATION,
            EvaluationFailureCode.MISSED_SAFETY_ESCALATION,
        ),
        (_protocol_violation_trajectory(), InvariantKind.PROTOCOL_VALID, EvaluationFailureCode.PROTOCOL_VIOLATION),
        (
            _route_state_violation_trajectory(),
            InvariantKind.ROUTE_STATE_CONSISTENCY,
            EvaluationFailureCode.ROUTE_STATE_INCONSISTENCY,
        ),
        (_replay_inequality_trajectory(), InvariantKind.REPLAY_EQUALITY, EvaluationFailureCode.REPLAY_INEQUALITY),
        (
            _bounded_question_count_violation_trajectory(),
            InvariantKind.BOUNDED_QUESTION_COUNT,
            EvaluationFailureCode.QUESTION_COUNT_UNBOUNDED,
        ),
        (
            _phi_projection_violation_trajectory(),
            InvariantKind.PHI_SAFE_PROJECTION,
            EvaluationFailureCode.PHI_PROJECTION_UNSAFE,
        ),
    ],
)
def test_each_invariant_fails_independently(
    trajectory: Trajectory,
    invariant: InvariantKind,
    code: EvaluationFailureCode,
) -> None:
    report = evaluate_trajectory(trajectory, recorded_steps_executor)
    assert report.adapter_failure is False
    assert report.failure_codes == (code,)
    assert len(report.invariant_outcomes) == 6
    for outcome in report.invariant_outcomes:
        if outcome.invariant is invariant:
            assert outcome.status is InvariantStatus.VIOLATED
        else:
            assert outcome.status is InvariantStatus.SATISFIED
    report.validate_digest()


def test_checkpoint_mismatch_attribution() -> None:
    traj = _trajectory(
        "traj.checkpoint_mismatch",
        Scenario.CHECKPOINT_RESUME,
        (
            _step(
                "s1",
                action=StepAction.RESPOND,
                route=Route.INTAKE,
                state_version=0,
                question_count=2,
                checkpoint_ref="ckpt.1",
            ),
            _step(
                "s2",
                action=StepAction.RESUME,
                route=Route.RESUME,
                state_version=1,
                question_count=2,
                checkpoint_ref="ckpt.2",
            ),
        ),
    )
    report = evaluate_trajectory(traj, recorded_steps_executor)
    assert report.failure_codes == (EvaluationFailureCode.ROUTE_STATE_INCONSISTENCY,)
    outcome = next(o for o in report.invariant_outcomes if o.invariant is InvariantKind.ROUTE_STATE_CONSISTENCY)
    assert outcome.status is InvariantStatus.VIOLATED


def test_route_action_mismatch_attribution() -> None:
    traj = _trajectory(
        "traj.route_action_mismatch",
        Scenario.MULTI_TURN_INTAKE,
        (
            _step("s1", action=StepAction.OBSERVE, route=Route.REASONING, state_version=0, question_count=0),
            _step("s2", action=StepAction.RESPOND, route=Route.TERMINAL, state_version=1, question_count=0),
        ),
    )
    report = evaluate_trajectory(traj, recorded_steps_executor)
    assert report.failure_codes == (EvaluationFailureCode.ROUTE_STATE_INCONSISTENCY,)
    outcome = next(o for o in report.invariant_outcomes if o.invariant is InvariantKind.ROUTE_STATE_CONSISTENCY)
    assert outcome.status is InvariantStatus.VIOLATED


@pytest.mark.parametrize(
    "steps",
    [
        (
            _step("s1", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=0, question_count=3),
            _step("s2", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=1, question_count=2),
            _step("s3", action=StepAction.RESPOND, route=Route.TERMINAL, state_version=2, question_count=2),
        ),
        (
            _step("s1", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=0, question_count=1),
            _step("s2", action=StepAction.EXTRACT, route=Route.INTAKE, state_version=1, question_count=3),
            _step("s3", action=StepAction.RESPOND, route=Route.TERMINAL, state_version=2, question_count=3),
        ),
    ],
)
def test_question_count_decrease_and_jump_fail(steps: tuple[TrajectoryStep, ...]) -> None:
    traj = _trajectory("traj.question_count_bounds", Scenario.MULTI_TURN_INTAKE, steps)
    report = evaluate_trajectory(traj, recorded_steps_executor)
    assert report.failure_codes == (EvaluationFailureCode.QUESTION_COUNT_UNBOUNDED,)
    outcome = next(o for o in report.invariant_outcomes if o.invariant is InvariantKind.BOUNDED_QUESTION_COUNT)
    assert outcome.status is InvariantStatus.VIOLATED


def test_adapter_exception_isolated_remaining_trajectories_execute() -> None:
    six = _six_trajectories()
    manifest = SuiteManifest.build(manifest_id="suite.v1", trajectories=six)
    failing_id = six[0].trajectory_id
    executor = _RecordingExecutor(fail_on={failing_id})
    suite_report = evaluate_suite(manifest, executor)
    assert sorted(executor.calls) == sorted(item.trajectory_id for item in six)
    assert len(executor.calls) == 6
    assert suite_report.trajectory_count == 6
    assert suite_report.passed_count == 5
    assert suite_report.failed_count == 1
    failed = next(item for item in suite_report.reports if item.trajectory_id == failing_id)
    assert failed.adapter_failure is True
    assert failed.failure_codes == (
        EvaluationFailureCode.MISSED_SAFETY_ESCALATION,
        EvaluationFailureCode.PROTOCOL_VIOLATION,
        EvaluationFailureCode.ROUTE_STATE_INCONSISTENCY,
        EvaluationFailureCode.REPLAY_INEQUALITY,
        EvaluationFailureCode.QUESTION_COUNT_UNBOUNDED,
        EvaluationFailureCode.PHI_PROJECTION_UNSAFE,
        EvaluationFailureCode.ADAPTER_FAILURE,
    )
    assert EvaluationFailureCode.ADAPTER_FAILURE in suite_report.failure_codes
    for report in suite_report.reports:
        if report.trajectory_id != failing_id:
            assert report.adapter_failure is False
            assert report.failure_codes == ()
            assert report.observed_steps_digest == observed_steps_digest(
                next(item.steps for item in six if item.trajectory_id == report.trajectory_id)
            )
    suite_report.validate_digest()


def test_adapter_exception_secret_absent_from_report_json() -> None:
    traj = _six_trajectories()[0]
    secret = "TOP-SECRET-INTERNAL-9f3d"
    executor = _RecordingExecutor(fail_on={traj.trajectory_id}, exception=RuntimeError(secret))
    report = evaluate_trajectory(traj, executor)
    assert report.adapter_failure is True
    text = model_canonical_json(report)
    assert secret not in text
    assert "RuntimeError" not in text


def test_repeated_adapter_failure_reports_identical() -> None:
    traj = _six_trajectories()[0]
    executor = _RecordingExecutor(fail_on={traj.trajectory_id})
    first = evaluate_trajectory(traj, executor)
    second = evaluate_trajectory(traj, executor)
    assert first == second
    assert model_canonical_json(first) == model_canonical_json(second)
    assert first.digest == second.digest
    assert first.adapter_failure is True


@pytest.mark.parametrize(
    ("bad_executor", "raw_fragment"),
    [
        (_returns_list, "step_id"),
        (_returns_wrong_member, "not-a-step"),
        (_returns_empty, "step_id"),
        (_returns_oversized, "step_id"),
    ],
)
def test_invalid_executor_output_isolated_without_raw_data(
    bad_executor: Callable[[Trajectory], object],
    raw_fragment: str,
) -> None:
    traj = _six_trajectories()[0]
    report = evaluate_trajectory(traj, bad_executor)  # type: ignore[arg-type]
    assert report.adapter_failure is True
    assert report.failure_codes[-1] is EvaluationFailureCode.ADAPTER_FAILURE
    assert len(report.failure_codes) == 7
    assert raw_fragment not in model_canonical_json(report)
    report.validate_digest()


def test_evaluation_does_not_mutate_manifest_or_observed_steps() -> None:
    six = _six_trajectories()
    manifest = SuiteManifest.build(manifest_id="suite.v1", trajectories=six)
    manifest_json_before = model_canonical_json(manifest)
    evaluate_suite(manifest, recorded_steps_executor)
    assert model_canonical_json(manifest) == manifest_json_before

    executor = _RecordingExecutor()
    traj = six[0]
    evaluate_trajectory(traj, executor)
    assert len(executor.observed) == 1
    returned = executor.observed[0]
    assert returned == traj.steps
    assert [step.model_dump(mode="json") for step in returned] == [step.model_dump(mode="json") for step in traj.steps]


def test_observed_steps_digest_drifts_deterministically() -> None:
    traj = _six_trajectories()[0]
    baseline = evaluate_trajectory(traj, recorded_steps_executor)
    assert baseline.observed_steps_digest == observed_steps_digest(traj.steps)
    assert observed_steps_digest(traj.steps) == observed_steps_digest(traj.steps)

    drifted = tuple(
        step.model_copy(update={"protocol_valid": False}) if step.step_id == "s1" else step for step in traj.steps
    )
    assert observed_steps_digest(drifted) != observed_steps_digest(traj.steps)

    drifted_report = evaluate_trajectory(traj, _FixedStepsExecutor(drifted))
    assert drifted_report.observed_steps_digest == observed_steps_digest(drifted)
    assert drifted_report.observed_steps_digest != baseline.observed_steps_digest
