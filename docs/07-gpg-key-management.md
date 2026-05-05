# GPG key management for OpsMemory backups

This doc explains the lifecycle of the GPG keypair that encrypts
`action_tracker` backups before they leave Spark #1's home network.

## Threat model

Encryption protects backups from:

- A compromise of **Spark #2** (which holds replicated `.dump.gpg` files
  but not the private key, in the recommended setup).
- A compromise of **Backblaze B2** offsite storage (Chunk 1.5 step 6).
- Theft of the Spark hardware where dumps live.

Encryption does NOT protect against:

- A compromise of Spark #1 itself, where the live database lives in
  cleartext under the postgres container's data dir. If Spark #1 is
  compromised, recent backups are no defense.
- Loss of the private key (no key, no recovery — keep multiple secure
  copies).

## Generation

Run once to create the keypair:

```bash
bash scripts/gpg_init.sh
```

Generates:

- `infra/keys/backup-public.asc` (committed to git)
- `/tmp/opsmemory-gpg-private.asc` (mode 0600, transient)

The script is idempotent at the FS level but every run produces a
**new keypair**. Don't re-run unless you intend to rotate.

## Distribution

Per Codex's review and the project's threat model:

| Location | Has private key? | Why |
|---|---|---|
| **Kyle's password manager** (1Password / Bitwarden / Apple Keychain) | YES | Primary recovery key. Travels with operator. |
| **Spark #2** | YES | Automated weekly restore-check needs to decrypt. Set up in Chunk 1.5 step 7 (restore-from-anywhere). |
| **Offline (paper, fire safe)** | YES | Worst-case recovery: laptop wiped + Spark #2 destroyed. Print the key on paper, store with insurance docs. |
| **Spark #1** | NO | The encrypting machine should NEVER hold the private key. Compromise of Spark #1 must not give attackers decryption capability for past backups. |
| **Operator laptop disk (outside password manager)** | NO | Don't leave private-key files on disk. PM-only. |

## Operator workflow after `gpg_init.sh`

1. Open `/tmp/opsmemory-gpg-private.asc` in a text editor.
2. Copy the entire `-----BEGIN PGP PRIVATE KEY BLOCK-----` ... `-----END PGP PRIVATE KEY BLOCK-----` content (including those headers).
3. Paste into your password manager under a clearly-labeled entry:
   - Title: `OpsMemory Backup GPG Private Key`
   - Notes: include the fingerprint printed by `gpg_init.sh`
4. Verify the entry was saved. Open it. Confirm the content begins with `-----BEGIN PGP PRIVATE KEY BLOCK-----`.
5. **Shred the temp file** (don't `rm` — that leaves recoverable disk fragments):
   ```bash
   shred -u /tmp/opsmemory-gpg-private.asc        # Linux
   rm -P /tmp/opsmemory-gpg-private.asc           # macOS
   sdelete /tmp/opsmemory-gpg-private.asc         # Windows (with sdelete)
   ```
6. Commit the public key:
   ```bash
   git add infra/keys/backup-public.asc
   git commit -m "chunk1.5: add backup-encryption GPG public key"
   git push
   ```
7. On Spark, pull and enable:
   ```bash
   ssh tolson@<spark1>
   cd /opt/opsmemory
   git pull
   sed -i 's|^GPG_ENABLED=.*|GPG_ENABLED=true|' .env
   sudo systemctl start opsmemory-backup.service
   sleep 5
   ls /var/backups/opsmemory/action_tracker/$(date +%Y)/$(date +%m)/
   ```
   You should see a new `*.dump.gpg` file (encrypted) instead of `*.dump`.

## Restore-check behavior on encrypted dumps

`scripts/restore_check.ps1` detects whether the latest dump is `.dump`
or `.dump.gpg` and adapts:

- **`.dump`** (plaintext): full restore-check as before.
- **`.dump.gpg`** + private key present: decrypt to a temp file (mode
  0600), restore to the test DB, run smoke checks, drop the test DB,
  shred the temp file.
- **`.dump.gpg`** + no private key on this machine: structure-only
  verification via `gpg --list-packets`. Confirms the file is a
  well-formed GPG envelope (not corrupted) but doesn't decrypt the body.
  `restore_status.json` records `verification_mode: encrypted-structure-only`.
  Spark #1 will run in this mode after GPG is enabled — full
  restore-check will only fully verify on Spark #2 once that's set up.

## Rotation

Keys expire 5 years from generation. Rotation should happen at year 4:

1. Generate new keypair via `gpg_init.sh`.
2. Update `.env` on Spark with the new recipient (the old one is no
   longer in `infra/keys/backup-public.asc`).
3. Old encrypted dumps in `/var/backups/opsmemory/...` are still
   decryptable with the **old private key**. Keep the old private key
   in your password manager until the retention horizon (90 days)
   passes.
4. After 90 days, all retained backups have been re-encrypted with the
   new key. Mark the old PM entry "RETIRED" but don't delete it for at
   least another 30 days as a safety margin.

## Recovery without the laptop password manager

If your laptop is gone and you need to restore from backups:

1. Borrow a clean machine.
2. Use the offline paper backup of the private key. Type or scan it
   back into a `.asc` file.
3. Import: `gpg --import recovered-private-key.asc`.
4. Decrypt a recent backup: `gpg --output action_tracker.dump --decrypt action_tracker-YYYYMMDD-HHMMSS.dump.gpg`.
5. Restore: `pg_restore -d action_tracker action_tracker.dump`.

Test this drill at least once a year.

## Compromise scenarios

- **Private key leaked**: rotate immediately (new keypair, all future
  backups use it). Old encrypted backups remain decryptable by anyone
  who has the leaked key, until those backups age out of retention.
  Treat any data in those backups as compromised for the retention
  horizon.
- **Public key on Spark #1 replaced by attacker**: backups would be
  encrypted with the attacker's key, making them undecryptable by the
  legitimate operator. Detection: include `gpg --fingerprint` output in
  `backup_status.json` and verify against a known-good fingerprint on
  every restore-check.
- **Both Sparks compromised + private key on Spark #2 stolen**: rotate
  immediately. Treat all retained backups as compromised. Restore from
  paper backup to a clean machine.
