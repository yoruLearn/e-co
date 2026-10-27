#requires -version 5.1
<#
.SYNOPSIS
    Stop the dev stack. -Volumes also deletes data volumes.
.EXAMPLE
    powershell -File scripts/down.ps1
    powershell -File scripts/down.ps1 -Volumes -WithObs -WithMq
#>
[CmdletBinding()]
param(
    [switch]$Volumes,
    [switch]$WithMq,
    [switch]$WithObs
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.yml'
$envFile = Join-Path $root 'deploy/.env'

$composeArgs = @('compose', '-f', $composeFile)
if (Test-Path $envFile) { $composeArgs += @('--env-file', $envFile) }
if ($WithMq) { $composeArgs += @('--profile', 'mq') }
if ($WithObs) { $composeArgs += @('--profile', 'obs') }
$composeArgs += @('down', '--remove-orphans', '--timeout', '30')
if ($Volumes) { $composeArgs += '--volumes' }

Write-Host "[down] docker $($composeArgs -join ' ')"
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw "[down] docker compose down failed (exit $LASTEXITCODE)" }

if ($Volumes) { Write-Host '[down] stack stopped, data volumes removed' }
else { Write-Host '[down] stack stopped, data volumes kept' }
