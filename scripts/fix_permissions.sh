#!/bin/bash
# Reset ownership and modes on agent memory trees so the curator (uid 1000)
# and agent containers (also uid 1000) can read and write their own notes.
#
# Mirrors what router/entrypoint.sh does on container start, but exposed as
# a manual command for hosts that ran the old root-curator before the fix
# in issue #116, or whenever drift is suspected.
#
# Usage:
#   scripts/fix_permissions.sh [config_dir]
#   make fix-permissions
#
# Re-run any time. Idempotent. Needs sudo if the files are root-owned.
set -e

CONFIG_DIR="${1:-config}"
CLAUDE_UID=1000
CLAUDE_GID=1000

if [ ! -d "$CONFIG_DIR/agents" ]; then
    echo "no agents dir at $CONFIG_DIR/agents — nothing to do" >&2
    exit 0
fi

# Find a chown invocation that works as the current user. If we aren't
# root and any target file is root-owned, chown will fail — re-exec
# under sudo in that case.
need_sudo() {
    [ "$(id -u)" -ne 0 ] && find "$CONFIG_DIR/agents" -path '*/memory*' \
        \( -not -user "$CLAUDE_UID" -o -not -group "$CLAUDE_GID" \) \
        -print -quit 2>/dev/null | grep -q .
}

if need_sudo; then
    if command -v sudo >/dev/null 2>&1; then
        echo "need root to chown root-owned files — re-running under sudo"
        exec sudo "$0" "$@"
    fi
    echo "warning: some files are not owned by uid $CLAUDE_UID and no sudo available;" >&2
    echo "         continuing on a best-effort basis" >&2
fi

fixed=0
for memory_dir in "$CONFIG_DIR"/agents/*/memory; do
    [ -d "$memory_dir" ] || continue
    chown -R "${CLAUDE_UID}:${CLAUDE_GID}" "$memory_dir" 2>/dev/null || true
    find "$memory_dir" -type d -exec chmod 0700 {} + 2>/dev/null || true
    find "$memory_dir" -type f -exec chmod 0600 {} + 2>/dev/null || true
    echo "fixed $memory_dir"
    fixed=$((fixed + 1))
done

if [ "$fixed" -eq 0 ]; then
    echo "no agent memory dirs found under $CONFIG_DIR/agents"
fi
