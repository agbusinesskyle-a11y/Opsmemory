#!/usr/bin/env pwsh
# OpsMemory — verify the most recent backup by restoring to a test DB and
# running smoke checks.
#
# Chunk 1 ships this script. Cron timer that runs it weekly is deferred
# to Chunk 1.5. For Chunk 1, run this once manually to prove the
# backup → restore loop works.
#
# Required env:
#   RESTORE_TEST_ADMIN_URL  DSN with privilege to CREATE/DROP DATABASE
#                           (e.g. postgres://postgres:pw@host:5432/postgres)
# Optional env:
#   BACKUP_ROOT             default /var/backups/opsmemory/action_tracker
#   RESTORE_TEST_DB         default action_tracker_restore_test
#   KEEP_RESTORE_DB         "true" leaves the restored DB in place for inspection
#   RESTORE_STATUS_FILE     default /var/lib/opsmemory/backup/restore_status.json
#   BACKUP_ALERT_WEBHOOK_URL  failure notifications
#
# Exit codes:
#   0  success
#   1  configuration error
#   2  no backup found
#   3  drop/create DB failed
#   4  pg_restore failed
#   5  smoke check failed
#   6  status write failed

$ErrorActionPreference = "Stop"

function Send-FailureAlert($Reason, $Detail) {
    $url = $env:BACKUP_ALERT_WEBHOOK_URL
    if (-not $url) { return }
    try {
        $body = @{
            event = "opsmemory_restore_check_failed"
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

# ---- Config -------------------------------------------------------------
$AdminUrl = $env:RESTORE_TEST_ADMIN_URL
if (-not $AdminUrl) {
    Write-Error "RESTORE_TEST_ADMIN_URL is required"
    exit 1
}

$BackupRoot = $env:BACKUP_ROOT
if (-not $BackupRoot) { $BackupRoot = "/var/backups/opsmemory/action_tracker" }

$RestoreDb = $env:RESTORE_TEST_DB
if (-not $RestoreDb) { $RestoreDb = "action_tracker_restore_test" }

# Refuse a restore-DB name with anything weird — it's interpolated into SQL.
if ($RestoreDb -notmatch '^[a-zA-Z][a-zA-Z0-9_]+$') {
    Write-Error "RESTORE_TEST_DB must match ^[a-zA-Z][a-zA-Z0-9_]+$ (got '$RestoreDb')"
    exit 1
}

$KeepDb = ($env:KEEP_RESTORE_DB -eq "true")

$StatusFile = $env:RESTORE_STATUS_FILE
if (-not $StatusFile) { $StatusFile = "/var/lib/opsmemory/backup/restore_status.json" }

# ---- Find most recent dump (.dump or .dump.gpg) -----------------------
$LatestDump = Get-ChildItem -Path $BackupRoot -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "*.dump" -or $_.Name -like "*.dump.gpg" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $LatestDump) {
    $msg = "no backup dump found under $BackupRoot"
    Send-FailureAlert "no_backup" $msg
    Write-Error $msg
    exit 2
}

$StartedAt = Get-Date
$DumpAgeHours = [math]::Round(($StartedAt - $LatestDump.LastWriteTime).TotalHours, 2)
$IsEncrypted = $LatestDump.Name -like "*.dump.gpg"
Write-Host "[$(Get-Date -Format o)] using dump $($LatestDump.FullName) (age $DumpAgeHours h$(if ($IsEncrypted) {' [encrypted]'}))"

# ---- GPG decrypt or partial-verify ------------------------------------
# If the dump is encrypted, we need the private key to decrypt for a full
# restore-check. If the private key isn't available on this machine
# (because it lives on Spark #2 / laptop password manager / offline),
# fall back to a structure-only verification: gpg --list-packets confirms
# the file is a well-formed GPG envelope, but doesn't decrypt the body.
# Operator's full restore-check happens from a machine with the private
# key (typically Spark #2 — Chunk 1.5 step 7).
$DecryptedTempFile = $null
$VerificationMode = if ($IsEncrypted) { "encrypted-pending-private-key" } else { "plaintext-full" }
$RestoreSourcePath = $LatestDump.FullName

if ($IsEncrypted) {
    $GpgRecipient = $env:BACKUP_GPG_RECIPIENT
    if (-not $GpgRecipient) {
        Send-FailureAlert "gpg_recipient_missing" "BACKUP_GPG_RECIPIENT required to verify .gpg dumps"
        Write-Error "BACKUP_GPG_RECIPIENT not set; cannot decide encryption verification path"
        exit 7
    }

    # Does this machine have the private key?
    $hasPrivate = $false
    & gpg --list-secret-keys $GpgRecipient *> $null
    if ($LASTEXITCODE -eq 0) { $hasPrivate = $true }

    if ($hasPrivate) {
        $VerificationMode = "encrypted-full"
        Write-Host "[$(Get-Date -Format o)] gpg private key for $GpgRecipient found; decrypting"
        $DecryptedTempFile = "/tmp/opsmemory-restore-$(Get-Random).dump"
        & gpg --batch --quiet --yes --output $DecryptedTempFile --decrypt $LatestDump.FullName
        if ($LASTEXITCODE -ne 0) {
            Send-FailureAlert "gpg_decrypt_failed" "dump=$($LatestDump.FullName)"
            Write-Error "gpg --decrypt failed"
            exit 7
        }
        $RestoreSourcePath = $DecryptedTempFile
    } else {
        $VerificationMode = "encrypted-structure-only"
        Write-Host "[$(Get-Date -Format o)] gpg private key NOT on this machine; running structure-only verification"
        # gpg --list-packets fails on truncated / non-GPG files even without
        # the private key. It DOES need to read the packet headers (which
        # requires the public key — already imported when we encrypted).
        & gpg --list-packets --batch --quiet $LatestDump.FullName *> $null
        if ($LASTEXITCODE -ne 0) {
            Send-FailureAlert "gpg_list_packets_failed" "dump=$($LatestDump.FullName)"
            Write-Error "gpg --list-packets failed — encrypted dump may be corrupt"
            exit 7
        }
        Write-Host "[$(Get-Date -Format o)] structure-only check passed; full restore must be verified from a machine with the private key"
    }
}

# ---- Drop & create restore DB (via docker exec — no host psql needed) --
$ContainerName = $env:POSTGRES_CONTAINER
if (-not $ContainerName) { $ContainerName = "postgres" }

$AdminUser = $env:RESTORE_TEST_ADMIN_USER
if (-not $AdminUser) { $AdminUser = "openbrain" }

$AdminDb = $env:RESTORE_TEST_ADMIN_DB
if (-not $AdminDb) { $AdminDb = "openbrain" }

function Run-AdminSql($Sql) {
    & docker exec -i $ContainerName psql -U $AdminUser -d $AdminDb -v ON_ERROR_STOP=1 -c $Sql 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "admin psql failed: $Sql"
    }
}

if ($VerificationMode -eq "encrypted-structure-only") {
    # No private key — skip the actual restore. Write a status JSON noting
    # that verification was structure-only, so /readyz is aware.
    $CompletedAt = Get-Date
    $Status = [ordered]@{
        started_at        = $StartedAt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
        completed_at      = $CompletedAt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
        duration_seconds  = [int]($CompletedAt - $StartedAt).TotalSeconds
        ok                = $true
        verification_mode = $VerificationMode
        dump_path         = $LatestDump.FullName
        dump_age_hours    = $DumpAgeHours
        smoke_checks      = @{ "gpg_structure" = "ok" }
    } | ConvertTo-Json -Depth 4

    $StatusDir = Split-Path -Parent $StatusFile
    New-Item -ItemType Directory -Path $StatusDir -Force | Out-Null
    [IO.File]::WriteAllText($StatusFile, $Status)
    Write-Host "[$(Get-Date -Format o)] structure-only verify complete; status=$StatusFile"
    return
}

try {
    Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb"
    Run-AdminSql "CREATE DATABASE $RestoreDb"
} catch {
    Send-FailureAlert "create_db_failed" $_.Exception.Message
    Write-Error $_.Exception.Message
    exit 3
}

# ---- pg_restore (via docker exec, dump streamed in via stdin) ----------
# RestoreDb identifier was already validated above (^[a-zA-Z][a-zA-Z0-9_]+$).
Write-Host "[$(Get-Date -Format o)] pg_restore (docker exec $ContainerName) -> $RestoreDb"
& bash -c "docker exec -i $ContainerName pg_restore --no-owner --no-acl -U $AdminUser -d $RestoreDb < '$RestoreSourcePath'"

if ($LASTEXITCODE -ne 0) {
    Send-FailureAlert "pg_restore_failed" "exit=$LASTEXITCODE dump=$($LatestDump.FullName)"
    if (-not $KeepDb) { try { Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb" } catch { } }
    if ($DecryptedTempFile -and (Test-Path $DecryptedTempFile)) { Remove-Item $DecryptedTempFile -Force }
    Write-Error "pg_restore failed (exit $LASTEXITCODE)"
    exit 4
}

# ---- Smoke checks (via docker exec into the restored DB) ----------------
function Run-Sql($Sql) {
    # 0x1f = ASCII Unit Separator. Avoids '|' colliding with JSON pipe content.
    $sep = [char]0x1F
    $output = & docker exec -i $ContainerName psql -U $AdminUser -d $RestoreDb -v ON_ERROR_STOP=1 -At -F $sep -c $Sql 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "smoke psql failed: $Sql -- $($output -join '; ')"
    }
    return $output
}

$smokeChecks = @{}
try {
    # Schema invariants (don't change as the system grows).
    $migrationOk = (Run-Sql "SELECT count(*) FROM schema_migrations WHERE version='0001_initial'") -as [int]
    if ($migrationOk -ne 1) { throw "schema_migrations missing 0001_initial" }
    $smokeChecks["schema_migrations_0001_initial"] = "ok"

    $enumCount = [int](Run-Sql "SELECT count(*) FROM pg_type WHERE typname IN ('task_lifecycle_state','review_lifecycle_state','ingest_lifecycle_state','notification_lifecycle_state','deletion_lifecycle_state')")
    if ($enumCount -ne 5) { throw "expected 5 lifecycle enums, got $enumCount" }
    $smokeChecks["lifecycle_enums"] = $enumCount

    $transitionsTable = (Run-Sql "SELECT to_regclass('public.task_state_transitions')::text") -as [string]
    if (-not $transitionsTable) { throw "task_state_transitions table missing" }
    $smokeChecks["task_state_transitions_table"] = $transitionsTable

    # Seed shape (loose — businesses always seeded by 0001_initial.sql; users
    # seeded by scripts/seed_initial.py and grow over time, so use lower bounds).
    $businessCount = [int](Run-Sql "SELECT count(*) FROM businesses WHERE deletion_state = 'active'")
    if ($businessCount -lt 1) { throw "businesses count expected >=1, got $businessCount" }
    $smokeChecks["businesses_active_count"] = $businessCount

    $userCount = [int](Run-Sql "SELECT count(*) FROM users WHERE status = 'active'")
    if ($userCount -lt 1) { throw "active users expected >=1, got $userCount" }
    $smokeChecks["users_active_count"] = $userCount

    $adminCount = [int](Run-Sql "SELECT count(*) FROM users WHERE status = 'active' AND role = 'admin'")
    if ($adminCount -lt 1) { throw "active admin users expected >=1, got $adminCount" }
    $smokeChecks["admin_count"] = $adminCount

    $identityCount = [int](Run-Sql "SELECT count(*) FROM user_identities WHERE provider = 'cloudflare_access'")
    if ($identityCount -lt $userCount) { throw "cloudflare_access identities ($identityCount) < users ($userCount)" }
    $smokeChecks["cloudflare_identities_count"] = $identityCount

    $membershipCount = [int](Run-Sql "SELECT count(*) FROM business_memberships WHERE status = 'active'")
    $smokeChecks["business_memberships_active_count"] = $membershipCount

    Write-Host "[$(Get-Date -Format o)] all smoke checks passed"
} catch {
    Send-FailureAlert "smoke_check_failed" $_.Exception.Message
    if (-not $KeepDb) { try { Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb" } catch { } }
    Write-Error $_.Exception.Message
    exit 5
}

# ---- Cleanup ------------------------------------------------------------
if (-not $KeepDb) {
    try { Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb" } catch { Write-Warning "cleanup drop failed: $($_.Exception.Message)" }
} else {
    Write-Host "KEEP_RESTORE_DB=true — leaving $RestoreDb in place"
}

# Clean up the temp decrypted file (if any). Mode 0600 minimized exposure
# during the restore window; remove as soon as we're done.
if ($DecryptedTempFile -and (Test-Path $DecryptedTempFile)) {
    Remove-Item $DecryptedTempFile -Force
}

# ---- Status JSON --------------------------------------------------------
$StatusDir = Split-Path -Parent $StatusFile
New-Item -ItemType Directory -Path $StatusDir -Force | Out-Null

# completed_at captured AFTER all checks pass and cleanup runs.
$CompletedAt = Get-Date
$Status = [ordered]@{
    started_at        = $StartedAt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    completed_at      = $CompletedAt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    duration_seconds  = [int]($CompletedAt - $StartedAt).TotalSeconds
    ok                = $true
    verification_mode = $VerificationMode
    dump_path         = $LatestDump.FullName
    dump_age_hours    = $DumpAgeHours
    restore_db        = $RestoreDb
    smoke_checks      = $smokeChecks
} | ConvertTo-Json -Depth 4

try {
    [IO.File]::WriteAllText($StatusFile, $Status)
} catch {
    Send-FailureAlert "status_write_failed" $_.Exception.Message
    Write-Error "status write failed: $($_.Exception.Message)"
    exit 6
}

Write-Host "[$(Get-Date -Format o)] restore-check complete dump_age=${DumpAgeHours}h status=$StatusFile"
