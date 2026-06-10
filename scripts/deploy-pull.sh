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

# Self-edit safety: bash reads scripts lazily, line by line. Mid-run we
# `git reset --hard origin/main`, which can rewrite this file under our
# feet — bash would then continue from a byte offset that no longer
# corresponds to the new contents, producing surreal failures. Wrapping
# the body in a brace group forces bash to slurp the whole block before
# executing it, so any post-reset rewrite to deploy-pull.sh only takes
# effect on the *next* timer tick.
{

REPO_DIR="${REPO_DIR:-/opt/ai-dev-team}"
BRANCH="${BRANCH:-main}"
LOCK_FILE="${LOCK_FILE:-/var/lock/ai-dev-team-deploy.lock}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/healthz}"
HEALTH_RETRIES="${HEALTH_RETRIES:-12}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
PREVIOUS_SHA_FILE="${PREVIOUS_SHA_FILE:-${REPO_DIR}/.deploy-previous-sha}"
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
TIMER_UNIT="${TIMER_UNIT:-ai-dev-team-deploy.timer}"
AUTODEPLOY_DRAIN_TIMEOUT="${AUTODEPLOY_DRAIN_TIMEOUT:-1800}"

log() {
    printf '%s deploy-pull: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# Build a Slack webhook payload with jq so commit subjects containing
# quotes/backslashes/newlines can't break the JSON. Echoes the JSON to
# stdout; callers feed it to curl. Kept as its own function so the unit
# tests can exercise the escape logic without curl in the loop.
build_slack_payload() {
    jq -cn --arg text "$1" '{text: $text}'
}

# Post a Slack message via webhook. Best-effort: never let a failed Slack
# call propagate up and mask a real deploy outcome.
slack_notify() {
    local text="$1"
    [ -n "$SLACK_WEBHOOK_URL" ] || return 0
    local payload
    payload=$(build_slack_payload "$text")
    curl --max-time 5 --silent --show-error \
        -H 'Content-Type: application/json' \
        -X POST \
        --data "$payload" \
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

# --- Drain in-flight dispatches before rebuilding -------------------------
COMMIT_SUBJECT=$(git log -1 --format=%s "origin/$BRANCH" 2>/dev/null || true)
if echo "$COMMIT_SUBJECT" | grep -qF "[deploy-force]"; then
    log "[deploy-force] marker detected in commit subject — skipping drain"
    slack_notify ":warning: deploy $(short_sha "$REMOTE") — [deploy-force] honoured, skipping drain and restarting immediately"
elif [ "$AUTODEPLOY_DRAIN_TIMEOUT" = "0" ]; then
    log "AUTODEPLOY_DRAIN_TIMEOUT=0 — skipping drain"
else
    log "running drain helper (timeout=${AUTODEPLOY_DRAIN_TIMEOUT}s) for $(short_sha "$REMOTE")"
    drain_exit=0
    python3 -m router.dispatch.drain \
        --timeout "$AUTODEPLOY_DRAIN_TIMEOUT" \
        --sha "$(short_sha "$REMOTE")" || drain_exit=$?
    if [ "$drain_exit" -eq 0 ]; then
        log "drain complete — proceeding with deploy"
    elif [ "$drain_exit" -eq 1 ]; then
        log "drain timeout reached — proceeding with deploy (workers will be SIGTERM'd)"
    else
        log "drain helper exited with code $drain_exit — treating as internal error, proceeding without drain"
        slack_notify ":warning: drain helper crashed (exit $drain_exit) for $(short_sha "$REMOTE"), proceeding without drain"
    fi
fi
# --------------------------------------------------------------------------

echo "$LOCAL" > "$PREVIOUS_SHA_FILE"
git reset --hard "origin/$BRANCH"
make seed-config

# docker-compose.yml is a *generated* artifact (rendered from the per-
# agent manifests under config/agents/*/agent.yaml by
# scripts/render_compose). Although it's tracked in git — and CI's
# `compose-check` gate is supposed to keep it in sync — we've still
# observed deploys where the on-box compose file doesn't match the
# deployed SHA's manifests, and new/changed agents silently never get
# a service entry. Re-rendering here is idempotent on the happy path
# and self-healing when something has drifted. `set -e` propagates a
# render failure, same as a build failure below.
make compose

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
make seed-config


# Re-render compose for the reverted SHA. `git reset --hard` will have
# restored the tracked docker-compose.yml from the previous SHA, but
# we re-render anyway for the same self-healing reason as the forward
# path — and because if drift was the original culprit, we don't want
# to leave the box on a half-matching file. Unlike the forward path we
# don't want `set -e` to kill us mid-revert: if the renderer blows up
# here, fall through to the "auto-revert ALSO failed" branch below so
# the timer gets stopped and the operator is paged instead of the
# script dying silently.
if ! make compose; then
    log "compose render FAILED during auto-revert — treating as revert failure"
    systemd-run --no-block --unit=ai-dev-team-deploy-stop systemctl stop "$TIMER_UNIT" \
        >/dev/null 2>&1 || systemctl stop "$TIMER_UNIT" >/dev/null 2>&1 || true
    slack_notify ":fire: auto-revert FAILED — compose render errored while reverting from $(short_sha "$BAD_SHA") to $(short_sha "$LOCAL"). Timer stopped; manual intervention required."
    exit 1
fi

# Rebuild before restarting. Without this, the restart below will
# happily reuse the freshly-built BAD_SHA image — `up -d` only rebuilds
# when an image is missing, not when source under a build context has
# changed. The forward path above already runs an explicit `build
# --no-cache`, but the revert path used to skip this step, so reverting
# would restart the stack with the same broken image it just rolled
# back from. Guarded with `if !` so a build failure routes to the
# "auto-revert ALSO failed" branch rather than dying via `set -e`.
if ! docker compose build --no-cache; then
    log "docker build FAILED during auto-revert — treating as revert failure"
    systemd-run --no-block --unit=ai-dev-team-deploy-stop systemctl stop "$TIMER_UNIT" \
        >/dev/null 2>&1 || systemctl stop "$TIMER_UNIT" >/dev/null 2>&1 || true
    slack_notify ":fire: auto-revert FAILED — docker build errored while reverting from $(short_sha "$BAD_SHA") to $(short_sha "$LOCAL"). Timer stopped; manual intervention required."
    exit 1
fi
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

}
