#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/ai-dev-team}"
DEPLOY_USER="${DEPLOY_USER:-$USER}"
BRANCH="${BRANCH:-main}"

# Resolve to an absolute path so the rendered systemd unit is valid even if
# the operator passed a relative REPO_DIR.
if [[ -d "$REPO_DIR" ]]; then
    REPO_DIR=$(cd "$REPO_DIR" && pwd)
fi

cat <<EOF
ai-dev-team — pull-based CD deploy daemon installer
====================================================

This script installs a systemd timer + service that polls origin/<branch> on
this machine every ~2 minutes and redeploys via 'docker compose' when new
commits land.

Requires:
  - docker, docker compose v2 plugin, git on PATH
  - REPO_DIR is a git checkout of ai-dev-team (default: /opt/ai-dev-team)
  - DEPLOY_USER is in the docker group (default: \$USER)
  - sudo to install unit files under /etc/systemd/system

Configuration:
  REPO_DIR     = $REPO_DIR
  DEPLOY_USER  = $DEPLOY_USER
  BRANCH       = $BRANCH

EOF

# 1. Verify required commands
for cmd in docker git; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: '$cmd' is not on PATH. Install it and re-run." >&2
        exit 1
    fi
done

if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' (v2 plugin) is not available." >&2
    echo "Install the Docker Compose v2 plugin and re-run." >&2
    exit 1
fi

# 2. Verify REPO_DIR is a git checkout
if [[ ! -d "$REPO_DIR/.git" ]]; then
    echo "ERROR: $REPO_DIR is not a git checkout." >&2
    echo "Clone the repo first, e.g.:" >&2
    echo "  sudo mkdir -p $REPO_DIR && sudo chown \$USER $REPO_DIR" >&2
    echo "  git clone https://github.com/bramveen1/ai-dev-team.git $REPO_DIR" >&2
    exit 1
fi

REMOTE_URL=$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || echo "<none>")
CURRENT_HEAD=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo "<unknown>")
echo "Repository check:"
echo "  remote: $REMOTE_URL"
echo "  HEAD:   $CURRENT_HEAD"
echo

# 3. Verify DEPLOY_USER is in the docker group
if ! id -nG "$DEPLOY_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    echo "ERROR: user '$DEPLOY_USER' is not in the 'docker' group." >&2
    echo "Add them with:" >&2
    echo "  sudo usermod -aG docker $DEPLOY_USER" >&2
    echo "Then log out and back in (or 'newgrp docker') and re-run." >&2
    exit 1
fi

# 4. Make the deploy script executable
chmod +x "$REPO_DIR/scripts/deploy-pull.sh"

# 5. Render the service unit with the correct User=, REPO_DIR, and BRANCH
SERVICE_SRC="$REPO_DIR/systemd/ai-dev-team-deploy.service"
TIMER_SRC="$REPO_DIR/systemd/ai-dev-team-deploy.timer"
SERVICE_TMP=$(mktemp)
trap 'rm -f "$SERVICE_TMP"' EXIT
sed \
    -e "s|__DEPLOY_USER__|$DEPLOY_USER|g" \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__BRANCH__|$BRANCH|g" \
    "$SERVICE_SRC" > "$SERVICE_TMP"

# 6 + 7. Install unit files
echo "Installing unit files (sudo required)..."
sudo install -m 644 "$SERVICE_TMP" /etc/systemd/system/ai-dev-team-deploy.service
sudo install -m 644 "$TIMER_SRC"   /etc/systemd/system/ai-dev-team-deploy.timer

# 8 + 9. Reload systemd and enable the timer
sudo systemctl daemon-reload
sudo systemctl enable --now ai-dev-team-deploy.timer

# 10. Show final status
echo
sudo systemctl status ai-dev-team-deploy.timer --no-pager || true

# 11. Print next-step instructions
cat <<'EOF'

Installed. Useful commands:

  Tail deploy logs:
    journalctl -u ai-dev-team-deploy.service -f

  List timer schedule:
    systemctl list-timers ai-dev-team-deploy.timer

  Pause deploys:
    sudo systemctl stop ai-dev-team-deploy.timer

  Resume deploys:
    sudo systemctl start ai-dev-team-deploy.timer

See docs/cd-deployment.md for full operational guidance.
EOF
