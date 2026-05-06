# Backblaze B2 offsite setup for OpsMemory backups

This is the third copy of the 3-2-1 backup rule (local + Spark #2 + B2
offsite). B2 is cheap (~$1-2/month at OpsMemory's data volume), private,
and survives any home/office disaster.

The OpsMemory backup script (`scripts/backup_action_tracker.ps1`)
already supports B2 via `rclone copy`. It stays dormant until you set
`B2_ENABLED=true` plus three credential vars. This doc walks through
the operator-side B2 setup.

## 1. Create a Backblaze account (if you don't have one)

https://www.backblaze.com/b2/sign-up.html

- Free tier covers up to 10 GB stored, 1 GB/day download. OpsMemory
  backups will be well under that.
- Add a payment method to allow growth past the free tier (you won't
  exceed it for years at OpsMemory's volume).

## 2. Create a private bucket

In the B2 console:

1. **Buckets** → **Create a Bucket**
2. **Bucket Unique Name**: something opaque — `kyleconway-opsmemory-backups-<random>`. Bucket names are GLOBAL across all of B2 (so `opsmemory-backups` is probably taken). Use a unique suffix.
3. **Files in Bucket are**: **Private** (DO NOT pick public — these are encrypted dumps but no need to expose).
4. **Default Encryption**: **Disable** (the dumps are already GPG-encrypted at rest).
5. **Object Lock**: **Disable** for now. (Object Lock prevents deletion; could revisit for compliance scenarios.)
6. Save. Note the **Bucket Name** — goes in `B2_BUCKET=...`.

## 3. Set bucket lifecycle rules

Per Codex's recommendation: keep 90 daily snapshots + 12 monthly.

In the bucket settings → **Lifecycle Settings**:

- **Keep all versions of the file**: NO (we don't keep multiple versions per dump filename — each dump has a unique timestamp).
- **Keep prior versions for this number of days**: 0 (irrelevant since each dump filename is unique).
- **Keep only the last version of the file**: Yes.

For lifecycle TTL: B2 doesn't have built-in "keep N days then delete" UI in the simple flow, but you can use the **Custom Lifecycle Rules** (B2 Cloud Storage advanced) or rclone-side cleanup. Simplest first pass: leave indefinite retention and revisit cleanup at year 1 (the bucket will be a few GB by then — affordable).

If you want explicit cleanup, run periodically (Chunk 1.5+ feature):
```bash
rclone --config /dev/null \
       --b2-account "$B2_KEY_ID" \
       --b2-key "$B2_APPLICATION_KEY" \
       --min-age 90d \
       delete ":b2:$B2_BUCKET/action_tracker/"
```

## 4. Create a bucket-scoped Application Key

NOT a master key. Bucket-scoped Application Keys can only access the
single bucket — limit blast radius if the key is leaked.

In the B2 console:

1. **App Keys** → **Add a New Application Key**
2. **Name of Key**: `opsmemory-spark1-backup-write`
3. **Allow access to Bucket(s)**: select the bucket you just created (NOT "All").
4. **Type of Access**: **Read and Write**.
5. **Allow List All Bucket Names**: NO.
6. **File name prefix**: leave empty (writes anywhere within the bucket).
7. **Duration**: 0 (no expiry — rotate manually).
8. Click **Create New Key**.
9. **Copy the keyID and applicationKey IMMEDIATELY** — applicationKey is shown once and never again. Store both in your password manager under "OpsMemory B2 Backup Key".

## 5. Wire credentials into Spark `.env`

On Spark:

```bash
cd /opt/opsmemory

# Append B2 vars if missing (idempotent — runs once on first activation)
grep -q '^B2_ENABLED=' .env || echo 'B2_ENABLED=true' >> .env
grep -q '^B2_BUCKET=' .env || echo 'B2_BUCKET=kyleconway-opsmemory-backups-XXXX' >> .env
grep -q '^B2_KEY_ID=' .env || echo 'B2_KEY_ID=YOUR_KEY_ID' >> .env
grep -q '^B2_APPLICATION_KEY=' .env || echo 'B2_APPLICATION_KEY=YOUR_APP_KEY' >> .env

# Substitute the placeholders with real values
sed -i 's|^B2_BUCKET=.*|B2_BUCKET=<actual-bucket-name>|' .env
sed -i 's|^B2_KEY_ID=.*|B2_KEY_ID=<actual-key-id>|' .env
sed -i 's|^B2_APPLICATION_KEY=.*|B2_APPLICATION_KEY=<actual-app-key>|' .env
sed -i 's|^B2_ENABLED=.*|B2_ENABLED=true|' .env

# sed -i resets file group to tolson — restore so opsmemory can read.
sudo chgrp opsmemory .env
sudo chmod 0640 .env
grep '^B2_' .env
```

## 6. Install rclone (if not already on Spark)

```bash
which rclone || sudo curl https://rclone.org/install.sh | sudo bash
rclone version
```

## 7. Verify the upload path

```bash
sudo systemctl start opsmemory-backup.service
sleep 10
sudo journalctl -u opsmemory-backup.service --since "30 seconds ago" --no-pager | tail -25
sudo cat /var/lib/opsmemory/backup/status.json | python3 -m json.tool
```

Expected: a new line `[timestamp] rclone copy -> b2:<bucket>/action_tracker/`, a new line `[timestamp] B2 upload complete`, and status JSON shows `"offsite": true`, `"b2_remote_path": "<bucket>/action_tracker"`.

## 8. Rotation

Rotate the bucket key annually OR on any leak suspicion:

1. Create a new Application Key with the same scope (read+write, bucket-only).
2. Update the four `.env` vars on Spark.
3. Restart `opsmemory-backup.service` and verify a successful run.
4. Delete the old key in the B2 console.
5. Update the password manager entry with the new keyID/key.

## Cost expectations

OpsMemory backup volume estimate:
- 1 backup per day × 6 KB (current encrypted size) × 365 = ~2 MB/year of new dump data
- Even at 10x growth (60 KB per dump → ~22 MB/year), well under B2's free tier 10 GB
- Egress is free for Cloudflare-fronted reads, but we won't be downloading the bucket regularly
- Expected cost: $0.00–$0.50/month for the foreseeable future

## Out of scope for this doc

- B2 client-side encryption (we use GPG; B2's own encryption isn't needed)
- Cross-region replication (B2 is single-region; if needed, add second provider in 1.5+)
- B2 → S3 mirror (overkill for OpsMemory volume)
