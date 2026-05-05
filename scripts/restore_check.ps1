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

# ---- Find most recent dump ---------------------------------------------
$LatestDump = Get-ChildItem -Path $BackupRoot -Recurse -Filter "*.dump" -ErrorAction SilentlyContinue |
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
Write-Host "[$(Get-Date -Format o)] using dump $($LatestDump.FullName) (age $DumpAgeHours h)"

# ---- Drop & create restore DB ------------------------------------------
function Run-AdminSql($Sql) {
    & psql $AdminUrl -v ON_ERROR_STOP=1 -c $Sql 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "admin psql failed: $Sql"
    }
}

try {
    Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb"
    Run-AdminSql "CREATE DATABASE $RestoreDb"
} catch {
    Send-FailureAlert "create_db_failed" $_.Exception.Message
    Write-Error $_.Exception.Message
    exit 3
}

# Build restore connection URL by swapping the DB name.
# Use [System.UriBuilder] for robust DSN parsing — the previous regex broke
# on edge cases (no path component, trailing slash, query containing slashes).
function Build-RestoreUrl([string]$AdminUrl, [string]$RestoreDbName) {
    try {
        $Builder = [System.UriBuilder]::new($AdminUrl)
    } catch {
        throw "RESTORE_TEST_ADMIN_URL is not a valid URI: $AdminUrl"
    }
    if ($RestoreDbName -notmatch '^[a-zA-Z][a-zA-Z0-9_]+$') {
        throw "RestoreDbName failed identifier check: $RestoreDbName"
    }
    $Builder.Path = "/$RestoreDbName"
    return $Builder.Uri.AbsoluteUri
}

$RestoreUrl = Build-RestoreUrl $AdminUrl $RestoreDb

# ---- pg_restore ---------------------------------------------------------
Write-Host "[$(Get-Date -Format o)] pg_restore -> $RestoreDb"
& pg_restore --no-owner --no-acl --dbname $RestoreUrl $LatestDump.FullName

if ($LASTEXITCODE -ne 0) {
    Send-FailureAlert "pg_restore_failed" "exit=$LASTEXITCODE dump=$($LatestDump.FullName)"
    if (-not $KeepDb) { try { Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb" } catch { } }
    Write-Error "pg_restore failed (exit $LASTEXITCODE)"
    exit 4
}

# ---- Smoke checks -------------------------------------------------------
function Run-Sql($Sql) {
    # 0x1f = ASCII Unit Separator. Avoids '|' colliding with JSON pipe content.
    $sep = [char]0x1F
    $output = & psql $RestoreUrl -v ON_ERROR_STOP=1 -At -F $sep -c $Sql 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "smoke psql failed: $Sql -- $($output -join '; ')"
    }
    return $output
}

$smokeChecks = @{}
try {
    $migrationOk = (Run-Sql "SELECT count(*) FROM schema_migrations WHERE version='0001_initial'") -as [int]
    if ($migrationOk -ne 1) { throw "schema_migrations missing 0001_initial" }
    $smokeChecks["schema_migrations_0001_initial"] = "ok"

    $userCount = [int](Run-Sql "SELECT count(*) FROM users")
    if ($userCount -ne 4) { throw "users count expected 4, got $userCount" }
    $smokeChecks["users_count"] = $userCount

    $businessCount = [int](Run-Sql "SELECT count(*) FROM businesses")
    if ($businessCount -ne 2) { throw "businesses count expected 2, got $businessCount" }
    $smokeChecks["businesses_count"] = $businessCount

    $joannaRole = (Run-Sql "SELECT role::text FROM users WHERE email='joanna@borderlinefireworksoutlet.com'") -as [string]
    if ($joannaRole -ne "admin") { throw "Joanna role expected admin, got '$joannaRole'" }
    $smokeChecks["joanna_role"] = $joannaRole

    $kyleRole = (Run-Sql "SELECT role::text FROM users WHERE email='agbusiness.kyle@gmail.com'") -as [string]
    if ($kyleRole -ne "admin") { throw "Kyle role expected admin, got '$kyleRole'" }
    $smokeChecks["kyle_role"] = $kyleRole

    $enumCount = [int](Run-Sql "SELECT count(*) FROM pg_type WHERE typname IN ('task_lifecycle_state','review_lifecycle_state','ingest_lifecycle_state','notification_lifecycle_state','deletion_lifecycle_state')")
    if ($enumCount -ne 5) { throw "expected 5 lifecycle enums, got $enumCount" }
    $smokeChecks["lifecycle_enums"] = $enumCount

    $transitionsTable = (Run-Sql "SELECT to_regclass('public.task_state_transitions')::text") -as [string]
    if (-not $transitionsTable) { throw "task_state_transitions table missing" }
    $smokeChecks["task_state_transitions_table"] = $transitionsTable

    $membershipCount = [int](Run-Sql "SELECT count(*) FROM business_memberships")
    if ($membershipCount -ne 6) { throw "business_memberships count expected 6, got $membershipCount" }
    $smokeChecks["business_memberships_count"] = $membershipCount

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
