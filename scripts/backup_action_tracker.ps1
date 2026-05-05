#!/usr/bin/env pwsh
# OpsMemory — backup action_tracker database.
#
# Chunk 1 scope: pg_dump custom format, gzip-9 compression, optional rsync to
# Spark #2, retention prune, status JSON for /readyz.
#
# Chunk 1.5 will add: GPG encryption + Backblaze B2 offsite.
#
# Run via systemd timer at 02:17 America/Phoenix daily.
# Prerequisites on Spark: pg_dump >= 13, rsync, ssh, pwsh 7.
#
# Required env:
#   ACTION_TRACKER_DATABASE_URL    DSN with sufficient privileges to pg_dump (use opsmemory_owner)
# Optional env:
#   BACKUP_ROOT                    default /var/backups/opsmemory/action_tracker
#   BACKUP_RETENTION_DAYS          default 14
#   BACKUP_SPARK2_TARGET           e.g. opsbackup@spark2:/srv/backups/opsmemory/action_tracker/
#   BACKUP_STATUS_FILE             default /var/lib/opsmemory/backup/status.json
#   BACKUP_ALERT_WEBHOOK_URL       optional: failure alerts
#
# Exit codes:
#   0  success
#   1  configuration error
#   2  pg_dump failed
#   3  pg_restore --list verification failed
#   4  rsync to Spark #2 failed
#   5  status write failed

$ErrorActionPreference = "Stop"

function Send-FailureAlert($Reason, $Detail) {
    $url = $env:BACKUP_ALERT_WEBHOOK_URL
    if (-not $url) { return }
    try {
        $body = @{
            event = "opsmemory_backup_failed"
            reason = $Reason
            detail = $Detail
            host = $env:HOSTNAME
            timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
        } | ConvertTo-Json -Compress
        Invoke-WebRequest -Uri $url -Method POST -ContentType "application/json" -Body $body -TimeoutSec 10 | Out-Null
    } catch {
        Write-Warning "alert webhook failed: $($_.Exception.Message)"
    }
}

# ---- Validate config ----------------------------------------------------
$DatabaseUrl = $env:ACTION_TRACKER_DATABASE_URL
if (-not $DatabaseUrl) {
    Write-Error "ACTION_TRACKER_DATABASE_URL is required"
    exit 1
}

$BackupRoot = $env:BACKUP_ROOT
if (-not $BackupRoot) { $BackupRoot = "/var/backups/opsmemory/action_tracker" }

$Spark2Target = $env:BACKUP_SPARK2_TARGET   # may be empty
$RetentionDays = 14
if ($env:BACKUP_RETENTION_DAYS) { $RetentionDays = [int]$env:BACKUP_RETENTION_DAYS }

$StatusFile = $env:BACKUP_STATUS_FILE
if (-not $StatusFile) { $StatusFile = "/var/lib/opsmemory/backup/status.json" }

# ---- Compute paths ------------------------------------------------------
$Now      = Get-Date
$Year     = $Now.ToString("yyyy")
$Month    = $Now.ToString("MM")
$Stamp    = $Now.ToString("yyyyMMdd-HHmmss")
$BackupDir = Join-Path -Path $BackupRoot -ChildPath "$Year/$Month"
$DumpFile  = Join-Path -Path $BackupDir -ChildPath "action_tracker-$Stamp.dump"

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null

$StatusDir = Split-Path -Parent $StatusFile
New-Item -ItemType Directory -Path $StatusDir -Force | Out-Null

# ---- pg_dump ------------------------------------------------------------
Write-Host "[$(Get-Date -Format o)] pg_dump -> $DumpFile"
& pg_dump --dbname $DatabaseUrl `
          --format custom `
          --compress 9 `
          --no-owner `
          --no-acl `
          --file $DumpFile

if ($LASTEXITCODE -ne 0) {
    Send-FailureAlert "pg_dump_failed" "exit=$LASTEXITCODE file=$DumpFile"
    Write-Error "pg_dump failed (exit $LASTEXITCODE)"
    exit 2
}

$DumpSize = (Get-Item $DumpFile).Length
Write-Host "[$(Get-Date -Format o)] pg_dump complete: $([math]::Round($DumpSize/1MB, 2)) MB"

# ---- Verify dump is parseable ------------------------------------------
& pg_restore --list $DumpFile *> $null
if ($LASTEXITCODE -ne 0) {
    Send-FailureAlert "pg_restore_list_failed" "dump may be corrupt: $DumpFile"
    Write-Error "pg_restore --list failed — dump may be corrupt"
    exit 3
}

# ---- Optional: rsync to Spark #2 ---------------------------------------
$Rsynced = $false
if ($Spark2Target) {
    Write-Host "[$(Get-Date -Format o)] rsync -> $Spark2Target"
    & rsync -av --partial $DumpFile "${Spark2Target}/"
    if ($LASTEXITCODE -ne 0) {
        Send-FailureAlert "rsync_failed" "target=$Spark2Target exit=$LASTEXITCODE"
        Write-Error "rsync to Spark #2 failed (exit $LASTEXITCODE)"
        exit 4
    }
    $Rsynced = $true
} else {
    Write-Host "BACKUP_SPARK2_TARGET not set — skipping rsync"
}

# ---- Retention prune (local only) --------------------------------------
$CutoffDate = $Now.AddDays(-$RetentionDays)
$Pruned = 0
Get-ChildItem -Path $BackupRoot -Recurse -Filter "*.dump" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $CutoffDate } |
    ForEach-Object {
        Write-Host "Pruning $($_.FullName)"
        Remove-Item $_.FullName -Force
        $Pruned++
    }

# Also remove empty year/month dirs.
Get-ChildItem -Path $BackupRoot -Directory -Recurse -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending |
    Where-Object { -not (Get-ChildItem -Path $_.FullName) } |
    ForEach-Object { Remove-Item $_.FullName -Force }

# ---- Write status JSON --------------------------------------------------
$Status = [ordered]@{
    completed_at      = $Now.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    dump_path         = $DumpFile
    dump_bytes        = $DumpSize
    spark2_target     = $Spark2Target
    spark2_synced     = $Rsynced
    retention_days    = $RetentionDays
    pruned_count      = $Pruned
    encrypted         = $false
    offsite           = $false
    schema_version    = "0001_initial"
} | ConvertTo-Json

try {
    [IO.File]::WriteAllText($StatusFile, $Status)
} catch {
    Send-FailureAlert "status_write_failed" "$($_.Exception.Message)"
    Write-Error "status write failed: $($_.Exception.Message)"
    exit 5
}

Write-Host "[$(Get-Date -Format o)] backup complete dump=$DumpFile size=$([math]::Round($DumpSize/1MB,2))MB synced=$Rsynced pruned=$Pruned"
