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

# Check authentication — either env var or OAuth credentials on disk
if [ -z "$ANTHROPIC_API_KEY" ] && [ ! -f /home/claude/.claude/.credentials.json ]; then
    echo "WARNING: No authentication configured."
    echo "Run:  docker exec -it $(hostname) claude auth login --claudeai"
    echo "Or set ANTHROPIC_API_KEY in your .env file."
fi

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

# Drop to claude user and execute the provided command
exec gosu claude "$@"
