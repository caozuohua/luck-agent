<# scripts/test-local.ps1 — Run luck-agent tests on Windows (PowerShell / pwsh)
<#
# Why this script exists:
#   * The agent runs inside the Hermes runtime, which injects PYTHONPATH
#     pointing at Hermes' own (broken) pydantic_core. That breaks
#     `google-genai` / `lark-oapi` imports. We clear PYTHONPATH so the
#     project's own .venv is the only source.
#   * pytest.ini sets asyncio_mode=auto and testpaths=tests/unit,tests/integration.
#
# Usage:
#   pwsh ./scripts/test-local.ps1                 # unit + integration (fast, offline)
#   pwsh ./scripts/test-local.ps1 -All            # full suite (unit+integration+root)
#   pwsh ./scripts/test-local.ps1 -Path tests/unit/test_router.py
#
# The V2 runtime uses a FakeLLMClient when LLM_BASE_URL is unset, so the
# entire suite runs offline with no model credentials.
#>
param(
    [switch]$All,
    [string]$Path = "",
    [string]$Venv = ".venv"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

# 1. Use the project venv exclusively (drop Hermes PYTHONPATH pollution).
$env:PYTHONPATH = ""
$python = Join-Path $Venv "Scripts/python.exe"
if (-not (Test-Path $python)) {
    Write-Host "venv not found at $python — run: uv venv --python 3.12 ; uv pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

# 2. Default = unit + integration (offline, no cloud). -All = everything.
if ($Path) {
    $args = @($Path)
} elseif ($All) {
    $args = @("tests/")
} else {
    $args = @("tests/unit", "tests/integration")
}
$resultDir = Join-Path $root "workspace/test-results"
New-Item -ItemType Directory -Force -Path $resultDir | Out-Null
$resultFile = Join-Path $resultDir ("pytest-" + [guid]::NewGuid().ToString("N") + ".xml")
$args += @("-q", "-p", "no:cacheprovider", "--junitxml=$resultFile")

Write-Host "Running: $python -m pytest $($args -join ' ')" -ForegroundColor Cyan
& $python -m pytest @args
$testExitCode = $LASTEXITCODE
if ($testExitCode -ne 0) { exit $testExitCode }
# An abrupt background os._exit(0) used to make the full suite look green.
# Require a fresh, completed pytest report before accepting exit code zero.
if (-not (Test-Path -LiteralPath $resultFile)) {
    Write-Error "pytest exited without a completed test report: $resultFile"
    exit 1
}
try {
    [xml]$testReport = Get-Content -Raw -LiteralPath $resultFile
    $suites = @($testReport.testsuites.testsuite)
    $totalTests = ($suites | ForEach-Object { [int]$_.tests } | Measure-Object -Sum).Sum
    $badTests = ($suites | ForEach-Object { [int]$_.failures + [int]$_.errors } | Measure-Object -Sum).Sum
    if ($totalTests -le 0 -or $badTests -gt 0) { throw "empty or failed test report" }
} catch {
    Write-Error "Invalid pytest completion report: $_"
    exit 1
}
exit 0
