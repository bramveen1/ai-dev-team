#!/bin/bash
set -e

# Ensure the claude user owns its config directory.
# This handles two cases:
#   1. Named volume mounted fresh (root-owned)
#   2. Login performed as root via container terminal
mkdir -p /home/claude/.claude

# Restore .claude.json from backup if missing (Claude CLI creates backups in the volume)
if [ ! -f /home/claude/.claude.json ]; then
    BACKUP=$(ls -t /home/claude/.claude/backups/.claude.json.backup.* 2>/dev/null | head -1)
    if [ -n "$BACKUP" ]; then
        cp "$BACKUP" /home/claude/.claude.json
    fi
fi

chown -R claude:claude /home/claude/.claude /home/claude/.claude.json 2>/dev/null || true

# Explicit auth-mode dispatch on CLAUDE_AUTH_MODE.
# Valid values: oauth_token | api_key | credentials (default when unset).
# Each mode exports only its own secret and actively unsets the others so the
# CLI cannot accidentally pick a stale credential from a different mode.
_AUTH_MODE="${CLAUDE_AUTH_MODE:-credentials}"
case "${_AUTH_MODE}" in
    oauth_token)
        if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
            echo "ERROR: CLAUDE_AUTH_MODE=oauth_token requires CLAUDE_CODE_OAUTH_TOKEN to be set." >&2
            exit 1
        fi
        export CLAUDE_CODE_OAUTH_TOKEN
        unset ANTHROPIC_API_KEY
        ;;
    api_key)
        if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
            echo "ERROR: CLAUDE_AUTH_MODE=api_key requires ANTHROPIC_API_KEY to be set." >&2
            exit 1
        fi
        export ANTHROPIC_API_KEY
        unset CLAUDE_CODE_OAUTH_TOKEN
        ;;
    credentials)
        if [ ! -f /home/claude/.claude/.credentials.json ]; then
            echo "WARNING: CLAUDE_AUTH_MODE=credentials but no .credentials.json found." >&2
            echo "Run:  docker exec -it $(hostname) claude auth login --claudeai" >&2
            echo "Credentials persist in the named Docker volume for this agent." >&2
        fi
        unset ANTHROPIC_API_KEY
        unset CLAUDE_CODE_OAUTH_TOKEN
        ;;
    *)
        echo "ERROR: Invalid CLAUDE_AUTH_MODE='${_AUTH_MODE}'. Valid values: oauth_token | api_key | credentials." >&2
        exit 1
        ;;
esac

# Wire GitHub machine-user identity if a per-persona PAT is available.
# The persona is derived from the container hostname (matches docker container_name).
# Non-comment, non-empty lines in the token file are treated as the PAT.
_PERSONA=$(hostname)
_AIDT_TOKEN_FILE="/config/secrets/gh-aidt-${_PERSONA}.token"
if [ -f "${_AIDT_TOKEN_FILE}" ]; then
    _AIDT_TOKEN=$(grep -v '^#' "${_AIDT_TOKEN_FILE}" | tr -d '[:space:]' | head -c 512)
    if [ -n "${_AIDT_TOKEN}" ]; then
        export GH_TOKEN="${_AIDT_TOKEN}"
        export GITHUB_TOKEN="${_AIDT_TOKEN}"
        # Capitalise the first letter for the display name (e.g. sam → Sam).
        _PERSONA_NAME=$(printf '%s' "${_PERSONA}" | awk '{print toupper(substr($0,1,1)) tolower(substr($0,2))}')
        gosu claude git config --global user.name "${_PERSONA_NAME} (aidt-${_PERSONA})"
        gosu claude git config --global user.email "aidt-${_PERSONA}@users.noreply.github.com"
        # Fail loudly if the token is stale or invalid.
        if ! gosu claude gh auth status 2>&1; then
            echo "WARNING: gh auth status failed for aidt-${_PERSONA} — token may be expired or invalid."
        fi
    else
        echo "INFO: ${_AIDT_TOKEN_FILE} contains only comments/placeholders; skipping GitHub identity wiring."
    fi
fi

# Issue #307: retire the legacy per-issue scratch path that caused three
# state-bleed incidents. Workers now use $DISPATCH_REPO instead.
rm -rf /tmp/sam-scratch

# #339 / #691: dispatch workspaces moved from a named volume to a repo-relative
# host bind mount (./var/dispatch). Docker creates a missing bind source as
# root:root, but Sam runs dispatches as claude (uid 1000) via gosu below, so
# the very first dispatch could not create its workspace. Unconditionally mkdir
# + chown so the directory is always present and owned by claude even if the
# bind source was never provisioned on the host (the #691 footgun).
mkdir -p /var/lib/dispatch
chown claude:claude /var/lib/dispatch 2>/dev/null || true
chmod 0755 /var/lib/dispatch 2>/dev/null || true

# Drop to claude user and execute the provided command
exec gosu claude "$@"
