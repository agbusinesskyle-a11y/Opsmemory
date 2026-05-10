#!/usr/bin/env pwsh
# OpsMemory — verify the most recent backup by restoring to a test DB and
# running smoke checks.
#
# --source local    (default) read from $BACKUP_ROOT on the local machine
# --source spark2   rsync from $BACKUP_SPARK2_TARGET, restore from the copy
# --source b2       rclone copy from $B2_BUCKET, restore from the copy
#
# Designed to be run from any machine that has the GPG private key to
# do a full restore. From Spark #1 (no private key by design) it falls
# back to encrypted-structure-only verification when reading
# .dump.gpg files.
#
# Optional env (script reads from environment):
#   POSTGRES_CONTAINER       default 'postgres'
#   ACTION_TRACKER_DB_ROLE   default 'opsmemory_owner'  (unused here; restore_check uses admin)
#   BACKUP_ROOT              default /var/backups/opsmemory/action_tracker
#   RESTORE_TEST_ADMIN_USER  default 'openbrain'
#   RESTORE_TEST_ADMIN_DB    default 'openbrain'
#   RESTORE_TEST_DB          default 'action_tracker_restore_test'
#   KEEP_RESTORE_DB          "true" leaves the restored DB in place for inspection
#   RESTORE_STATUS_FILE      default /var/lib/opsmemory/backup/restore_status.json
#   BACKUP_ALERT_WEBHOOK_URL failure notifications
#   BACKUP_SPARK2_TARGET     rsync target (used when --source spark2)
#   B2_BUCKET, B2_KEY_ID, B2_APPLICATION_KEY  used when --source b2
#
# Exit codes:
#   0  success
#   1  configuration error
#   2  no backup found
#   3  drop/create DB failed
#   4  pg_restore failed
#   5  smoke check failed
#   6  status write failed
#   7  GPG verify/decrypt failed
#   8  remote source fetch failed (--source spark2 / b2)

[CmdletBinding()]
param(
    [ValidateSet("local", "spark2", "b2")]
    [string]$Source = "local"
)

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

# ---- Temp resources: initialize before try, clean up in finally -------
# All three temp resources may be created below. Initializing here
# (before the try/finally that wraps the rest of the script) means the
# finally block can reliably clean up regardless of which exit path
# fired, including PowerShell `exit` calls and unhandled exceptions.
$RemoteFetchTempDir = $null
$DecryptedTempFile = $null
$TmpGpgHome = $null

try {

# ---- Source selection: local, spark2, b2 ------------------------------
# When fetching from remote, we copy the entire backup tree to a temp
# directory and treat that as $BackupRoot for the rest of the script.
$EffectiveBackupRoot = $BackupRoot

if ($Source -eq "spark2") {
    $Spark2Target = $env:BACKUP_SPARK2_TARGET
    if (-not $Spark2Target) {
        Write-Error "Source=spark2 but BACKUP_SPARK2_TARGET is empty"
        exit 1
    }
    $RemoteFetchTempDir = "/tmp/opsmemory-restore-spark2-$(Get-Random)"
    New-Item -ItemType Directory -Path $RemoteFetchTempDir -Force | Out-Null
    Write-Host "[$(Get-Date -Format o)] rsync $Spark2Target -> $RemoteFetchTempDir/"
    & rsync -av "${Spark2Target}/" "$RemoteFetchTempDir/"
    if ($LASTEXITCODE -ne 0) {
        Send-FailureAlert "spark2_fetch_failed" "exit=$LASTEXITCODE target=$Spark2Target"
        Write-Error "rsync from Spark #2 failed (exit $LASTEXITCODE)"
        exit 8
    }
    $EffectiveBackupRoot = $RemoteFetchTempDir
}
elseif ($Source -eq "b2") {
    $B2Bucket = $env:B2_BUCKET
    $B2KeyId = $env:B2_KEY_ID
    $B2AppKey = $env:B2_APPLICATION_KEY
    if (-not $B2Bucket -or -not $B2KeyId -or -not $B2AppKey) {
        Write-Error "Source=b2 requires B2_BUCKET + B2_KEY_ID + B2_APPLICATION_KEY"
        exit 1
    }
    if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
        Write-Error "rclone not found in PATH"
        exit 1
    }
    $RemoteFetchTempDir = "/tmp/opsmemory-restore-b2-$(Get-Random)"
    New-Item -ItemType Directory -Path $RemoteFetchTempDir -Force | Out-Null
    Write-Host "[$(Get-Date -Format o)] rclone copy b2:$B2Bucket/action_tracker/ -> $RemoteFetchTempDir/"
    & rclone --config /dev/null `
             --b2-account $B2KeyId `
             --b2-key $B2AppKey `
             --transfers 4 `
             copy ":b2:$B2Bucket/action_tracker/" $RemoteFetchTempDir
    if ($LASTEXITCODE -ne 0) {
        Send-FailureAlert "b2_fetch_failed" "exit=$LASTEXITCODE bucket=$B2Bucket"
        Write-Error "rclone copy from B2 failed (exit $LASTEXITCODE)"
        exit 8
    }
    $EffectiveBackupRoot = $RemoteFetchTempDir
}

# ---- Find most recent dump (.dump or .dump.gpg) -----------------------
$LatestDump = Get-ChildItem -Path $EffectiveBackupRoot -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "*.dump" -or $_.Name -like "*.dump.gpg" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $LatestDump) {
    $msg = "no backup dump found under $EffectiveBackupRoot (source=$Source)"
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
        # 0600 enforced via umask-equivalent: New-Item with -Force then chmod.
        # PowerShell on Linux honors chmod via filesystem ACLs.
        $DecryptedTempFile = "/tmp/opsmemory-restore-$(Get-Random).dump"
        New-Item -ItemType File -Path $DecryptedTempFile -Force | Out-Null
        & chmod 0600 $DecryptedTempFile
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
        # IMPORTANT: gpg --list-packets exits non-zero when the secret key
        # isn't available (it tries to decrypt and fails) BUT still emits
        # the parsed packet structure on stdout. We can't rely on the exit
        # code; instead capture combined stdout+stderr and look for the
        # expected envelope packet markers. Truncated/non-GPG files won't
        # have these markers.
        # Use a temp GNUPGHOME so we don't need ~/.gnupg under /var/lib/opsmemory.
        $TmpGpgHome = "/tmp/opsmemory-gpg-rcheck-$(Get-Random)"
        New-Item -ItemType Directory -Path $TmpGpgHome -Force | Out-Null
        $env:GNUPGHOME = $TmpGpgHome
        $listOutput = & gpg --list-packets $LatestDump.FullName 2>&1
        $listText = ($listOutput | Out-String)
        $hasPubkeyEnc = $listText -match ':pubkey enc packet:'
        $hasEncrypted = $listText -match ':aead encrypted packet:|:encrypted packet:|:encrypted data packet:'
        if (-not ($hasPubkeyEnc -and $hasEncrypted)) {
            Send-FailureAlert "gpg_list_packets_invalid" "dump=$($LatestDump.FullName)"
            Write-Error "gpg --list-packets did not find PKESK + encrypted-data packets — file may not be a valid GPG envelope"
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
        source            = $Source
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

    # MT-2: platform_admin is the new platform-owner role. At least
    # one active platform_admin must exist or restore is broken.
    $adminCount = [int](Run-Sql "SELECT count(*) FROM users WHERE status = 'active' AND role = 'platform_admin'")
    if ($adminCount -lt 1) { throw "active platform_admin users expected >=1, got $adminCount" }
    $smokeChecks["platform_admin_count"] = $adminCount

    $identityCount = [int](Run-Sql "SELECT count(*) FROM user_identities WHERE provider = 'cloudflare_access'")
    if ($identityCount -lt $userCount) { throw "cloudflare_access identities ($identityCount) < users ($userCount)" }
    $smokeChecks["cloudflare_identities_count"] = $identityCount

    $membershipCount = [int](Run-Sql "SELECT count(*) FROM business_memberships WHERE status = 'active'")
    $smokeChecks["business_memberships_active_count"] = $membershipCount

    Write-Host "[$(Get-Date -Format o)] all smoke checks passed"
} catch {
    Send-FailureAlert "smoke_check_failed" $_.Exception.Message
    if (-not $KeepDb) { try { Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb" } catch { } }
    if ($DecryptedTempFile -and (Test-Path $DecryptedTempFile)) {
        Remove-Item $DecryptedTempFile -Force
    }
    Write-Error $_.Exception.Message
    exit 5
}

# ---- Cleanup ------------------------------------------------------------
if (-not $KeepDb) {
    try { Run-AdminSql "DROP DATABASE IF EXISTS $RestoreDb" } catch { Write-Warning "cleanup drop failed: $($_.Exception.Message)" }
} else {
    Write-Host "KEEP_RESTORE_DB=true — leaving $RestoreDb in place"
}

# Per-success cleanup happens here AND again in the top-level finally
# below. Belt and suspenders — finally is the canonical cleanup; this
# inline block only matters in the success path so the temp files are
# gone before we write the status JSON (smaller window for stale state
# if a future bug causes the script to hang).
if ($DecryptedTempFile -and (Test-Path $DecryptedTempFile)) {
    Remove-Item $DecryptedTempFile -Force
    $DecryptedTempFile = $null
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
    source            = $Source
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

}  # end of top-level try
finally {
    # Canonical cleanup. Runs on success, on every `exit N` path, and
    # on any unhandled exception. Order: GNUPGHOME env var first (so
    # nested gpg invocations stop using the temp dir), then the three
    # temp resources.
    Remove-Item Env:GNUPGHOME -ErrorAction SilentlyContinue
    if ($TmpGpgHome -and (Test-Path $TmpGpgHome)) {
        Remove-Item -Recurse -Force $TmpGpgHome -ErrorAction SilentlyContinue
    }
    if ($DecryptedTempFile -and (Test-Path $DecryptedTempFile)) {
        Remove-Item -Force $DecryptedTempFile -ErrorAction SilentlyContinue
    }
    if ($RemoteFetchTempDir -and (Test-Path $RemoteFetchTempDir)) {
        Remove-Item -Recurse -Force $RemoteFetchTempDir -ErrorAction SilentlyContinue
    }
}
