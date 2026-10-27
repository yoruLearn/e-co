#requires -version 5.1
<#
.SYNOPSIS
    Run the same gates as CI locally (Spec section 16.3).
.DESCRIPTION
    1) ruff check  2) ruff format --check  3) mypy  4) pytest + coverage  5) docker compose config
    Prefers the in-repo .venv (works offline), falls back to `uv run`.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipCompose
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $venvPy = Join-Path $root '.venv\Scripts\python.exe'
    $useVenv = Test-Path $venvPy

    function Invoke-Py {
        param([string[]]$PyArgs)
        if ($useVenv) { & $venvPy @PyArgs | Out-Host }
        else { & uv run @PyArgs | Out-Host }
        return $LASTEXITCODE
    }

    $failures = @()

    Write-Host '=== [1/5] ruff check ===' -ForegroundColor Cyan
    if ((Invoke-Py @('-m', 'ruff', 'check', '.')) -ne 0) { $failures += 'ruff' }

    Write-Host '=== [2/5] ruff format --check ===' -ForegroundColor Cyan
    if ((Invoke-Py @('-m', 'ruff', 'format', '--check', '.')) -ne 0) { $failures += 'ruff-format' }

    Write-Host '=== [3/5] mypy ===' -ForegroundColor Cyan
    if ((Invoke-Py @('-m', 'mypy', 'app')) -ne 0) { $failures += 'mypy' }

    if (-not $SkipTests) {
        Write-Host '=== [4/5] pytest + coverage ===' -ForegroundColor Cyan
        if ((Invoke-Py @('-m', 'pytest', '-q', '--cov=app', '--cov-report=term-missing')) -ne 0) {
            $failures += 'pytest'
        }
    }

    if (-not $SkipCompose) {
        Write-Host '=== [5/5] docker compose config ===' -ForegroundColor Cyan
        & docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config -q
        if ($LASTEXITCODE -ne 0) { $failures += 'compose-config' }
    }

    if ($failures.Count -gt 0) {
        Write-Host ''
        Write-Host "GATES FAILED: $($failures -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host ''
    Write-Host 'ALL GATES GREEN' -ForegroundColor Green
}
finally {
    Pop-Location
}
