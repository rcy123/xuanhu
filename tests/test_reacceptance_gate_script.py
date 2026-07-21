from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "verify_l0_l4_reacceptance.ps1"
RULES_PATH = Path(__file__).parents[1] / "deploy" / "prometheus" / "rules" / "xuanhu-outbox-alerts.yml"
WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "quality.yml"
GITIGNORE_PATH = Path(__file__).parents[1] / ".gitignore"


def _script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _assert_compact_fragment(script: str, fragment: str) -> None:
    assert _compact(fragment) in _compact(script)


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
    assert executable is not None, "PowerShell is required to validate the reacceptance gate script"
    return executable


def test_gate_script_parses_with_powershell_ast_and_fails_fast_when_tools_are_missing() -> None:
    executable = _powershell()
    escaped_path = str(SCRIPT_PATH.resolve()).replace("'", "''")
    parser_command = (
        f"$path = '{escaped_path}'; "
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) "
        "| Out-Null; "
        "if ($errors.Count -ne 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    parsed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", parser_command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr

    isolated_environment = os.environ.copy()
    isolated_environment["PATH"] = ""
    smoke = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT_PATH.resolve()),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=isolated_environment,
    )
    assert smoke.returncode != 0
    assert "Required tool is unavailable: git" in smoke.stdout + smoke.stderr


def test_gate_script_has_exact_unit_static_integration_and_frontend_commands() -> None:
    script = _script()
    assert "single-command, locked-dependency" in script
    assert "..." not in script
    assert "…" not in script

    for fragment in (
        '"run", "--isolated", "--python", "3.12", "--locked", "pytest"',
        '"run", "--isolated", "--python", "3.12", "--locked", "ruff", "check", "."',
        '"run", "--isolated", "--python", "3.12", "--locked", "mypy", "app", "scripts"',
        '"lock", "--check"',
        (
            '"run", "--isolated", "--python", "3.12", "--locked", "pytest", '
            '"-o", "addopts=", "-m", "integration and not performance", '
            '"-n", "4", "--dist=loadscope", '
            '"--strict-markers", "tests"'
        ),
        (
            '"run", "--isolated", "--python", "3.12", "--locked", "pytest", '
            '"-o", "addopts=", "-m", "integration and performance", "--strict-markers", '
            '"tests/golden/test_legacy_performance_baseline.py", '
            '"tests/golden/test_langgraph_performance_baseline.py"'
        ),
        (
            '"run", "--isolated", "--python", "3.12", "--locked", "pytest", '
            '"-o", "addopts=", "-m", "integration", "-n", "2", "--dist=each", '
            '"--strict-markers", "tests/test_infrastructure_isolation.py"'
        ),
        'Invoke-NativeGate -Command "npm" -Arguments @("ci") -WorkingDirectory $FrontendRoot',
        'Invoke-NativeGate -Command "npm" -Arguments @("run", "typecheck")',
        'Invoke-NativeGate -Command "npm" -Arguments @("run", "lint")',
        'Invoke-NativeGate -Command "npm" -Arguments @("test")',
        'Invoke-NativeGate -Command "npm" -Arguments @("run", "build")',
        '"audit", "--omit=dev", "--audit-level=high"',
        "environment-evidence.json",
        "postgres_server_version_num",
        "redis_version",
        "prom/prometheus:v3.5.0",
        "rhysd/actionlint:1.7.7",
        "ghcr.io/gitleaks/gitleaks:v8.28.0",
    ):
        _assert_compact_fragment(script, fragment)

    assert '"--python", "3.11"' not in script
    assert "python_3_11" not in script
    assert "expected major version 24" in script
    assert "expected major version 11" in script
    assert "expected the validated 0.9 through 0.11 line" in script


def test_integration_preflight_requires_all_three_explicit_safety_inputs() -> None:
    script = _script()

    for name in (
        "TEST_DATABASE_URL",
        "TEST_REDIS_URL",
        "XUANHU_ALLOW_DESTRUCTIVE_TESTS",
    ):
        assert f'[Environment]::GetEnvironmentVariable("{name}")' in script
        assert name in script

    assert 'if ($destructiveSentinel -ne "1")' in script
    assert "require_destructive_test_database()" in script
    assert "require_destructive_test_redis()" in script

    preflight_index = script.index("Assert-IntegrationEnvironment -RepoRoot $RepoRoot")
    integration_index = script.index('Write-Host "==> Isolated PostgreSQL and Redis integration gate"')
    assert preflight_index < integration_index


def test_embedded_python_literals_survive_windows_powershell_native_argument_passing() -> None:
    script = _script()
    for fragment in (
        "print('integration targets validated')",
        "os.environ['TEST_DATABASE_URL']",
        "os.environ['TEST_REDIS_URL']",
        "redis.info(section='server')['redis_version']",
        "{'postgres_server_version_num': postgres_version, 'redis_version': redis_version}",
    ):
        assert fragment in script

    executable = _powershell()
    escaped_python = sys.executable.replace("'", "''")
    smoke_command = (
        "$code = \"import json`nprint(json.dumps({'status': 'ok'}))\"; "
        f"$output = @(& '{escaped_python}' -c $code); "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
        "$result = ($output -join [Environment]::NewLine) | ConvertFrom-Json; "
        "if ($result.status -ne 'ok') { exit 1 }"
    )
    smoke = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", smoke_command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_native_helpers_fail_closed_on_real_nonzero_exit_codes() -> None:
    executable = _powershell()
    escaped_script = str(SCRIPT_PATH.resolve()).replace("'", "''")
    escaped_python = sys.executable.replace("'", "''")
    escaped_working_directory = str(SCRIPT_PATH.parents[1].resolve()).replace("'", "''")
    smoke_command = (
        f"$path = '{escaped_script}'; "
        "$tokens = $null; $errors = $null; "
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        "$path, [ref]$tokens, [ref]$errors); "
        "$functions = $ast.FindAll({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and ($node.Name -eq 'Invoke-NativeGate' "
        "-or $node.Name -eq 'Invoke-NativeCapture') }, $true); "
        "foreach ($function in $functions) { "
        ". ([ScriptBlock]::Create($function.Extent.Text)) }; "
        "$gateCaught = $false; "
        "try { "
        f"Invoke-NativeGate -Command '{escaped_python}' "
        "-Arguments @('-c', 'import sys; sys.exit(23)') "
        f"-WorkingDirectory '{escaped_working_directory}' "
        "} catch { "
        "if ($_.Exception.Message -notmatch 'exit code 23') { exit 92 }; "
        "$gateCaught = $true }; "
        "if (-not $gateCaught) { exit 91 }; "
        "$captureCaught = $false; "
        "try { "
        f"Invoke-NativeCapture -Command '{escaped_python}' "
        "-Arguments @('-c', 'import sys; sys.exit(23)') "
        f"-WorkingDirectory '{escaped_working_directory}' | Out-Null "
        "} catch { "
        "if ($_.Exception.Message -notmatch 'exit code 23') { exit 94 }; "
        "$captureCaught = $true }; "
        "if (-not $captureCaught) { exit 93 }; "
        f"Invoke-NativeGate -Command '{escaped_python}' "
        "-Arguments @('-c', 'import sys; sys.exit(0)') "
        f"-WorkingDirectory '{escaped_working_directory}'; "
        f"$captured = Invoke-NativeCapture -Command '{escaped_python}' "
        "-Arguments @('-c', \"print('captured-ok')\") "
        f"-WorkingDirectory '{escaped_working_directory}'; "
        "if ($captured -ne 'captured-ok') { exit 95 }"
    )
    smoke = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", smoke_command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_gate_script_has_exact_dependency_sbom_workflow_and_secret_scan_commands() -> None:
    script = _script()
    assert RULES_PATH.is_file()
    assert ".codex_tmp/" in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()

    for fragment in (
        (
            '"export", "--locked", "--no-dev", "--no-emit-project", "--format", '
            '"requirements.txt", "--output-file", $RequirementsPartialPath'
        ),
        '"pip-audit==2.10.1", "pip-audit", "--strict", "--requirement", $RequirementsPath',
        '"cyclonedx-bom==7.3.0", "cyclonedx-py", "requirements"',
        '"--spec-version", "1.6", "--output-reproducible", "--output-format", "JSON"',
        '"sbom", "--sbom-format", "cyclonedx"',
        (
            '"run", "--rm", "--entrypoint=/bin/promtool", "--volume", $repoReadOnlyMount, '
            '"--workdir=/repo", "prom/prometheus:v3.5.0", "check", "rules", '
            '"deploy/prometheus/rules/xuanhu-outbox-alerts.yml"'
        ),
        (
            '"run", "--rm", "--entrypoint=/bin/promtool", "--volume", $repoReadOnlyMount, '
            '"--workdir=/repo", "prom/prometheus:v3.5.0", "test", "rules", '
            '"deploy/prometheus/tests/xuanhu-outbox-alerts.test.yml"'
        ),
        '"rhysd/actionlint:1.7.7", "-color"',
        '"ghcr.io/gitleaks/gitleaks:v8.28.0", "git", "/repo"',
        '"ghcr.io/gitleaks/gitleaks:v8.28.0", "dir", "/repo"',
        '"--gitleaks-ignore-path=/repo/.gitleaksignore", "--no-banner", "--redact"',
    ):
        _assert_compact_fragment(script, fragment)

    assert 'if ($pythonSbom.bomFormat -ne "CycloneDX"' in script
    assert 'if ($nodeSbom.bomFormat -ne "CycloneDX")' in script
    assert script.count("$global:LASTEXITCODE") == 4
    assert "function Assert-NonEmptyFile" in script
    assert "function Publish-GateArtifact" in script
    assert "[IO.File]::Move($sourceFull, $destinationFull)" in script
    assert "destination already exists" in script
    assert "requirements-production.txt.partial" in script
    assert "sbom-python.cdx.json.partial" in script
    assert "sbom-node.cdx.json.partial" in script
    assert "reacceptance-result.json" in script
    assert "reacceptance-result.json.partial" in script
    assert 'status = "passed"' in script
    assert script.index('status = "passed"') > script.index('"dir", "/repo"')
    assert "Final reacceptance result manifest did not satisfy the exact-HEAD success contract" in script

    workflow = _compact(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert 'python-version: "3.12"' in workflow
    assert "3.11" not in workflow
    assert 'docker run --rm --entrypoint=/bin/promtool -v "$PWD:/repo:ro"' in workflow
    assert "prom/prometheus:v3.5.0 check rules" in workflow
    assert "deploy/prometheus/rules/xuanhu-outbox-alerts.yml" in workflow
    assert "prom/prometheus:v3.5.0 test rules" in workflow
    assert "deploy/prometheus/tests/xuanhu-outbox-alerts.test.yml" in workflow


def test_clean_worktree_scan_and_cleanup_are_path_guarded() -> None:
    script = _script()

    assert '"status", "--porcelain=v1", "--untracked-files=all"' in script
    assert "requires a clean worktree" in script
    assert script.count("Assert-CleanRepository -RepoRoot $RepoRoot") == 3
    assert script.count("Assert-ExactHead -RepoRoot $RepoRoot -ExpectedHead $ExpectedGitHead") == 2
    assert "function Assert-SafeTemporaryWorktreePath" in script
    assert "[IO.Path]::GetTempPath()" in script
    assert script.count("[IO.Path]::GetFullPath") >= 4
    assert ".StartsWith($systemTempPrefix, $comparison)" in script
    assert ".StartsWith($approvedPrefix, $comparison)" in script
    assert (
        '"worktree", "add", "--detach", $CleanWorktreePath, $ExpectedGitHead'
        in _compact(script)
    )
    assert '$cleanMount = "${CleanWorktreePath}:/repo:ro"' in script

    cleanup_fragment = '"worktree", "remove", "--force", $CleanWorktreePath'
    cleanup_index = _compact(script).index(cleanup_fragment)
    cleanup_prefix = _compact(script)[:cleanup_index]
    assert cleanup_prefix.rfind("Assert-SafeTemporaryWorktreePath") != -1
    assert "-RequireExistingWorktree" in cleanup_prefix
    assert "Remove-Item" not in script


def test_gate_script_avoids_shell_reentry_and_skip_switches() -> None:
    script = _script()
    lowered = script.casefold()

    assert "#Requires -Version 5.1" in script
    assert "$IsWindows" not in script

    for forbidden in (
        "invoke-expression",
        "cmd /c",
        "powershell -command",
        "pwsh -command",
        "skipintegration",
        "skipsecurity",
    ):
        assert forbidden not in lowered
