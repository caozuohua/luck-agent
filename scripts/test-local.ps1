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
$args += @("-q", "-p", "no:cacheprovider")

Write-Host "Running: $python -m pytest $($args -join ' ')" -ForegroundColor Cyan
& $python -m pytest @args
exit $LASTEXITCODE
