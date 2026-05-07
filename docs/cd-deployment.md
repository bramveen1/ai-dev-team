# Continuous Deployment — pull-based systemd daemon

## Overview

ai-dev-team's production Linux box runs a small systemd timer that wakes a
shell script every ~2 minutes. The script checks `origin/main` for new commits
and, when there are any, pulls, rebuilds the docker images, and restarts the
stack via `docker compose`.

### Why pull, not push

ai-dev-team is a public repo. Inbound CI runners on public repos (GitHub
Actions self-hosted runners in particular) carry a well-known fork-PR
code-execution risk: a malicious fork PR can be crafted to run arbitrary code
on the runner host. Pull-based CD avoids that entire class of problem —
nothing on the box is reachable from the outside, the box just polls a public
read-only git remote. Forks can't reach in.

Trade-off: deploys are pull-driven, so there is up to ~poll-interval lag
between merge and deploy. That's acceptable for this project.

## Prerequisites

- Docker Engine + Docker Compose v2 plugin (`docker compose ...`, not
  `docker-compose ...`)
- Git
- A user account with `sudo` and `docker` group membership
- Repo cloned to `/opt/ai-dev-team` (or a path of your choice via `REPO_DIR`)
- A populated `.env` file present in that directory

## One-time setup

```bash
sudo mkdir -p /opt/ai-dev-team
sudo chown "$USER" /opt/ai-dev-team
git clone https://github.com/bramveen1/ai-dev-team.git /opt/ai-dev-team
cd /opt/ai-dev-team
cp .env.example .env  # then edit .env and fill in the secrets

scripts/install-deploy-daemon.sh
```

The installer accepts these env-var overrides:

| Variable      | Default              | What it controls                               |
| ------------- | -------------------- | ---------------------------------------------- |
| `REPO_DIR`    | `/opt/ai-dev-team`   | Directory the daemon runs from / pulls into.   |
| `DEPLOY_USER` | `$USER`              | Unix user the service runs as (needs docker).  |
| `BRANCH`      | `main`               | Branch the box tracks.                         |

Example: install from a non-default directory:

```bash
REPO_DIR=/srv/ai-dev-team scripts/install-deploy-daemon.sh
```

The values are baked into the generated systemd unit at install time, so
re-run the installer if you ever want to change them.

Verify:

```bash
systemctl list-timers ai-dev-team-deploy.timer
```

You should see the timer active with a NEXT firing time roughly two minutes
out.

## How deployments work

1. The timer (`ai-dev-team-deploy.timer`) fires every two minutes.
2. It triggers the oneshot service (`ai-dev-team-deploy.service`).
3. The service runs `scripts/deploy-pull.sh`, which:
   - Acquires an exclusive `flock` on `/var/lock/ai-dev-team-deploy.lock` so
     two firings can never overlap. If the lock is busy, the run exits 0
     silently.
   - Runs `git fetch origin <branch>`.
   - Compares local HEAD to `origin/<branch>`. If equal: logs "up to date"
     and exits.
   - Otherwise: `git reset --hard origin/<branch>`,
     `docker compose build --no-cache`,
     `docker compose up -d --remove-orphans`,
     prints `docker compose ps`, then `docker image prune -f`.
4. All output goes to the systemd journal.

## Checking deployment status

```bash
journalctl -u ai-dev-team-deploy.service -f
systemctl list-timers ai-dev-team-deploy.timer
docker compose ps
docker compose logs --tail=50 router
```

## Pausing deploys

```bash
sudo systemctl stop ai-dev-team-deploy.timer    # pause
sudo systemctl start ai-dev-team-deploy.timer   # resume
```

The service unit itself is oneshot, so stopping the timer is enough — no
in-flight deploy will be interrupted, and no new ones will start.

## Rolling back

```bash
sudo systemctl stop ai-dev-team-deploy.timer
cd /opt/ai-dev-team
git log --oneline -10
git checkout <good-sha>
docker compose up -d

# When ready to resume normal CD:
git checkout main && git pull
sudo systemctl start ai-dev-team-deploy.timer
```

You must stop the timer before rolling back. Otherwise the next tick will
notice your local HEAD is behind `origin/main` and immediately roll forward
again.

## Security notes

- **No inbound network access required on the box.** The deploy daemon only
  makes outbound HTTPS calls to GitHub.
- **No GitHub tokens stored on the box.** A public repo over HTTPS needs no
  authentication for `git fetch`.
- **The deploy daemon runs as `DEPLOY_USER`, not root.** It only needs
  `docker` group membership at runtime; `sudo` is only used during one-time
  install to drop the unit files into `/etc/systemd/system`.
- **`git reset --hard` discards local edits on `main`.** Never edit code
  directly on the production box — your changes will be wiped on the next
  deploy. Edit on a branch in a PR.

## Tuning

- Poll interval is set in `systemd/ai-dev-team-deploy.timer` via
  `OnUnitActiveSec=`. Default is 2 minutes. Drop to 1 minute if deploys feel
  sluggish; raise to 5 if it's too chatty.
- Branch tracked is set via the `BRANCH` environment variable in the service
  unit (default `main`). Either re-run the installer with
  `BRANCH=<other> scripts/install-deploy-daemon.sh`, or edit
  `/etc/systemd/system/ai-dev-team-deploy.service` directly and reload
  (`sudo systemctl daemon-reload`).
- `REPO_DIR` is also baked into the unit at install time. If you move the
  checkout, re-run the installer with the new `REPO_DIR=<path>`.

## Why not GitHub Actions self-hosted runners?

We considered it (issue #69) and rejected it. ai-dev-team is a public repo,
and self-hosted runners on public repos are a known code-execution risk: a
fork PR can be crafted to run arbitrary code on the runner host. The
`workflow_run` trigger closes the most obvious path, but it remains a footgun
— one bad config change away from RCE on the deploy box. Not a great default
for an open-source project we want others to fork safely.

If we ever need push-based deploys (e.g. private repo, faster turnaround), a
small webhook receiver on the box is the next step up — but only worth it if
we hit a real limitation with polling.
