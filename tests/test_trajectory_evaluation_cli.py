"""R3-B trajectory-evaluation CLI, loader, fixture, and gate tests.

These tests pin the *deployment* layer of the offline evaluation suite: the
versioned JSON fixture in ``evals/agent_trajectories/``, the fail-closed
bytes loader, the fixed bundled-manifest loader, the deterministic CLI output
contract, and the PHI-safe fixed failure object.  They exercise both the
in-process module functions and the real subprocess CLI, and they scan the
fixture itself for forbidden volatile/clinical tokens.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.evaluate_agent_trajectories as cli
from app.agent_runtime.trajectory_evaluation import (
    SAFE_EXPECTED_INVARIANTS,
    EvaluationFailureCode,
    InvariantStatus,
    Scenario,
    SuiteManifest,
    Trajectory,
    TrajectoryEvaluationError,
    evaluate_suite,
    model_canonical_json,
    recorded_steps_executor,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "evals" / "agent_trajectories" / "manifest.v1.json"

REQUIRED_SCENARIO_VALUES = {scenario.value for scenario in Scenario}

_FORBIDDEN_FIELD_SUBSTRINGS = (
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

_FORBIDDEN_FIXTURE_SUBSTRINGS = _FORBIDDEN_FIELD_SUBSTRINGS + (
    "secret",
    "token",
    "password",
    "api_key",
    "bearer",
    "credential",
    "patient",
    "physician",
    "doctor",
    "clinic",
    "diagnosis",
    "prescription",
    "disease",
    "symptom",
    "herb",
    "name",
    "http",
    "https",
    "www",
)

_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _run_cli() -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.evaluate_agent_trajectories"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=120,
    )


def _valid_manifest() -> SuiteManifest:
    # Read the fixture directly so helpers stay independent of monkeypatching
    # ``cli.load_bundled_manifest``.
    return cli.load_manifest_bytes(MANIFEST_PATH.read_bytes())


def _tampered_manifest_digest_manifest() -> SuiteManifest:
    valid = _valid_manifest()
    first = valid.trajectories[0]
    flipped = "1" if first.digest[0] != "1" else "2"
    tampered = first.model_copy(update={"digest": flipped + first.digest[1:]})
    return valid.model_copy(update={"trajectories": tuple([tampered] + list(valid.trajectories[1:]))})


def _tampered_content_manifest() -> SuiteManifest:
    valid = _valid_manifest()
    first = valid.trajectories[0]
    step0 = first.steps[0]
    tampered_step = step0.model_copy(update={"question_count": step0.question_count + 1})
    tampered = first.model_copy(update={"steps": tuple([tampered_step] + list(first.steps[1:]))})
    return valid.model_copy(update={"trajectories": tuple([tampered] + list(valid.trajectories[1:]))})


def _unsafe_expectations_manifest() -> SuiteManifest:
    valid = _valid_manifest()
    first = valid.trajectories[0]
    rebuilt = Trajectory.build(
        trajectory_id=first.trajectory_id,
        scenario=first.scenario,
        steps=first.steps,
        expected_invariants=SAFE_EXPECTED_INVARIANTS.model_copy(update={"protocol_valid": False}),
    )
    return SuiteManifest.build(
        manifest_id=valid.manifest_id,
        trajectories=tuple([rebuilt] + list(valid.trajectories[1:])),
    )


def _tampered_manifest_digest_bytes() -> bytes:
    """Bytes whose top-level ``digest`` is a valid-format but wrong value."""
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    parsed["digest"] = "deadbeef" * 8
    return json.dumps(parsed).encode("utf-8")


def _tampered_trajectory_digest_bytes() -> bytes:
    """Bytes whose first trajectory content no longer matches its stored digest."""
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    parsed["trajectories"][0]["steps"][0]["step_id"] = "TOP-SECRET-9f3d-INJECTED"
    return json.dumps(parsed).encode("utf-8")


def _unsafe_expectations_bytes() -> bytes:
    """Bytes for an internally digest-consistent manifest that blesses an unsafe outcome."""
    valid = cli.load_manifest_bytes(MANIFEST_PATH.read_bytes())
    first = valid.trajectories[0]
    rebuilt = Trajectory.build(
        trajectory_id=first.trajectory_id,
        scenario=first.scenario,
        steps=first.steps,
        expected_invariants=SAFE_EXPECTED_INVARIANTS.model_copy(update={"protocol_valid": False}),
    )
    unsafe = SuiteManifest.build(
        manifest_id="TOP-SECRET-9f3d-INJECTED",
        trajectories=tuple([rebuilt] + list(valid.trajectories[1:])),
    )
    return model_canonical_json(unsafe).encode("utf-8")


#: Tampered or unsafe manifest bytes, the fixed loader code they must map to,
#: and a fragment carried only by those bytes that must never leak into an
#: exception string or a CLI failure object.
_INTEGRITY_FAILURES: tuple[tuple[bytes, str, str], ...] = (
    (_tampered_manifest_digest_bytes(), cli.ERROR_MANIFEST_DIGEST_MISMATCH, "deadbeef"),
    (_tampered_trajectory_digest_bytes(), cli.ERROR_TRAJECTORY_DIGEST_MISMATCH, "TOP-SECRET-9f3d-INJECTED"),
    (_unsafe_expectations_bytes(), cli.ERROR_UNSAFE_EXPECTED_INVARIANTS, "TOP-SECRET-9f3d-INJECTED"),
)


# ---------------------------------------------------------------------------
# bundled fixture + all six scenarios
# ---------------------------------------------------------------------------


def test_bundled_manifest_loads_and_all_six_scenarios_pass() -> None:
    assert MANIFEST_PATH.is_file()
    manifest = cli.load_bundled_manifest()
    assert manifest.schema_version == "agent-trajectory-eval.v1"
    assert manifest.manifest_id == "suite.agent_trajectories.v1"
    assert len(manifest.trajectories) == 6
    assert {item.scenario for item in manifest.trajectories} == set(Scenario)
    assert {item.scenario.value for item in manifest.trajectories} == REQUIRED_SCENARIO_VALUES
    manifest.validate_digest()
    for item in manifest.trajectories:
        assert item.expected_invariants == SAFE_EXPECTED_INVARIANTS
        item.validate_digest()
    suite_report = evaluate_suite(manifest, recorded_steps_executor)
    assert suite_report.trajectory_count == 6
    assert suite_report.passed_count == 6
    assert suite_report.failed_count == 0
    assert suite_report.failure_codes == ()
    for report in suite_report.reports:
        assert report.failure_codes == ()
        assert len(report.invariant_outcomes) == 6
        assert len({outcome.invariant for outcome in report.invariant_outcomes}) == 6
        assert all(outcome.status is InvariantStatus.SATISFIED for outcome in report.invariant_outcomes)
        report.validate_digest()
    suite_report.validate_digest()


# ---------------------------------------------------------------------------
# repeated CLI runs are byte-identical and exit 0
# ---------------------------------------------------------------------------


def test_cli_two_runs_are_byte_identical_and_exit_zero() -> None:
    first = _run_cli()
    second = _run_cli()
    assert first.returncode == 0, first.stderr.decode("utf-8", errors="replace")
    assert second.returncode == 0, second.stderr.decode("utf-8", errors="replace")
    assert first.stderr == b""
    assert second.stderr == b""
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout.decode("utf-8"))
    assert payload["schema_version"] == "agent-trajectory-eval.v1"
    assert payload["trajectory_count"] == 6
    assert payload["passed_count"] == 6
    assert payload["failed_count"] == 0
    assert payload["failure_codes"] == []
    assert len(payload["reports"]) == 6
    assert {report["scenario"] for report in payload["reports"]} == REQUIRED_SCENARIO_VALUES
    for report in payload["reports"]:
        assert report["failure_codes"] == []
        assert len(report["invariant_outcomes"]) == 6
        assert len({outcome["invariant"] for outcome in report["invariant_outcomes"]}) == 6


# ---------------------------------------------------------------------------
# direct script execution from an arbitrary working directory
# ---------------------------------------------------------------------------


def test_direct_script_execution_from_arbitrary_cwd(tmp_path: Path) -> None:
    script = REPO_ROOT / "scripts" / "evaluate_agent_trajectories.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stderr == b""
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["schema_version"] == "agent-trajectory-eval.v1"
    assert payload["trajectory_count"] == 6
    assert payload["passed_count"] == 6
    assert payload["failed_count"] == 0


# ---------------------------------------------------------------------------
# loader: layered fail-closed validation
# ---------------------------------------------------------------------------


def test_load_manifest_bytes_rejects_non_bytes() -> None:
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes("not bytes")  # type: ignore[arg-type]
    assert exc_info.value.code == cli.ERROR_MANIFEST_NOT_BYTES


def test_load_manifest_bytes_rejects_invalid_utf8() -> None:
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes(b'\xff\xfe{"a":1}')
    assert exc_info.value.code == cli.ERROR_MANIFEST_INVALID_UTF8


def test_load_manifest_bytes_rejects_malformed_json() -> None:
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes(b'{"unclosed": ')
    assert exc_info.value.code == cli.ERROR_MANIFEST_NOT_JSON


def test_load_manifest_bytes_rejects_oversize() -> None:
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes(b" " * (cli.MAX_MANIFEST_BYTES + 1))
    assert exc_info.value.code == cli.ERROR_MANIFEST_OVERSIZE
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes(MANIFEST_PATH.read_bytes(), max_bytes=10)
    assert exc_info.value.code == cli.ERROR_MANIFEST_OVERSIZE


@pytest.mark.parametrize(
    "raw",
    [
        b'["not", "an", "object"]',
        b'"just-a-string"',
        b"123",
        b"null",
        b"true",
    ],
)
def test_load_manifest_bytes_rejects_non_object(raw: bytes) -> None:
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes(raw)
    assert exc_info.value.code == cli.ERROR_MANIFEST_NOT_OBJECT


def test_load_manifest_bytes_rejects_extra_fields_without_leaking() -> None:
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    secret = "TOP-SECRET-9f3d-INJECTED"
    parsed["injected_secret_payload"] = secret
    tampered = json.dumps(parsed).encode("utf-8")
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes(tampered)
    assert exc_info.value.code == cli.ERROR_MANIFEST_SCHEMA_INVALID
    assert secret not in str(exc_info.value)


def test_load_manifest_bytes_round_trips_the_bundled_fixture() -> None:
    manifest = cli.load_manifest_bytes(MANIFEST_PATH.read_bytes())
    assert manifest == cli.load_bundled_manifest()
    assert manifest.digest == json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["digest"]


# ---------------------------------------------------------------------------
# digest tamper and unsafe expectations are rejected with fixed codes
# ---------------------------------------------------------------------------


def test_manifest_digest_tamper_rejected_with_fixed_code() -> None:
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        evaluate_suite(_tampered_manifest_digest_manifest(), recorded_steps_executor)
    assert exc_info.value.code is EvaluationFailureCode.MANIFEST_DIGEST_MISMATCH


def test_trajectory_content_tamper_rejected_with_fixed_code() -> None:
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        evaluate_suite(_tampered_content_manifest(), recorded_steps_executor)
    assert exc_info.value.code is EvaluationFailureCode.TRAJECTORY_DIGEST_MISMATCH


def test_unsafe_expectations_rejected_with_fixed_code() -> None:
    with pytest.raises(TrajectoryEvaluationError) as exc_info:
        evaluate_suite(_unsafe_expectations_manifest(), recorded_steps_executor)
    assert exc_info.value.code is EvaluationFailureCode.UNSAFE_EXPECTED_INVARIANTS


# ---------------------------------------------------------------------------
# the loader itself verifies digests and safe expectations before returning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "error_code", "fragment"),
    _INTEGRITY_FAILURES,
    ids=["manifest-digest", "trajectory-digest", "unsafe-expectations"],
)
def test_load_manifest_bytes_rejects_integrity_failure_without_leaking(
    raw: bytes,
    error_code: str,
    fragment: str,
) -> None:
    with pytest.raises(cli.LoaderError) as exc_info:
        cli.load_manifest_bytes(raw)
    assert exc_info.value.code == error_code
    assert fragment not in str(exc_info.value)


@pytest.mark.parametrize(
    ("raw", "error_code", "fragment"),
    _INTEGRITY_FAILURES,
    ids=["manifest-digest", "trajectory-digest", "unsafe-expectations"],
)
def test_cli_rejects_integrity_failure_before_any_evaluation(
    raw: bytes,
    error_code: str,
    fragment: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_bundled_manifest", lambda: cli.load_manifest_bytes(raw))

    def _must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("evaluate_suite must not run when the loader rejects")

    monkeypatch.setattr(cli, "evaluate_suite", _must_not_run)
    assert cli.main([]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip()) == {"status": "failed", "error_code": error_code}
    assert fragment not in captured.err


# ---------------------------------------------------------------------------
# fixed-path / no-traversal design
# ---------------------------------------------------------------------------


def test_bundled_manifest_path_is_fixed_within_repo() -> None:
    path = cli.bundled_manifest_path()
    assert path == MANIFEST_PATH
    assert path.is_file()
    assert path.resolve().is_relative_to(REPO_ROOT.resolve())
    # The loader never accepts a path argument, so traversal is impossible.
    assert "read_bytes" in dir(path)


@pytest.mark.parametrize(
    "args",
    [
        ["anything"],
        ["--manifest", "evals/agent_trajectories/manifest.v1.json"],
        ["evals/agent_trajectories/manifest.v1.json"],
        ["../evals/agent_trajectories/manifest.v1.json"],
        ["/etc/passwd"],
    ],
)
def test_main_rejects_arguments_with_fixed_failure(
    args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert json.loads(captured.err.strip()) == {"status": "failed", "error_code": "UNEXPECTED_ARGUMENT"}


# ---------------------------------------------------------------------------
# CLI failure output never leaks input or exception text
# ---------------------------------------------------------------------------


def test_cli_digest_tamper_emits_fixed_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_bundled_manifest", _tampered_manifest_digest_manifest)
    assert cli.main([]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip()) == {"status": "failed", "error_code": "MANIFEST_DIGEST_MISMATCH"}


def test_cli_unsafe_expectations_emits_fixed_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_bundled_manifest", _unsafe_expectations_manifest)
    assert cli.main([]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip()) == {"status": "failed", "error_code": "UNSAFE_EXPECTED_INVARIANTS"}


def test_cli_failure_output_does_not_leak_injected_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "TOP-SECRET-9f3d-INJECTED"

    def _boom() -> SuiteManifest:
        raise RuntimeError(f"internal failure carrying {secret}")

    monkeypatch.setattr(cli, "load_bundled_manifest", _boom)
    assert cli.main([]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    assert "RuntimeError" not in captured.err
    assert "internal failure" not in captured.err
    assert json.loads(captured.err.strip()) == {"status": "failed", "error_code": "EVALUATION_FAILED"}


# ---------------------------------------------------------------------------
# fixture forbidden-token scan
# ---------------------------------------------------------------------------


def test_fixture_forbidden_token_scan() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    lowered = text.lower()
    for token in _FORBIDDEN_FIXTURE_SUBSTRINGS:
        assert token not in lowered, f"forbidden token present in fixture: {token}"
    assert _UUID_PATTERN.search(text) is None
    assert _TIMESTAMP_PATTERN.search(text) is None
    # The fixture must be a single versioned manifest that validates end to end.
    manifest = cli.load_manifest_bytes(text.encode("utf-8"))
    assert manifest.schema_version == "agent-trajectory-eval.v1"
    assert len(manifest.trajectories) == 6
    manifest.validate_digest()
