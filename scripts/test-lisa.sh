#!/bin/bash
# test-lisa.sh — Smoke test for the Lisa agent container.
#
# Sends a test message directly to the Lisa container via docker exec
# and verifies that a non-empty response comes back.
#
# Prerequisites:
#   1. Build images:  scripts/build.sh build
#   2. Start stack:   scripts/build.sh up
#   3. ANTHROPIC_API_KEY must be set in .env or environment

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Lisa Agent Smoke Test ==="
echo ""

# ── Pre-flight checks ────────────────────────────────────────────────

if ! command -v docker &>/dev/null; then
    echo "ERROR: docker is not installed."
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q '^lisa$'; then
    echo "ERROR: Lisa container is not running."
    echo "Start the stack first:  scripts/build.sh build && scripts/build.sh up"
    exit 1
fi

# ── Send test message ────────────────────────────────────────────────

TEST_MESSAGE="Hi Lisa, what can you help me with?"
echo "Message: \"${TEST_MESSAGE}\""
echo ""

START_NS=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")

RESPONSE=$(docker exec -u claude lisa claude -p "$TEST_MESSAGE" \
    --output-format json \
    --append-system-prompt-file /agent/role.md \
    --no-session-persistence \
    --max-turns 1)

END_NS=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
DURATION_MS=$(( (END_NS - START_NS) / 1000000 ))

# ── Parse and display ────────────────────────────────────────────────

RESULT=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('result', ''))
")

COST=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"\${data.get('total_cost_usd', 0):.4f}\")
")

echo "--- Response ---"
echo "$RESULT"
echo ""
echo "--- Metrics ---"
echo "Duration : ${DURATION_MS}ms"
echo "Cost     : ${COST}"
echo ""

# ── Validate ─────────────────────────────────────────────────────────

if [ -z "$RESULT" ]; then
    echo "FAIL: Lisa returned an empty response."
    exit 1
fi

echo "PASS: Lisa responded successfully."
