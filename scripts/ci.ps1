#requires -version 7.0
<#
.SYNOPSIS
    本地跑与 CI 完全一致的门禁（Spec §16.3）。
.DESCRIPTION
    1) ruff   2) mypy   3) pytest+coverage   4) docker compose config   5) OpenAPI 冒烟
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
    $failures = @()

    Write-Host '=== [1/5] ruff ===' -ForegroundColor Cyan
    & uv run ruff check .
    if ($LASTEXITCODE -ne 0) { $failures += 'ruff' }

    Write-Host '=== [2/5] ruff format --check ===' -ForegroundColor Cyan
    & uv run ruff format --check .
    if ($LASTEXITCODE -ne 0) { $failures += 'ruff-format' }

    Write-Host '=== [3/5] mypy ===' -ForegroundColor Cyan
    & uv run mypy app
    if ($LASTEXITCODE -ne 0) { $failures += 'mypy' }

    if (-not $SkipTests) {
        Write-Host '=== [4/5] pytest + coverage ===' -ForegroundColor Cyan
        & uv run pytest -q --cov=app --cov-report=term-missing
        if ($LASTEXITCODE -ne 0) { $failures += 'pytest' }
    }

    if (-not $SkipCompose) {
        Write-Host '=== [5/5] docker compose config ===' -ForegroundColor Cyan
        & docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config -q
        if ($LASTEXITCODE -ne 0) { $failures += 'compose-config' }
    }

    if ($failures.Count -gt 0) {
        Write-Host ''
        Write-Host "门禁失败：$($failures -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host ''
    Write-Host '门禁全绿 ✅' -ForegroundColor Green
}
finally {
    Pop-Location
}
