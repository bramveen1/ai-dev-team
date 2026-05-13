#!/usr/bin/env bash
# scripts/deploy-pull.sh — one deploy-check + apply cycle.
#
# Pull-based CD: invoked by ai-dev-team-deploy.timer every ~2 minutes.
# Checks origin/<BRANCH> for new commits, and if there are any, fast-forwards
# the working tree, rebuilds images, restarts the stack, and health-checks
# the router. On health-check failure the previous SHA is auto-restored
# (issue #107).
#
# Environment (override via /etc/ai-dev-team-deploy.env):
#   REPO_DIR              repo checkout on the box       (default: /opt/ai-dev-team)
#   BRANCH                branch to track                (default: main)
#   HEALTH_URL            health endpoint to probe       (default: http://localhost:8080/healthz)
#   HEALTH_RETRIES        max probe attempts             (default: 12; 0 disables auto-revert)
#   HEALTH_INTERVAL       seconds between probes         (default: 5)
#   SLACK_WEBHOOK_URL     optional notify webhook        (default: unset → skip)
#
# Exit codes:
#   0  — no-op (already up to date), or deploy + health check succeeded,
#        or deploy failed but auto-revert succeeded.
#   1  — auto-revert also failed, or fatal setup error (repo missing,
#        not a git repo, etc.). Timer is stopped before exiting non-zero
#        when an auto-revert flaps so we don't loop.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/ai-dev-team}"
BRANCH="${BRANCH:-main}"
LOCK_FILE="${LOCK_FILE:-/var/lock/ai-dev-team-deploy.lock}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/healthz}"
HEALTH_RETRIES="${HEALTH_RETRIES:-12}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
PREVIOUS_SHA_FILE="${PREVIOUS_SHA_FILE:-${REPO_DIR}/.deploy-previous-sha}"
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
TIMER_UNIT="${TIMER_UNIT:-ai-dev-team-deploy.timer}"

log() {
    printf '%s deploy-pull: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# Post a Slack message via webhook. Best-effort: never let a failed Slack
# call propagate up and mask a real deploy outcome.
slack_notify() {
    local text="$1"
    [ -n "$SLACK_WEBHOOK_URL" ] || return 0
    # Single quotes inside JSON body would break the payload; rely on the
    # caller to keep `text` free of newlines / control chars (we control
    # every call site in this script).
    curl --max-time 5 --silent --show-error \
        -H 'Content-Type: application/json' \
        -X POST \
        -d "{\"text\":\"${text}\"}" \
        "$SLACK_WEBHOOK_URL" >/dev/null || log "slack_notify failed (non-fatal)"
}

short_sha() {
    printf '%s' "${1:0:12}"
}

# Probe HEALTH_URL up to HEALTH_RETRIES times. Returns 0 on first 200,
# 1 if every attempt failed. With HEALTH_RETRIES=0 we skip probing
# entirely and treat the deploy as successful — the documented escape
# hatch for disabling auto-revert.
health_check() {
    if [ "$HEALTH_RETRIES" -le 0 ]; then
        log "HEALTH_RETRIES=0 — skipping health check (auto-revert disabled)"
        return 0
    fi
    local attempt
    for attempt in $(seq 1 "$HEALTH_RETRIES"); do
        if curl --max-time 3 --silent --fail --output /dev/null "$HEALTH_URL"; then
            log "health check passed on attempt $attempt/$HEALTH_RETRIES"
            return 0
        fi
        log "health check attempt $attempt/$HEALTH_RETRIES failed; sleeping ${HEALTH_INTERVAL}s"
        sleep "$HEALTH_INTERVAL"
    done
    return 1
}

# Best-effort dump of currently-running router task IDs so the journal
# has a record of what got cut off by the restart. Never blocks the
# deploy — any failure is logged and ignored. The router doesn't have
# a list-tasks endpoint today; when it does, swap in the curl call.
record_inflight_tasks() {
    local target_sha="$1"
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    log "recording in-flight tasks before restart (target=$(short_sha "$target_sha"))"
    docker compose ps --format '{{.Service}}\t{{.State}}\t{{.Name}}' 2>/dev/null \
        | while IFS= read -r line; do log "  inflight: $line"; done || true
}

# Acquire an exclusive lock so two overlapping timer firings can't fight
# over the working tree. ``flock -n`` exits non-zero immediately if the
# lock is held; we map that to a silent exit-0 so journalctl doesn't
# fill up with noise.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another deploy already in progress; exiting"
    exit 0
fi

if [ ! -d "$REPO_DIR" ]; then
    log "REPO_DIR=$REPO_DIR does not exist; cannot deploy" >&2
    exit 1
fi
cd "$REPO_DIR"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "REPO_DIR=$REPO_DIR is not a git repo; cannot deploy" >&2
    exit 1
fi

git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    log "up to date at $LOCAL"
    exit 0
fi

log "deploying $(short_sha "$LOCAL") -> $(short_sha "$REMOTE")"
record_inflight_tasks "$REMOTE"

echo "$LOCAL" > "$PREVIOUS_SHA_FILE"
git reset --hard "origin/$BRANCH"

# `--no-cache` matches the spec: every deploy is a clean build. If this
# becomes too slow we can revisit, but cold-cache builds eliminate a
# whole class of "ghost layer" bugs.
docker compose build --no-cache
docker compose up -d --remove-orphans

log "stack restarted; sleeping 10s before health probe"
sleep 10

if health_check; then
    docker image prune -f >/dev/null 2>&1 || true
    NEW_SHA=$(git rev-parse HEAD)
    SUBJECT=$(git log -1 --pretty=%s "$NEW_SHA")
    log "deploy complete at $NEW_SHA"
    slack_notify ":rocket: deployed $(short_sha "$NEW_SHA") — ${SUBJECT}"
    exit 0
fi

# --- Auto-revert path -------------------------------------------------
log "health check failed at $(short_sha "$REMOTE"); reverting to $(short_sha "$LOCAL")"
BAD_SHA="$REMOTE"
git reset --hard "$LOCAL"
docker compose up -d --remove-orphans
log "reverted stack restarted; sleeping 10s before re-probe"
sleep 10

if health_check; then
    log "auto-revert succeeded; box is back on $(short_sha "$LOCAL")"
    slack_notify ":rotating_light: auto-reverted to $(short_sha "$LOCAL") — health check failed on $(short_sha "$BAD_SHA")"
    exit 0
fi

# --- Auto-revert ALSO failed: stop the timer so we don't flap ---------
log "auto-revert health check ALSO failed — stopping $TIMER_UNIT to prevent flapping"
# `systemd-run` lets the running script ask systemd to stop the timer
# that triggered it without inheriting the timer's own systemd dependency
# graph (which would deadlock). Best-effort: if it fails we still exit
# non-zero so the operator sees the failure in `systemctl list-timers`.
systemd-run --no-block --unit=ai-dev-team-deploy-stop systemctl stop "$TIMER_UNIT" \
    >/dev/null 2>&1 || systemctl stop "$TIMER_UNIT" >/dev/null 2>&1 || true
slack_notify ":fire: auto-revert FAILED — both $(short_sha "$BAD_SHA") and $(short_sha "$LOCAL") are unhealthy. Timer stopped; manual intervention required."
exit 1
