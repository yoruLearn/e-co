#Requires -Version 5.1
<#
.SYNOPSIS
  Restore MySQL from a backup produced by backup.ps1 (drill helper).
.DESCRIPTION
  Verifies SHA256SUMS, restores MySQL, then prints table count for sanity check.
  ASCII-only on purpose (Windows PowerShell 5.1).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/restore.ps1 -BackupDir D:\backups\e-co\20261028-120000
#>
param(
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [string]$ComposeFile = "deploy/docker-compose.yml",
    [string]$MysqlService = "mysql",
    [string]$MysqlUser = "root",
    [string]$MysqlPassword = "root",
    [string]$Database = "aftersales"
)

$ErrorActionPreference = "Stop"
$sumFile = Join-Path $BackupDir "SHA256SUMS"
if (-not (Test-Path $sumFile)) { throw "SHA256SUMS not found: $sumFile" }

Write-Host "[restore] verifying checksums"
Get-Content $sumFile | ForEach-Object {
    $parts = $_ -split "\s+", 2
    $expected = $parts[0]
    $name = $parts[1]
    $actual = (Get-FileHash -Algorithm SHA256 (Join-Path $BackupDir $name)).Hash.ToLower()
    if ($actual -ne $expected) { throw "checksum mismatch: $name" }
}

Write-Host "[restore] mysql < mysql.sql"
Get-Content (Join-Path $BackupDir "mysql.sql") -Raw | `
    docker compose -f $ComposeFile exec -T $MysqlService mysql -u"$MysqlUser" -p"$MysqlPassword" $Database

Write-Host "[restore] sanity: table count"
docker compose -f $ComposeFile exec -T $MysqlService `
    mysql -u"$MysqlUser" -p"$MysqlPassword" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$Database';"

Write-Host "[restore] done. Redis RDB restore: stop redis, copy dump.rdb to /data, start redis."
