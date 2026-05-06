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
# Container/role/db come via the docker exec block below (POSTGRES_CONTAINER,
# ACTION_TRACKER_DB_ROLE, ACTION_TRACKER_DB_NAME). The pre-1.5 script also
# read ACTION_TRACKER_DATABASE_URL; that variable is no longer used since
# pg_dump now connects via Unix socket inside the container with peer auth.
# Left intentionally absent to avoid confusion.

$BackupRoot = $env:BACKUP_ROOT
if (-not $BackupRoot) { $BackupRoot = "/var/backups/opsmemory/action_tracker" }

$Spark2Target = $env:BACKUP_SPARK2_TARGET   # may be empty
$RetentionDays = 14
if ($env:BACKUP_RETENTION_DAYS) { $RetentionDays = [int]$env:BACKUP_RETENTION_DAYS }

$StatusFile = $env:BACKUP_STATUS_FILE
if (-not $StatusFile) { $StatusFile = "/var/lib/opsmemory/backup/status.json" }

# ---- Compute paths ------------------------------------------------------
$StartedAt = Get-Date
$Year     = $StartedAt.ToString("yyyy")
$Month    = $StartedAt.ToString("MM")
$Stamp    = $StartedAt.ToString("yyyyMMdd-HHmmss")
$BackupDir = Join-Path -Path $BackupRoot -ChildPath "$Year/$Month"
$DumpFile  = Join-Path -Path $BackupDir -ChildPath "action_tracker-$Stamp.dump"

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null

$StatusDir = Split-Path -Parent $StatusFile
New-Item -ItemType Directory -Path $StatusDir -Force | Out-Null

# ---- pg_dump (via docker exec — no host postgres-client needed) ---------
# We connect to the existing postgres container via Unix socket inside the
# container (peer auth, no password). bash handles the binary stdout
# redirect since PowerShell's pipeline mangles binary streams.
$ContainerName = $env:POSTGRES_CONTAINER
if (-not $ContainerName) { $ContainerName = "postgres" }

$DbRole = $env:ACTION_TRACKER_DB_ROLE
if (-not $DbRole) { $DbRole = "opsmemory_owner" }

$DbName = $env:ACTION_TRACKER_DB_NAME
if (-not $DbName) { $DbName = "action_tracker" }

# GPG encryption settings (chunk1.5 step 5).
$GpgEnabled = ($env:GPG_ENABLED -eq "true")
$GpgRecipient = $env:BACKUP_GPG_RECIPIENT
$GpgPublicKeyFile = $env:BACKUP_GPG_PUBLIC_KEY_FILE
if (-not $GpgPublicKeyFile) {
    $GpgPublicKeyFile = "/opt/opsmemory/infra/keys/backup-public.asc"
}

if ($GpgEnabled -and -not $GpgRecipient) {
    Write-Error "GPG_ENABLED=true but BACKUP_GPG_RECIPIENT not set"
    exit 1
}

# Final filename depends on whether we encrypt.
$FinalFile = if ($GpgEnabled) { "$DumpFile.gpg" } else { $DumpFile }

# Atomic publish pattern: write to <name>.partial first, verify with
# pg_restore --list, then optionally GPG-encrypt, then rename to the final
# filename. Means restore-check never picks up a half-written or
# half-encrypted dump.
$PartialFile = "$DumpFile.partial"
$EncryptedPartial = "$FinalFile.partial"

Write-Host "[$(Get-Date -Format o)] pg_dump (docker exec $ContainerName) -> $PartialFile"

try {
    & bash -c "docker exec -i $ContainerName pg_dump -U $DbRole -d $DbName --format custom --compress 9 --no-owner --no-acl > '$PartialFile'"

    if ($LASTEXITCODE -ne 0) {
        Send-FailureAlert "pg_dump_failed" "exit=$LASTEXITCODE file=$PartialFile"
        Write-Error "pg_dump failed (exit $LASTEXITCODE)"
        exit 2
    }

    $PartialSize = (Get-Item $PartialFile).Length
    Write-Host "[$(Get-Date -Format o)] pg_dump produced $([math]::Round($PartialSize/1MB, 2)) MB; verifying"

    # ---- Verify dump is parseable -----------------------------------------
    & bash -c "docker exec -i $ContainerName pg_restore --list < '$PartialFile' > /dev/null"
    if ($LASTEXITCODE -ne 0) {
        Send-FailureAlert "pg_restore_list_failed" "partial dump corrupt: $PartialFile"
        Write-Error "pg_restore --list failed — dump may be corrupt"
        exit 3
    }

    if ($GpgEnabled) {
        # ---- GPG encrypt before publishing ---------------------------------
        # Use a temp GNUPGHOME so we don't mutate the operator's main keyring
        # and so encryption only sees the OpsMemory backup public key.
        Write-Host "[$(Get-Date -Format o)] gpg --encrypt (recipient $GpgRecipient)"
        if (-not (Test-Path $GpgPublicKeyFile)) {
            Send-FailureAlert "gpg_pubkey_missing" "expected at $GpgPublicKeyFile"
            Write-Error "GPG public key file not found: $GpgPublicKeyFile"
            exit 6
        }
        $TmpGpgHome = "/tmp/opsmemory-gpg-$(Get-Random)"
        New-Item -ItemType Directory -Path $TmpGpgHome -Force | Out-Null
        try {
            $env:GNUPGHOME = $TmpGpgHome
            & gpg --batch --quiet --import $GpgPublicKeyFile
            if ($LASTEXITCODE -ne 0) { throw "gpg --import failed" }

            & gpg --batch --yes --quiet --trust-model always `
                  --recipient $GpgRecipient `
                  --output $EncryptedPartial `
                  --encrypt $PartialFile
            if ($LASTEXITCODE -ne 0) { throw "gpg --encrypt failed" }
        } finally {
            Remove-Item Env:GNUPGHOME -ErrorAction SilentlyContinue
            if (Test-Path $TmpGpgHome) { Remove-Item -Recurse -Force $TmpGpgHome }
        }
        Remove-Item -Path $PartialFile -Force
        $PartialFile = $EncryptedPartial
        $EncryptedSize = (Get-Item $PartialFile).Length
        Write-Host "[$(Get-Date -Format o)] gpg encrypted: $([math]::Round($EncryptedSize/1MB, 2)) MB"
    }

    # ---- Atomic rename: .partial → final filename ------------------------
    # Move-Item on the same filesystem is atomic on Linux (rename(2)).
    Move-Item -Path $PartialFile -Destination $FinalFile
} finally {
    # Clean up any partial files if anything failed before the atomic rename.
    foreach ($p in @($PartialFile, $EncryptedPartial)) {
        if (Test-Path $p) {
            Write-Host "Cleaning up incomplete partial: $p"
            Remove-Item -Path $p -Force -ErrorAction SilentlyContinue
        }
    }
}

$DumpSize = (Get-Item $FinalFile).Length
$DumpFile = $FinalFile  # downstream code (rsync, status, retention) uses $DumpFile
Write-Host "[$(Get-Date -Format o)] backup file ready: $FinalFile ($([math]::Round($DumpSize/1MB, 2)) MB)$(if ($GpgEnabled) { ' [encrypted]' })"

# ---- Optional: rsync to Spark #2 ---------------------------------------
# rsync default behavior writes to a hidden temp name and renames after
# successful transfer. Removing --partial avoids leaving incomplete remote
# files on connection drops, so the receiver only ever sees complete dumps.
$Rsynced = $false
if ($Spark2Target) {
    Write-Host "[$(Get-Date -Format o)] rsync -> $Spark2Target"
    & rsync -av $DumpFile "${Spark2Target}/"
    if ($LASTEXITCODE -ne 0) {
        Send-FailureAlert "rsync_failed" "target=$Spark2Target exit=$LASTEXITCODE"
        Write-Error "rsync to Spark #2 failed (exit $LASTEXITCODE)"
        exit 4
    }
    $Rsynced = $true
} else {
    Write-Host "BACKUP_SPARK2_TARGET not set — skipping rsync"
}

# ---- Optional: Backblaze B2 offsite upload (Chunk 1.5 step 6) ----------
# Uses `rclone copy` (NOT sync — sync deletes destination files; copy is
# additive). No persistent rclone.conf — credentials passed via flags so
# no secrets land on disk outside .env.
#
# Bucket retention is configured at the B2 console (lifecycle rules) —
# the script just uploads. Local prune still happens via BACKUP_RETENTION_DAYS.
$B2Enabled = ($env:B2_ENABLED -eq "true")
$B2Uploaded = $false
$B2RemotePath = ""
if ($B2Enabled) {
    $B2Bucket = $env:B2_BUCKET
    $B2KeyId = $env:B2_KEY_ID
    $B2AppKey = $env:B2_APPLICATION_KEY
    if (-not $B2Bucket -or -not $B2KeyId -or -not $B2AppKey) {
        Send-FailureAlert "b2_config_missing" "B2_ENABLED=true but bucket/key incomplete"
        Write-Error "B2_ENABLED=true requires B2_BUCKET + B2_KEY_ID + B2_APPLICATION_KEY"
        exit 8
    }
    if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
        Send-FailureAlert "b2_rclone_missing" "rclone not found in PATH"
        Write-Error "rclone not found — install via 'sudo curl https://rclone.org/install.sh | sudo bash'"
        exit 8
    }
    # Path layout: bucket/action_tracker/YYYY/MM/<filename>. Uploading the
    # full backup tree below this prefix means rclone copy can pick up any
    # new files (including historical ones rsynced from Spark #2).
    $B2RemotePath = "${B2Bucket}/action_tracker"
    Write-Host "[$(Get-Date -Format o)] rclone copy -> b2:$B2RemotePath/"
    & rclone --config /dev/null `
             --b2-account $B2KeyId `
             --b2-key $B2AppKey `
             --b2-hard-delete `
             --transfers 4 `
             --b2-chunk-size 96M `
             copy $BackupRoot ":b2:$B2RemotePath/"
    if ($LASTEXITCODE -ne 0) {
        Send-FailureAlert "b2_upload_failed" "exit=$LASTEXITCODE bucket=$B2Bucket"
        Write-Error "rclone copy to B2 failed (exit $LASTEXITCODE)"
        exit 8
    }
    $B2Uploaded = $true
    Write-Host "[$(Get-Date -Format o)] B2 upload complete"
} else {
    Write-Host "B2_ENABLED!=true — skipping offsite upload"
}

# ---- Retention prune (local only) --------------------------------------
# Match both .dump (plaintext) and .dump.gpg (encrypted) so retention works
# regardless of GPG_ENABLED toggling between runs.
$CutoffDate = $StartedAt.AddDays(-$RetentionDays)
$Pruned = 0
Get-ChildItem -Path $BackupRoot -Recurse -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.Name -like "*.dump" -or $_.Name -like "*.dump.gpg") -and
        $_.LastWriteTime -lt $CutoffDate
    } |
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
# completed_at is captured AFTER all work succeeds so /readyz freshness
# reflects actual completion, not start time.
$CompletedAt = Get-Date
$Status = [ordered]@{
    started_at        = $StartedAt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    completed_at      = $CompletedAt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    duration_seconds  = [int]($CompletedAt - $StartedAt).TotalSeconds
    dump_path         = $DumpFile
    dump_bytes        = $DumpSize
    spark2_target     = $Spark2Target
    spark2_synced     = $Rsynced
    retention_days    = $RetentionDays
    pruned_count      = $Pruned
    encrypted         = [bool]$GpgEnabled
    gpg_recipient     = if ($GpgEnabled) { $GpgRecipient } else { "" }
    offsite           = [bool]$B2Uploaded
    b2_remote_path    = if ($B2Uploaded) { $B2RemotePath } else { "" }
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
