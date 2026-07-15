#Requires -Version 5.1

<#
.SYNOPSIS
Runs the single-command, locked-dependency L0-L4 engineering reacceptance gate.

.DESCRIPTION
The full gate is intentionally fail-closed. It requires an explicit guarded
PostgreSQL test URL, Redis logical database, and destructive-test sentinel.
It also requires a clean Git worktree so security scans and SBOMs describe the
exact HEAD revision rather than an ambiguous mixture of committed and local
content.
#>

[CmdletBinding()]
param(
    [string]$ArtifactDirectory = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-NativeGate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory
    )

    Write-Host "==> $Command $($Arguments -join ' ')"
    Push-Location -LiteralPath $WorkingDirectory
    try {
        # Windows PowerShell 5.1 writes native exit codes to the global scope.
        # A local assignment here would shadow that value and silently turn
        # every native failure into exit code 0.
        $global:LASTEXITCODE = 0
        & $Command @Arguments
        $exitCode = $global:LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "Gate command failed with exit code ${exitCode}: $Command"
    }
}

function Invoke-NativeCapture {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory
    )

    Push-Location -LiteralPath $WorkingDirectory
    try {
        $global:LASTEXITCODE = 0
        $output = @(& $Command @Arguments)
        $exitCode = $global:LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "Gate command failed with exit code ${exitCode}: $Command"
    }
    return ($output -join [Environment]::NewLine).Trim()
}

function Assert-NonEmptyFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (-not [IO.File]::Exists($Path)) {
        throw "$Description was not created: $Path"
    }
    $fileInfo = [IO.FileInfo]::new($Path)
    if ($fileInfo.Length -le 0) {
        throw "$Description is empty: $Path"
    }
}

function Publish-GateArtifact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $sourceFull = [IO.Path]::GetFullPath($SourcePath)
    $destinationFull = [IO.Path]::GetFullPath($DestinationPath)
    $sourceDirectory = [IO.Path]::GetDirectoryName($sourceFull)
    $destinationDirectory = [IO.Path]::GetDirectoryName($destinationFull)
    $comparison = if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        [StringComparison]::OrdinalIgnoreCase
    }
    else {
        [StringComparison]::Ordinal
    }
    if (-not $sourceDirectory.Equals($destinationDirectory, $comparison)) {
        throw "$Description must be published inside its validated artifact directory"
    }
    if ([IO.File]::Exists($destinationFull)) {
        throw "$Description destination already exists: $destinationFull"
    }

    Assert-NonEmptyFile -Path $sourceFull -Description $Description
    [IO.File]::Move($sourceFull, $destinationFull)
    Assert-NonEmptyFile -Path $destinationFull -Description $Description
}

function Assert-IntegrationEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )

    $databaseUrl = [Environment]::GetEnvironmentVariable("TEST_DATABASE_URL")
    $redisUrl = [Environment]::GetEnvironmentVariable("TEST_REDIS_URL")
    $destructiveSentinel = [Environment]::GetEnvironmentVariable("XUANHU_ALLOW_DESTRUCTIVE_TESTS")

    if ([string]::IsNullOrWhiteSpace($databaseUrl)) {
        throw "TEST_DATABASE_URL is required and must identify an explicit guarded test database"
    }
    if ([string]::IsNullOrWhiteSpace($redisUrl)) {
        throw "TEST_REDIS_URL is required and must identify Redis logical database 8 through 15"
    }
    if ($destructiveSentinel -ne "1") {
        throw "XUANHU_ALLOW_DESTRUCTIVE_TESTS must equal 1"
    }

    $guardCode = @"
from tests._database_safety import require_destructive_test_database, require_destructive_test_redis
require_destructive_test_database()
require_destructive_test_redis()
print('integration targets validated')
"@
    Invoke-NativeGate -Command "uv" -Arguments @(
        "run", "--isolated", "--python", "3.12", "--locked", "python", "-c", $guardCode
    ) -WorkingDirectory $RepoRoot
}

function Assert-CleanRepository {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )

    $worktreeStatus = Invoke-NativeCapture -Command "git" -Arguments @(
        "-C", $RepoRoot, "status", "--porcelain=v1", "--untracked-files=all"
    ) -WorkingDirectory $RepoRoot
    if (-not [string]::IsNullOrWhiteSpace($worktreeStatus)) {
        throw "L0-L4 reacceptance requires a clean worktree so every result maps to exact HEAD"
    }
}

function Assert-ExactHead {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedHead
    )

    $currentHead = Invoke-NativeCapture -Command "git" -Arguments @(
        "-C", $RepoRoot, "rev-parse", "HEAD"
    ) -WorkingDirectory $RepoRoot
    if ($currentHead -ne $ExpectedHead) {
        throw "Repository HEAD changed while the L0-L4 reacceptance gate was running"
    }
}

function Assert-SafeTemporaryWorktreePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$CandidatePath,
        [Parameter(Mandatory = $true)]
        [string]$ApprovedRoot,
        [switch]$RequireExistingWorktree
    )

    $trimCharacters = [char[]]@(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd($trimCharacters)
    $rootFull = [IO.Path]::GetFullPath($ApprovedRoot).TrimEnd($trimCharacters)
    $candidateFull = [IO.Path]::GetFullPath($CandidatePath).TrimEnd($trimCharacters)
    $separator = [IO.Path]::DirectorySeparatorChar
    $isWindowsPlatform = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
    $comparison = if ($isWindowsPlatform) {
        [StringComparison]::OrdinalIgnoreCase
    }
    else {
        [StringComparison]::Ordinal
    }

    $systemTempPrefix = "${systemTemp}${separator}"
    if (-not $rootFull.StartsWith($systemTempPrefix, $comparison)) {
        throw "Approved worktree root must be a child of the operating-system temporary directory"
    }
    $approvedPrefix = "${rootFull}${separator}"
    if (-not $candidateFull.StartsWith($approvedPrefix, $comparison)) {
        throw "Temporary worktree path escaped the approved worktree root"
    }
    if ($candidateFull.Equals($rootFull, $comparison)) {
        throw "Temporary worktree path must not equal its approved root"
    }

    if ($RequireExistingWorktree) {
        if (-not (Test-Path -LiteralPath $candidateFull -PathType Container)) {
            throw "Temporary worktree directory does not exist"
        }
        $reportedRoot = Invoke-NativeCapture -Command "git" -Arguments @(
            "-C", $candidateFull, "rev-parse", "--show-toplevel"
        ) -WorkingDirectory $candidateFull
        $reportedRootFull = [IO.Path]::GetFullPath($reportedRoot).TrimEnd($trimCharacters)
        if (-not $reportedRootFull.Equals($candidateFull, $comparison)) {
            throw "Temporary cleanup target is not the expected Git worktree root"
        }
    }

    return $candidateFull
}

foreach ($requiredTool in @("git", "uv", "node", "npm", "docker")) {
    if ($null -eq (Get-Command $requiredTool -ErrorAction SilentlyContinue)) {
        throw "Required tool is unavailable: $requiredTool"
    }
}

$repoOutput = @(& git -C $PSScriptRoot rev-parse --show-toplevel)
if ($LASTEXITCODE -ne 0 -or $repoOutput.Count -ne 1) {
    throw "Unable to resolve repository root"
}
$RepoRoot = [IO.Path]::GetFullPath($repoOutput[0].Trim())
$FrontendRoot = Join-Path $RepoRoot "frontend"

Assert-CleanRepository -RepoRoot $RepoRoot

if ([string]::IsNullOrWhiteSpace($ArtifactDirectory)) {
    $ArtifactDirectory = Join-Path $RepoRoot ".codex_tmp/l0-l4-reacceptance"
}
$ArtifactDirectory = [IO.Path]::GetFullPath($ArtifactDirectory)
New-Item -ItemType Directory -Path $ArtifactDirectory -Force | Out-Null
$KnownArtifactNames = @(
    "environment-evidence.json",
    "requirements-production.txt",
    "requirements-production.txt.partial",
    "sbom-python.cdx.json",
    "sbom-python.cdx.json.partial",
    "sbom-node.cdx.json",
    "sbom-node.cdx.json.partial",
    "reacceptance-result.json",
    "reacceptance-result.json.partial"
)
foreach ($artifactName in $KnownArtifactNames) {
    [IO.File]::Delete((Join-Path $ArtifactDirectory $artifactName))
}
Assert-IntegrationEnvironment -RepoRoot $RepoRoot

Write-Host "==> Toolchain and infrastructure evidence manifest"
$uvVersion = Invoke-NativeCapture -Command "uv" -Arguments @("--version") -WorkingDirectory $RepoRoot
$nodeVersion = Invoke-NativeCapture -Command "node" -Arguments @("--version") -WorkingDirectory $RepoRoot
$npmVersion = Invoke-NativeCapture -Command "npm" -Arguments @("--version") -WorkingDirectory $RepoRoot
if ($uvVersion -notmatch '^uv 0\.(9|10|11)\.') {
    throw "Unsupported uv version; expected the validated 0.9 through 0.11 line"
}
if ($nodeVersion -notmatch '^v24\.') {
    throw "Unsupported Node.js version; expected major version 24"
}
if ($npmVersion -notmatch '^11\.') {
    throw "Unsupported npm version; expected major version 11"
}
$infrastructureVersionCode = @"
import json, os
import psycopg
from redis import Redis
with psycopg.connect(os.environ['TEST_DATABASE_URL']) as connection:
    postgres_version = connection.info.server_version
redis = Redis.from_url(os.environ['TEST_REDIS_URL'], decode_responses=True)
try:
    redis_version = redis.info(section='server')['redis_version']
finally:
    redis.close()
print(json.dumps({'postgres_server_version_num': postgres_version, 'redis_version': redis_version}, sort_keys=True))
"@
$infrastructureVersions = (
    Invoke-NativeCapture -Command "uv" -Arguments @(
        "run", "--isolated", "--python", "3.12", "--locked", "python", "-c",
        $infrastructureVersionCode
    ) -WorkingDirectory $RepoRoot
) | ConvertFrom-Json
$ExpectedGitHead = Invoke-NativeCapture -Command "git" -Arguments @(
    "-C", $RepoRoot, "rev-parse", "HEAD"
) -WorkingDirectory $RepoRoot
$evidenceManifest = [ordered]@{
    git_head = $ExpectedGitHead
    powershell = $PSVersionTable.PSVersion.ToString()
    uv = $uvVersion
    python_3_11 = Invoke-NativeCapture -Command "uv" -Arguments @(
        "run", "--isolated", "--python", "3.11", "--locked", "python", "--version"
    ) -WorkingDirectory $RepoRoot
    python_3_12 = Invoke-NativeCapture -Command "uv" -Arguments @(
        "run", "--isolated", "--python", "3.12", "--locked", "python", "--version"
    ) -WorkingDirectory $RepoRoot
    node = $nodeVersion
    npm = $npmVersion
    docker = Invoke-NativeCapture -Command "docker" -Arguments @("--version") -WorkingDirectory $RepoRoot
    postgres_server_version_num = $infrastructureVersions.postgres_server_version_num
    redis = $infrastructureVersions.redis_version
    pinned_images = @(
        "prom/prometheus:v3.5.0",
        "rhysd/actionlint:1.7.7",
        "ghcr.io/gitleaks/gitleaks:v8.28.0"
    )
}
$EvidenceManifestPath = Join-Path $ArtifactDirectory "environment-evidence.json"
[IO.File]::WriteAllText(
    $EvidenceManifestPath,
    ($evidenceManifest | ConvertTo-Json -Depth 4) + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)

Write-Host "==> Python 3.11 unit and L0 contract gate"
Invoke-NativeGate -Command "uv" -Arguments @(
    "run", "--isolated", "--python", "3.11", "--locked", "pytest"
) -WorkingDirectory $RepoRoot

Write-Host "==> Python 3.12 unit and L0 contract gate"
Invoke-NativeGate -Command "uv" -Arguments @(
    "run", "--isolated", "--python", "3.12", "--locked", "pytest"
) -WorkingDirectory $RepoRoot

Write-Host "==> Python static and lock gates"
Invoke-NativeGate -Command "uv" -Arguments @(
    "run", "--isolated", "--python", "3.12", "--locked", "ruff", "check", "."
) -WorkingDirectory $RepoRoot
Invoke-NativeGate -Command "uv" -Arguments @(
    "run", "--isolated", "--python", "3.12", "--locked", "mypy", "app", "scripts"
) -WorkingDirectory $RepoRoot
Invoke-NativeGate -Command "uv" -Arguments @("lock", "--check") -WorkingDirectory $RepoRoot
Invoke-NativeGate -Command "git" -Arguments @("-C", $RepoRoot, "diff", "--check") -WorkingDirectory $RepoRoot

Write-Host "==> Isolated PostgreSQL and Redis integration gate"
Invoke-NativeGate -Command "uv" -Arguments @(
    "run", "--isolated", "--python", "3.12", "--locked", "pytest",
    "-o", "addopts=", "-m", "integration and not performance", "-n", "4", "--dist=loadscope",
    "--strict-markers", "tests"
) -WorkingDirectory $RepoRoot

Write-Host "==> Serial production-shaped Legacy and LangGraph performance baselines"
Invoke-NativeGate -Command "uv" -Arguments @(
    "run", "--isolated", "--python", "3.12", "--locked", "pytest",
    "-o", "addopts=", "-m", "integration and performance", "--strict-markers",
    "tests/golden/test_legacy_performance_baseline.py",
    "tests/golden/test_langgraph_performance_baseline.py"
) -WorkingDirectory $RepoRoot

Write-Host "==> PostgreSQL and Redis xdist collision gate"
Invoke-NativeGate -Command "uv" -Arguments @(
    "run", "--isolated", "--python", "3.12", "--locked", "pytest",
    "-o", "addopts=", "-m", "integration", "-n", "2", "--dist=each",
    "--strict-markers", "tests/test_infrastructure_isolation.py"
) -WorkingDirectory $RepoRoot

Write-Host "==> Frontend type, lint, test, build, and production dependency audit gates"
Invoke-NativeGate -Command "npm" -Arguments @("ci") -WorkingDirectory $FrontendRoot
Invoke-NativeGate -Command "npm" -Arguments @("run", "typecheck") -WorkingDirectory $FrontendRoot
Invoke-NativeGate -Command "npm" -Arguments @("run", "lint") -WorkingDirectory $FrontendRoot
Invoke-NativeGate -Command "npm" -Arguments @("test") -WorkingDirectory $FrontendRoot
Invoke-NativeGate -Command "npm" -Arguments @("run", "build") -WorkingDirectory $FrontendRoot
Invoke-NativeGate -Command "npm" -Arguments @(
    "audit", "--omit=dev", "--audit-level=high"
) -WorkingDirectory $FrontendRoot

Write-Host "==> Python locked dependency audit"
$RequirementsPath = Join-Path $ArtifactDirectory "requirements-production.txt"
$RequirementsPartialPath = Join-Path $ArtifactDirectory "requirements-production.txt.partial"
Invoke-NativeGate -Command "uv" -Arguments @(
    "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements.txt",
    "--output-file", $RequirementsPartialPath
) -WorkingDirectory $RepoRoot | Out-Null
Publish-GateArtifact `
    -SourcePath $RequirementsPartialPath `
    -DestinationPath $RequirementsPath `
    -Description "Locked production requirements"
Invoke-NativeGate -Command "uv" -Arguments @(
    "tool", "run", "--from", "pip-audit==2.10.1", "pip-audit", "--strict",
    "--requirement", $RequirementsPath
) -WorkingDirectory $RepoRoot

Write-Host "==> Reproducible Python CycloneDX SBOM"
$PythonSbomPath = Join-Path $ArtifactDirectory "sbom-python.cdx.json"
$PythonSbomPartialPath = Join-Path $ArtifactDirectory "sbom-python.cdx.json.partial"
Invoke-NativeGate -Command "uv" -Arguments @(
    "tool", "run", "--from", "cyclonedx-bom==7.3.0", "cyclonedx-py", "requirements",
    $RequirementsPath, "--pyproject", (Join-Path $RepoRoot "pyproject.toml"),
    "--spec-version", "1.6", "--output-reproducible", "--output-format", "JSON",
    "--output-file", $PythonSbomPartialPath
) -WorkingDirectory $RepoRoot
Assert-NonEmptyFile -Path $PythonSbomPartialPath -Description "Python CycloneDX SBOM"
$pythonSbom = Get-Content -LiteralPath $PythonSbomPartialPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($pythonSbom.bomFormat -ne "CycloneDX" -or $pythonSbom.specVersion -ne "1.6") {
    throw "Python SBOM did not satisfy the CycloneDX 1.6 contract"
}
Publish-GateArtifact `
    -SourcePath $PythonSbomPartialPath `
    -DestinationPath $PythonSbomPath `
    -Description "Python CycloneDX SBOM"

Write-Host "==> Locked Node CycloneDX SBOM"
Invoke-NativeGate -Command "npm" -Arguments @("ci", "--ignore-scripts") -WorkingDirectory $FrontendRoot
$NodeSbomPath = Join-Path $ArtifactDirectory "sbom-node.cdx.json"
$NodeSbomPartialPath = Join-Path $ArtifactDirectory "sbom-node.cdx.json.partial"
$nodeSbomText = Invoke-NativeCapture -Command "npm" -Arguments @(
    "sbom", "--sbom-format", "cyclonedx"
) -WorkingDirectory $FrontendRoot
[IO.File]::WriteAllText(
    $NodeSbomPartialPath,
    $nodeSbomText + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
Assert-NonEmptyFile -Path $NodeSbomPartialPath -Description "Node CycloneDX SBOM"
$nodeSbom = Get-Content -LiteralPath $NodeSbomPartialPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($nodeSbom.bomFormat -ne "CycloneDX") {
    throw "Node SBOM did not satisfy the CycloneDX contract"
}
Publish-GateArtifact `
    -SourcePath $NodeSbomPartialPath `
    -DestinationPath $NodeSbomPath `
    -Description "Node CycloneDX SBOM"

$repoReadOnlyMount = "${RepoRoot}:/repo:ro"
Write-Host "==> Prometheus Outbox alerting rule syntax gate"
Invoke-NativeGate -Command "docker" -Arguments @(
    "run", "--rm", "--entrypoint=/bin/promtool", "--volume", $repoReadOnlyMount,
    "--workdir=/repo", "prom/prometheus:v3.5.0", "check", "rules",
    "deploy/prometheus/rules/xuanhu-outbox-alerts.yml"
) -WorkingDirectory $RepoRoot
Invoke-NativeGate -Command "docker" -Arguments @(
    "run", "--rm", "--entrypoint=/bin/promtool", "--volume", $repoReadOnlyMount,
    "--workdir=/repo", "prom/prometheus:v3.5.0", "test", "rules",
    "deploy/prometheus/tests/xuanhu-outbox-alerts.test.yml"
) -WorkingDirectory $RepoRoot

Write-Host "==> GitHub Actions workflow syntax gate"
Invoke-NativeGate -Command "docker" -Arguments @(
    "run", "--rm", "--volume", $repoReadOnlyMount, "--workdir", "/repo",
    "rhysd/actionlint:1.7.7", "-color"
) -WorkingDirectory $RepoRoot

Write-Host "==> Gitleaks repository history gate"
Assert-CleanRepository -RepoRoot $RepoRoot
Assert-ExactHead -RepoRoot $RepoRoot -ExpectedHead $ExpectedGitHead
$repoMount = "${RepoRoot}:/repo:ro"
Invoke-NativeGate -Command "docker" -Arguments @(
    "run", "--rm", "--volume", $repoMount,
    "ghcr.io/gitleaks/gitleaks:v8.28.0", "git", "/repo",
    "--gitleaks-ignore-path=/repo/.gitleaksignore", "--no-banner", "--redact"
) -WorkingDirectory $RepoRoot

Write-Host "==> Gitleaks detached clean-worktree gate"
$TemporaryWorktreeRoot = Join-Path ([IO.Path]::GetTempPath()) "xuanhu-l0-l4-reacceptance-worktrees"
New-Item -ItemType Directory -Path $TemporaryWorktreeRoot -Force | Out-Null
$CleanWorktreePath = Join-Path $TemporaryWorktreeRoot ("clean-" + [guid]::NewGuid().ToString("N"))
$CleanWorktreePath = Assert-SafeTemporaryWorktreePath `
    -CandidatePath $CleanWorktreePath `
    -ApprovedRoot $TemporaryWorktreeRoot
$worktreeAdded = $false
try {
    Invoke-NativeGate -Command "git" -Arguments @(
        "-C", $RepoRoot, "worktree", "add", "--detach", $CleanWorktreePath, $ExpectedGitHead
    ) -WorkingDirectory $RepoRoot
    $worktreeAdded = $true
    $CleanWorktreePath = Assert-SafeTemporaryWorktreePath `
        -CandidatePath $CleanWorktreePath `
        -ApprovedRoot $TemporaryWorktreeRoot `
        -RequireExistingWorktree
    $cleanMount = "${CleanWorktreePath}:/repo:ro"
    Invoke-NativeGate -Command "docker" -Arguments @(
        "run", "--rm", "--volume", $cleanMount,
        "ghcr.io/gitleaks/gitleaks:v8.28.0", "dir", "/repo",
        "--gitleaks-ignore-path=/repo/.gitleaksignore", "--no-banner", "--redact"
    ) -WorkingDirectory $RepoRoot
}
finally {
    if ($worktreeAdded) {
        $CleanWorktreePath = Assert-SafeTemporaryWorktreePath `
            -CandidatePath $CleanWorktreePath `
            -ApprovedRoot $TemporaryWorktreeRoot `
            -RequireExistingWorktree
        Invoke-NativeGate -Command "git" -Arguments @(
            "-C", $RepoRoot, "worktree", "remove", "--force", $CleanWorktreePath
        ) -WorkingDirectory $RepoRoot
        if (Test-Path -LiteralPath $CleanWorktreePath) {
            throw "Validated temporary worktree still exists after cleanup"
        }
        Invoke-NativeGate -Command "git" -Arguments @(
            "-C", $RepoRoot, "worktree", "prune"
        ) -WorkingDirectory $RepoRoot
    }
}

Assert-CleanRepository -RepoRoot $RepoRoot
Assert-ExactHead -RepoRoot $RepoRoot -ExpectedHead $ExpectedGitHead
$ResultManifestPath = Join-Path $ArtifactDirectory "reacceptance-result.json"
$ResultManifestPartialPath = Join-Path $ArtifactDirectory "reacceptance-result.json.partial"
$resultManifest = [ordered]@{
    status = "passed"
    git_head = $ExpectedGitHead
    passed_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    artifact_sha256 = [ordered]@{
        environment_evidence = (Get-FileHash -LiteralPath $EvidenceManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        production_requirements = (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash.ToLowerInvariant()
        python_sbom = (Get-FileHash -LiteralPath $PythonSbomPath -Algorithm SHA256).Hash.ToLowerInvariant()
        node_sbom = (Get-FileHash -LiteralPath $NodeSbomPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
[IO.File]::WriteAllText(
    $ResultManifestPartialPath,
    ($resultManifest | ConvertTo-Json -Depth 4) + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
Assert-NonEmptyFile -Path $ResultManifestPartialPath -Description "Final reacceptance result manifest"
$validatedResultManifest = Get-Content `
    -LiteralPath $ResultManifestPartialPath `
    -Raw `
    -Encoding utf8 | ConvertFrom-Json
if (
    $validatedResultManifest.status -ne "passed" -or
    $validatedResultManifest.git_head -ne $ExpectedGitHead
) {
    throw "Final reacceptance result manifest did not satisfy the exact-HEAD success contract"
}
Publish-GateArtifact `
    -SourcePath $ResultManifestPartialPath `
    -DestinationPath $ResultManifestPath `
    -Description "Final reacceptance result manifest"

Write-Host "L0-L4 engineering reacceptance gates passed for exact HEAD."
Write-Host "SBOM and audit inputs: $ArtifactDirectory"
Write-Host "Final success receipt: $ResultManifestPath"
