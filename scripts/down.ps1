#requires -version 7.0
<#
.SYNOPSIS
    停止开发栈；-Volumes 同时删除数据卷（清库/清缓存）。
.EXAMPLE
    pwsh scripts/down.ps1
    pwsh scripts/down.ps1 -Volumes -WithObs -WithMq
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

# SSE 长连接先摘流：先停 nginx，再停 api/worker（Spec §17.3 drain）
& docker compose -f $composeFile down --timeout 30 2>$null | Out-Null

$composeArgs += @('down', '--remove-orphans')
if ($Volumes) { $composeArgs += '--volumes' }
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw "[down] docker compose 停止失败（exit $LASTEXITCODE）" }
Write-Host ('[down] 已停止' + $(if ($Volumes) { '（数据卷已删除）' } else { '（数据卷保留）' }))
