#!/bin/bash
set -e

# Validate that an authentication method is configured
if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ] && [ -z "$ANTHROPIC_AUTH_TOKEN" ]; then
    echo "WARNING: No authentication configured."
    echo "Set one of: ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_AUTH_TOKEN"
fi

# Execute the provided command
exec "$@"
