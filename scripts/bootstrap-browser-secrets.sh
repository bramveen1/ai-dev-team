#!/usr/bin/env bash
# Bootstrap the browser_use pack's age keyfile and secrets dir.
#
# Run this ONCE on the host before bringing up the browser-use sidecar.
# Idempotent re-runs do not overwrite an existing keyfile — back up and
# delete the old one first if you want to rotate.
#
# What it does:
#   1. Verifies the `age` and `age-keygen` binaries are on PATH.
#   2. Creates /etc/ai-dev-team/age.key (mode 0400) if missing.
#   3. Creates ./config/browser_profiles/ (mode 0700) if missing.
#   4. Creates ./config/secrets/browser/ (mode 0700) if missing.
#   5. Writes a starter README into the secrets dir with the recipient
#      pubkey so the operator can encrypt their first blob with
#      `age -r <pubkey> -o <name>.env.age secrets.env`.
#
# Usage:
#   scripts/bootstrap-browser-secrets.sh                    # default paths
#   AGE_KEYFILE=/srv/keys/age.key scripts/bootstrap-browser-secrets.sh
#
# Threat model: anyone with read access to the keyfile can decrypt every
# encrypted browser secret on disk. Keep it owned by root (or the
# docker-running user), mode 0400, and don't back it up unencrypted.

set -euo pipefail

KEYFILE="${AGE_KEYFILE:-/etc/ai-dev-team/age.key}"
PROFILES_DIR="${BROWSER_USE_PROFILES_DIR:-./config/browser_profiles}"
SECRETS_DIR="${BROWSER_USE_SECRETS_DIR:-./config/secrets/browser}"

log() { printf '%s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

command -v age >/dev/null 2>&1 || die "\`age\` not on PATH. Install via your package manager (apt: \`age\`; brew: \`age\`)."
command -v age-keygen >/dev/null 2>&1 || die "\`age-keygen\` not on PATH. Install it alongside \`age\`."

# 1. Keyfile.
KEYDIR="$(dirname "$KEYFILE")"
if [ ! -d "$KEYDIR" ]; then
  log "Creating $KEYDIR"
  install -d -m 0700 "$KEYDIR"
fi

if [ -f "$KEYFILE" ]; then
  log "Keyfile already exists at $KEYFILE — refusing to overwrite."
  log "To rotate: back up the old one, run \`rm $KEYFILE\`, re-run this script,"
  log "and re-encrypt every blob under $SECRETS_DIR with the new pubkey."
else
  log "Generating age keyfile at $KEYFILE"
  age-keygen -o "$KEYFILE"
  chmod 0400 "$KEYFILE"
fi

PUBKEY="$(grep '^# public key:' "$KEYFILE" | awk '{print $4}')"
[ -n "$PUBKEY" ] || die "Could not parse pubkey from $KEYFILE"

# 2. Profile dir.
if [ ! -d "$PROFILES_DIR" ]; then
  log "Creating $PROFILES_DIR (mode 0700)"
  install -d -m 0700 "$PROFILES_DIR"
fi

# 3. Secrets dir.
if [ ! -d "$SECRETS_DIR" ]; then
  log "Creating $SECRETS_DIR (mode 0700)"
  install -d -m 0700 "$SECRETS_DIR"
fi

# 4. Starter README so the next encrypter knows what pubkey to target.
README="$SECRETS_DIR/README.txt"
if [ ! -f "$README" ]; then
  cat > "$README" <<EOF
browser_use encrypted secrets bundle
=====================================

Encrypt a per-profile env file:

    age -r $PUBKEY -o linkedin-bram.env.age linkedin-bram.env

After encryption you can shred the plaintext:

    shred -u linkedin-bram.env

The sidecar decrypts each blob in memory at session start. Plaintext
never touches disk inside the container.

This file is safe to keep in the repo (it contains only the pubkey).
The matching private key is at $KEYFILE on the host — do not back
that up unencrypted, do not check it into git, and do not mount it
into any container besides the browser-use sidecar.
EOF
  chmod 0600 "$README"
fi

log ""
log "Done. Recipient pubkey:"
log "    $PUBKEY"
log ""
log "Next: encrypt your first secrets blob, e.g."
log "    age -r $PUBKEY -o $SECRETS_DIR/linkedin-bram.env.age linkedin-bram.env"
log ""
log "Then bring up the sidecar:"
log "    docker compose --profile browser up -d browser-use"
