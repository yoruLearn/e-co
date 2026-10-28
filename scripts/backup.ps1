#Requires -Version 5.1
<#
.SYNOPSIS
  Backup MySQL + Redis (staging/production drill helper).
.DESCRIPTION
  Dumps MySQL via the compose service, triggers Redis SAVE and copies dump.rdb,
  then writes sha256 checksums. ASCII-only on purpose (Windows PowerShell 5.1).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/backup.ps1 -OutDir D:\backups\e-co
#>
param(
    [string]$OutDir = ".\backups",
    [string]$ComposeFile = "deploy/docker-compose.yml",
    [string]$MysqlService = "mysql",
    [string]$RedisService = "redis",
    [string]$MysqlUser = "root",
    [string]$MysqlPassword = "root",
    [string]$Database = "aftersales"
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $OutDir $stamp
New-Item -ItemType Directory -Force -Path $target | Out-Null

Write-Host "[backup] mysql dump -> $target/mysql.sql"
docker compose -f $ComposeFile exec -T $MysqlService `
    mysqldump -u"$MysqlUser" -p"$MysqlPassword" --single-transaction --routines --triggers $Database `
    | Out-File -FilePath (Join-Path $target "mysql.sql") -Encoding utf8

Write-Host "[backup] redis save + rdb copy"
docker compose -f $ComposeFile exec -T $RedisService redis-cli SAVE | Out-Null
docker compose -f $ComposeFile cp "${RedisService}:/data/dump.rdb" (Join-Path $target "dump.rdb") | Out-Null

Write-Host "[backup] checksums"
Get-ChildItem $target | ForEach-Object {
    $hash = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower()
    "$hash  $($_.Name)" | Out-File -Append -Encoding ascii (Join-Path $target "SHA256SUMS")
}

Write-Host "[backup] done: $target"
