#!/bin/bash
# Router entrypoint — heal pre-existing root-owned memory files left by
# previous runs that ran the curator as root, then drop privileges to
# ``claude`` (uid 1000) so new files inherit the correct ownership.
#
# See issue #116: agents (running as uid 1000 inside their containers)
# could not write or read their own memory because every curator pass
# re-created files as root.
set -e

CLAUDE_UID=1000
CLAUDE_GID=1000

# 1. Heal historical drift: chown every agent memory tree to claude and
#    apply the expected modes (0700 dirs, 0600 files). Idempotent — runs
#    on every container start but is cheap for the small memory trees.
if [ -d /config/agents ]; then
    for memory_dir in /config/agents/*/memory; do
        [ -d "$memory_dir" ] || continue
        chown -R "${CLAUDE_UID}:${CLAUDE_GID}" "$memory_dir" 2>/dev/null || true
        find "$memory_dir" -type d -exec chmod 0700 {} + 2>/dev/null || true
        find "$memory_dir" -type f -exec chmod 0600 {} + 2>/dev/null || true
    done
fi

# 2. Ensure the SQLite data dir exists and is writable by claude. /app/data
#    is bind-mounted from the host; on Linux the host uid/gid is preserved
#    inside the container, so without this chown a host-owned mount would
#    block the uid-1000 claude user from creating drafts.db etc.
mkdir -p /app/data
chown "${CLAUDE_UID}:${CLAUDE_GID}" /app/data 2>/dev/null || true

# 2b. Ensure the attachments scratch root exists and is writable by claude.
#     /var/lib/attachments is an absolute host bind mount (#326 contract);
#     if Docker auto-creates it before this runs it lands root:root, so the
#     router (uid 1000) can't mkdir per-thread dirs or os.utime them — the
#     same named-volume/bind footgun #116 fixed for /app/data. Mirror it.
mkdir -p /var/lib/attachments
chown "${CLAUDE_UID}:${CLAUDE_GID}" /var/lib/attachments 2>/dev/null || true
chmod 0755 /var/lib/attachments 2>/dev/null || true

# 2c. Ensure the dispatch workspace root exists and is writable by claude.
#     #339 converted /var/lib/dispatch from a named volume to a repo-relative
#     host bind mount (./var/dispatch). Docker auto-creates a missing bind
#     source as root:root, so without this heal the supervision callable
#     (uid 1000) can't write synthetic exitcode/halt_marker — the same
#     named-volume/bind footgun #116 fixed for /app/data. Mirror it.
mkdir -p /var/lib/dispatch
chown "${CLAUDE_UID}:${CLAUDE_GID}" /var/lib/dispatch 2>/dev/null || true
chmod 0755 /var/lib/dispatch 2>/dev/null || true

# 3. Grant the claude user access to the host's docker socket so the
#    dispatcher can still ``docker exec`` into agent containers after we
#    drop privileges. The socket's gid varies between hosts (commonly
#    999 on Debian/Ubuntu, 0 on rootless setups), so we discover it.
if [ -S /var/run/docker.sock ]; then
    DOCKER_SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
    if [ -n "$DOCKER_SOCK_GID" ] && [ "$DOCKER_SOCK_GID" != "0" ]; then
        if ! getent group "$DOCKER_SOCK_GID" >/dev/null; then
            groupadd -g "$DOCKER_SOCK_GID" docker-host
        fi
        DOCKER_GROUP_NAME=$(getent group "$DOCKER_SOCK_GID" | cut -d: -f1)
        usermod -aG "$DOCKER_GROUP_NAME" claude
    else
        # Socket owned by root group — just let claude in via chgrp.
        chgrp claude /var/run/docker.sock 2>/dev/null || true
        chmod g+rw /var/run/docker.sock 2>/dev/null || true
    fi
fi

# 4. Drop to claude and exec the configured command (python -m router.app
#    by default). ``ps``/``docker top`` will show the curator running as
#    uid 1000, which is the visible acceptance criterion in issue #116.
exec gosu claude "$@"
