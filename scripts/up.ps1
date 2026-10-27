#requires -version 5.1
<#
.SYNOPSIS
    Start the M0 dev stack (api / worker / mysql / redis / chroma / nginx).
.DESCRIPTION
    ASCII-only output on purpose: Windows PowerShell 5.1 decodes BOM-less UTF-8 scripts
    as ANSI, which can corrupt Chinese literals and break parsing.
.EXAMPLE
    powershell -File scripts/up.ps1 -Build
    pwsh -File scripts/up.ps1 -WithMq -WithObs
#>
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$WithMq,
    [switch]$WithObs,
    [int]$TimeoutSec = 240
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.yml'
$envFile = Join-Path $root 'deploy/.env'

if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root 'deploy/.env.example') $envFile
    Write-Host "[up] created deploy/.env from .env.example (dev defaults; do not commit)"
}

# Docker Desktop exposes a named pipe; checking it avoids the noisy NativeCommandError
# that Windows PowerShell 5.1 raises when invoking `docker info` with a dead daemon.
$pipes = @('\\.\pipe\docker_engine', '\\.\pipe\dockerDesktopLinuxEngine')
if (-not ($pipes | Where-Object { Test-Path $_ })) {
    throw '[up] Docker daemon is not running. Start Docker Desktop first (compose config check does not need it, starting the stack does).'
}

if ($WithMq) { $env:MQ_ENABLED = 'true' }

$composeArgs = @('compose', '-f', $composeFile, '--env-file', $envFile)
if ($WithMq) { $composeArgs += @('--profile', 'mq') }
if ($WithObs) { $composeArgs += @('--profile', 'obs') }
$composeArgs += @('up', '-d')
if ($Build) { $composeArgs += '--build' }

Write-Host "[up] docker $($composeArgs -join ' ')"
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw "[up] docker compose up failed (exit $LASTEXITCODE)" }

Write-Host "[up] waiting for /readyz (timeout ${TimeoutSec}s) ..."
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/readyz' -TimeoutSec 5
        if ($resp.code -eq 0) {
            $ready = $true
            $summary = ($resp.data.checks | ForEach-Object { "$($_.name)=$($_.status)" }) -join ', '
            Write-Host "[up] ready: $summary"
            break
        }
    }
    catch {
        Start-Sleep -Seconds 3
    }
}

if (-not $ready) {
    Write-Warning "[up] not ready within ${TimeoutSec}s, collecting diagnostics:"
    & docker compose -f $composeFile --env-file $envFile ps
    & docker compose -f $composeFile --env-file $envFile logs --tail 40 api
    throw '[up] stack is not ready. Check mysql/redis/chroma logs.'
}

Write-Host ''
Write-Host '  API      : http://127.0.0.1:8000/docs'
Write-Host '  Nginx    : http://127.0.0.1:8080/healthz'
Write-Host '  Metrics  : http://127.0.0.1:8000/metrics'
if ($WithObs) { Write-Host '  Grafana  : http://127.0.0.1:3000 (admin/admin)' }
Write-Host '  Stop     : powershell -File scripts/down.ps1'
