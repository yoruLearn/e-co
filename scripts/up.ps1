#requires -version 7.0
<#
.SYNOPSIS
    一键启动 M0 开发栈（api / worker / mysql / redis / chroma / nginx）。
.EXAMPLE
    pwsh scripts/up.ps1 -Build
    pwsh scripts/up.ps1 -WithMq -WithObs
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
    Write-Host "[up] 已从 .env.example 生成 deploy/.env（dev 默认值，勿提交）"
}

& docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) {
    throw '[up] Docker daemon 未运行：请先启动 Docker Desktop（compose config 校验不需要 daemon，起栈需要）'
}

if ($WithMq) { $env:MQ_ENABLED = 'true' }

$composeArgs = @('compose', '-f', $composeFile, '--env-file', $envFile)
if ($WithMq) { $composeArgs += @('--profile', 'mq') }
if ($WithObs) { $composeArgs += @('--profile', 'obs') }
$composeArgs += @('up', '-d')
if ($Build) { $composeArgs += '--build' }

Write-Host "[up] docker $($composeArgs -join ' ')"
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw "[up] docker compose 启动失败（exit $LASTEXITCODE）" }

Write-Host "[up] 等待 /readyz 就绪（最多 ${TimeoutSec}s）..."
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/readyz' -TimeoutSec 5
        if ($resp.code -eq 0) {
            $ready = $true
            Write-Host ("[up] ready: " + ($resp.data.checks | ForEach-Object { "$($_.name)=$($_.status)" } -join ', '))
            break
        }
    }
    catch {
        Start-Sleep -Seconds 3
    }
}

if (-not $ready) {
    Write-Warning "[up] ${TimeoutSec}s 内未就绪，以下为诊断信息："
    & docker @composeArgs[0..($composeArgs.Length - 1)] ps
    & docker compose -f $composeFile --env-file $envFile logs --tail 40 api
    throw '[up] 栈未就绪，请检查 mysql/redis/chroma 日志'
}

Write-Host ''
Write-Host '  API      : http://127.0.0.1:8000/docs'
Write-Host '  Nginx    : http://127.0.0.1:8080/healthz'
Write-Host '  Metrics  : http://127.0.0.1:8000/metrics'
if ($WithObs) { Write-Host '  Grafana  : http://127.0.0.1:3000 (admin/admin)' }
Write-Host '  停止     : pwsh scripts/down.ps1'
