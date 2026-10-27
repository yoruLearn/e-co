#requires -version 5.1
<#
.SYNOPSIS
    Show the dev stack health matrix plus probe output.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.yml'
$envFile = Join-Path $root 'deploy/.env'

Write-Host '=== docker compose ps ===' -ForegroundColor Cyan
$pipes = @('\\.\pipe\docker_engine', '\\.\pipe\dockerDesktopLinuxEngine')
if (-not ($pipes | Where-Object { Test-Path $_ })) {
    Write-Warning 'Docker daemon is not running (compose status unavailable); probing local port 8000 only.'
    $daemonUp = $false
}
else {
    $daemonUp = $true
}
else {
    $composeArgs = @('compose', '-f', $composeFile)
    if (Test-Path $envFile) { $composeArgs += @('--env-file', $envFile) }
    else { $composeArgs += @('--env-file', (Join-Path $root 'deploy/.env.example')) }
    & docker @($composeArgs + @('ps', '--format', 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'))
}

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
        $msg = $_.Exception.Message
        Write-Warning "${path} request failed: $msg"
    }
}
