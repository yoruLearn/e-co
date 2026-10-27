#requires -version 7.0
<#
.SYNOPSIS
    打印开发栈健康矩阵 + 探针结果。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.yml'
$envFile = Join-Path $root 'deploy/.env'

Write-Host '=== docker compose ps ===' -ForegroundColor Cyan
$composeArgs = @('compose', '-f', $composeFile)
if (Test-Path $envFile) { $composeArgs += @('--env-file', $envFile) } else { $composeArgs += @('--env-file', (Join-Path $root 'deploy/.env.example')) }
& docker @($composeArgs + @('ps', '--format', 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'))

foreach ($path in @('/healthz', '/readyz')) {
    Write-Host ''
    Write-Host "=== GET $path ===" -ForegroundColor Cyan
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8000$path" -TimeoutSec 5
        if ($path -eq '/readyz') {
            $resp.data.checks | Format-Table name, status, latency_ms, detail -AutoSize
        }
        else {
            $resp.data | Format-List
        }
    }
    catch {
        Write-Warning "$path 请求失败：$($_.Exception.Message)"
    }
}
