#!/bin/bash
# Entrypoint for the browser_use sidecar.
#
# Runs as root inside the container, fixes up ownership of the bind-
# mounted private dirs (which inherit ownership from the host and
# therefore never match the sidecar uid on a fresh checkout), then
# drops privileges via ``gosu`` and execs whatever CMD compose passed
# (uvicorn). Keeps tini as PID 1 — the script is run by tini, not
# replacing it, so signal handling is preserved.
#
# Why this exists: see docs/packs/browser_use-entrypoint.md and issue
# #143. The TL;DR is that the image-time ``chown -R sidecar:sidecar
# /config`` in the Dockerfile is masked at runtime when the host
# bind-mounts ``./config/...`` over it. Without this script, first run
# on a fresh checkout always hits ``PermissionError: [Errno 13]`` from
# inside ``helpers/secrets.py``.

set -u

PROFILES_DIR="${BROWSER_USE_PROFILES_DIR:-/config/browser_profiles}"
SECRETS_DIR="${BROWSER_USE_SECRETS_DIR:-/config/secrets/browser}"
SIDECAR_UID="${SIDECAR_UID:-1000}"
SIDECAR_GID="${SIDECAR_GID:-1000}"

log() {
    echo "[entrypoint-browser] $*" >&2
}

# Ensure a private dir exists, is owned by the sidecar user, and is
# mode 0700. Failures here are logged but non-fatal — a read-only
# bind mount can legitimately refuse the chown, in which case the
# sidecar's runtime perms checks will surface the real cause with a
# scoped error message rather than letting the entrypoint crash hide
# it.
ensure_dir() {
    local dir="$1"
    if ! mkdir -p "$dir"; then
        log "warning: could not mkdir $dir (continuing)"
        return 0
    fi
    if ! chown "$SIDECAR_UID:$SIDECAR_GID" "$dir"; then
        log "warning: could not chown $dir to ${SIDECAR_UID}:${SIDECAR_GID} (read-only mount?)"
    fi
    if ! chmod 0700 "$dir"; then
        log "warning: could not chmod 0700 $dir (read-only mount?)"
    fi
}

ensure_dir "$PROFILES_DIR"
ensure_dir "$SECRETS_DIR"

# Encrypted bundles must be readable by uid 1000. We fix ownership
# and mode on each ``*.age`` blob directly under SECRETS_DIR (not
# recursive — there's no nested structure today).
if [ -d "$SECRETS_DIR" ]; then
    shopt -s nullglob
    for blob in "$SECRETS_DIR"/*.age; do
        if ! chown "$SIDECAR_UID:$SIDECAR_GID" "$blob"; then
            log "warning: could not chown $blob (read-only mount?)"
        fi
        if ! chmod 0600 "$blob"; then
            log "warning: could not chmod 0600 $blob (read-only mount?)"
        fi
    done
    shopt -u nullglob
fi

# Deliberately do NOT touch /run/secrets/age.key — Docker manages
# that mount's ownership, and ``assert_keyfile_safe`` in the sidecar
# enforces its mode check at request time.

exec gosu sidecar "$@"
