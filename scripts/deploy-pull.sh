#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/ai-dev-team}"
BRANCH="${BRANCH:-main}"
LOCK_FILE="/var/lock/ai-dev-team-deploy.lock"

log() {
    printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# Re-exec under flock so two timer firings can never overlap. If the lock is
# already held, exit 0 silently — the previous run is still in progress.
if [[ "${_DEPLOY_LOCKED:-0}" != "1" ]]; then
    exec env _DEPLOY_LOCKED=1 flock --nonblock "$LOCK_FILE" "$0" "$@" || exit 0
fi

if [[ ! -d "$REPO_DIR" ]]; then
    log "ERROR: REPO_DIR=$REPO_DIR does not exist"
    exit 1
fi

cd "$REPO_DIR"

if [[ ! -d ".git" ]]; then
    log "ERROR: $REPO_DIR is not a git repository"
    exit 1
fi

git fetch --quiet origin "$BRANCH"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [[ "$LOCAL" == "$REMOTE" ]]; then
    log "up to date at $LOCAL"
    exit 0
fi

log "deploying $LOCAL -> $REMOTE"

git reset --hard "origin/$BRANCH"

docker compose build --no-cache
docker compose up -d --remove-orphans

sleep 10
docker compose ps

docker image prune -f

log "deploy complete at $(git rev-parse HEAD)"
