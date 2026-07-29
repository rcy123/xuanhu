"""Privacy and requirement gates for the L9 rollout status CLI."""

from __future__ import annotations

import pytest

from scripts import check_runtime_rollout


def _ready_result() -> dict[str, object]:
    return {
        "status": "ready",
        "phase": "full",
        "configured_runtime": "langgraph",
        "audited_runtime": "langgraph",
        "audit_present": True,
        "phase_audit_status": "ok",
        "phase_audit_present": True,
        "phase_deployment_id": "phase-full",
        "runtime_switch_deployment_id": "runtime-deploy-a",
        "full_phase_age_seconds": 7_200,
        "public_langgraph_enabled": True,
        "product_ready_authorized": True,
        "policy_error_code": None,
        "sessions": {
            "legacy": {"open": 0, "terminal": 12},
            "langgraph": {"open": 4, "terminal": 8},
        },
        "legacy_removal_ready": True,
    }


def test_cli_accepts_full_cutover_only_after_legacy_drain(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def collect() -> dict[str, object]:
        return _ready_result()

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main(
        [
            "--require-phase",
            "full",
            "--require-legacy-drained",
            "--require-stable-minutes",
            "60",
            "--deployment-id",
            "phase-full",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"legacy_removal_ready": true' in captured.out
    assert '"stable_window_ready": true' in captured.out
    assert captured.err == ""


def test_cli_blocks_removal_while_any_legacy_session_is_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def collect() -> dict[str, object]:
        result = _ready_result()
        result["legacy_removal_ready"] = False
        return result

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main(["--require-legacy-drained"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert '"requirement_error_code": "LEGACY_NOT_DRAINED"' in captured.out


def test_cli_failure_never_echoes_lower_layer_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://user:password@private-host/private-db"

    async def collect() -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "RUNTIME_ROLLOUT_CHECK_FAILED" in captured.err
    assert secret not in captured.err


def test_cli_blocks_when_the_audited_full_window_is_too_young(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def collect() -> dict[str, object]:
        result = _ready_result()
        result["full_phase_age_seconds"] = 3_599
        return result

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main(["--require-stable-minutes", "60", "--deployment-id", "phase-full"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert '"stable_window_ready": false' in captured.out
    assert '"requirement_error_code": "STABLE_WINDOW_NOT_MET"' in captured.out


def test_cli_rejects_age_alone_outside_the_full_phase(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def collect() -> dict[str, object]:
        result = _ready_result()
        result["phase"] = "canary"
        return result

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main(["--require-stable-minutes", "60", "--deployment-id", "phase-full"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert '"stable_window_ready": false' in captured.out


def test_cli_does_not_reuse_an_old_runtime_switch_or_canary_window(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def collect() -> dict[str, object]:
        result = _ready_result()
        result["last_switch_age_seconds"] = 2_592_000
        result["full_phase_age_seconds"] = 59
        return result

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main(["--require-stable-minutes", "60", "--deployment-id", "phase-full"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert '"stable_window_ready": false' in captured.out


def test_cli_requires_phase_deployment_identity_for_stable_window(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def collect() -> dict[str, object]:
        return _ready_result()

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main(["--require-stable-minutes", "60"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert '"requirement_error_code": "STABLE_WINDOW_DEPLOYMENT_REQUIRED"' in captured.out


def test_cli_blocks_stable_window_for_another_phase_deployment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def collect() -> dict[str, object]:
        return _ready_result()

    monkeypatch.setattr(check_runtime_rollout, "_collect", collect)
    exit_code = check_runtime_rollout.main(
        [
            "--require-stable-minutes",
            "60",
            "--deployment-id",
            "phase-other",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert '"requirement_error_code": "ROLLOUT_DEPLOYMENT_MISMATCH"' in captured.out
