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

# Drop to claude user and execute the provided command
exec gosu claude "$@"
