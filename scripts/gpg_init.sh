#!/usr/bin/env bash
# Generate the OpsMemory backup-encryption GPG keypair.
#
# WHERE TO RUN: ideally your laptop (clean machine). Spark works too — the
# private key exists briefly there during generation, but you immediately
# copy it to your password manager and shred the file.
#
# WHAT IT DOES:
#   1. Generates 4096-bit RSA keypair, no passphrase, 5-year expiry.
#   2. Exports public key to infra/keys/backup-public.asc (commit this).
#   3. Writes private key to /tmp/opsmemory-gpg-private.asc mode 0600.
#      You copy that file's contents into your password manager
#      (1Password / Bitwarden / Apple Keychain), THEN shred the file.
#
# ENVIRONMENT: GPG must be installed (`gpg --version` should work).
#   - Linux/Spark: usually pre-installed via gnupg2 package.
#   - macOS: brew install gnupg.
#   - Windows: install Gpg4win or use Git Bash + gpg.
#
# Re-running: regenerates a fresh keypair. The old public key is
# overwritten. If you've encrypted backups with the old key, you can no
# longer decrypt them with the new key. Rotate carefully.

set -euo pipefail

REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || pwd)
PUBLIC_KEY_PATH="${REPO_ROOT}/infra/keys/backup-public.asc"
PRIVATE_KEY_PATH=${OPSMEMORY_GPG_PRIVATE_OUT:-/tmp/opsmemory-gpg-private.asc}

KEY_NAME="OpsMemory Backup"
KEY_EMAIL="opsmemory-backup@kyleconway.ai"
EXPIRE="5y"

# Use a temporary GNUPGHOME so we don't pollute the operator's main keyring.
GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap "rm -rf $GNUPGHOME" EXIT

if ! command -v gpg >/dev/null 2>&1; then
  echo "ERROR: gpg not found in PATH. Install GnuPG first." >&2
  exit 1
fi

mkdir -p "${REPO_ROOT}/infra/keys"

cat > "$GNUPGHOME/keygen-config" <<KEYCFG
%no-protection
Key-Type: RSA
Key-Length: 4096
Subkey-Type: RSA
Subkey-Length: 4096
Name-Real: $KEY_NAME
Name-Email: $KEY_EMAIL
Expire-Date: $EXPIRE
%commit
KEYCFG

echo "Generating 4096-bit RSA keypair (this can take 30-60 seconds)..."
gpg --batch --gen-key "$GNUPGHOME/keygen-config" 2>&1 | tail -3

# Export public key (committed).
gpg --armor --export "$KEY_EMAIL" > "$PUBLIC_KEY_PATH"
chmod 0644 "$PUBLIC_KEY_PATH"

# Export private key to a tempfile with restrictive permissions.
umask 077
gpg --armor --export-secret-keys "$KEY_EMAIL" > "$PRIVATE_KEY_PATH"
chmod 0600 "$PRIVATE_KEY_PATH"

# Capture fingerprint for the operator handoff.
FPR=$(gpg --with-colons --fingerprint "$KEY_EMAIL" 2>/dev/null \
        | awk -F: '/^fpr:/ {print $10; exit}')

cat <<DONE

================================================================
GPG keypair generated.

Public key  (commit this):
    ${PUBLIC_KEY_PATH}

Private key (DO NOT commit; copy + shred):
    ${PRIVATE_KEY_PATH}

Fingerprint:
    ${FPR}

NEXT STEPS:
  1. Open ${PRIVATE_KEY_PATH} in a text editor.
  2. Copy the entire contents into your password manager
     (1Password / Bitwarden / Apple Keychain) under a clearly-labeled
     entry "OpsMemory Backup GPG Private Key".
  3. Verify the entry was saved.
  4. Shred the private key file:
        shred -u ${PRIVATE_KEY_PATH}
     (On macOS: rm -P; on Windows: sdelete or just rm and accept that
      the data may linger on disk briefly.)
  5. Add the public key to git:
        git add ${PUBLIC_KEY_PATH#${REPO_ROOT}/}
        git commit -m 'chunk1.5: add backup-encryption GPG public key'
        git push
  6. In .env, set:
        BACKUP_GPG_RECIPIENT=${KEY_EMAIL}
        GPG_ENABLED=true
  7. (Optional offsite backup) print the private key to a piece of paper
     in a fire-safe / safety deposit box. Same for password manager
     master password.

================================================================
DONE
