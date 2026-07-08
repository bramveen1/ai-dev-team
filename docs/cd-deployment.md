# Continuous Deployment — pull-based deploy daemon

`main` is the source of truth. Every merge to `main` lands on the
production Linux box automatically via a systemd timer that polls
`origin/main` every couple of minutes. There is no GitHub Actions
self-hosted runner, no SSH push, no inbound listener related to CD.

This document describes how to install, operate, and reason about the
daemon. The design rationale lives in issue
[#107](https://github.com/bramveen1/ai-dev-team/issues/107).

## What it does

A `systemd.timer` on the prod host fires `ai-dev-team-deploy.service`
every 2 minutes. The service runs `scripts/deploy-pull.sh`, which:

1. Acquires a flock so two timer firings can't overlap.
2. `git fetch`es the tracked branch (`main` by default).
3. If `HEAD == origin/main`, logs "up to date" and exits.
4. Otherwise: records the current SHA, `git reset --hard origin/main`,
   rebuilds `ai-dev-team-base:latest` if its inputs changed (see
   [Base-image rebuilds](#base-image-rebuilds)), `docker compose build
   --no-cache`, `docker compose up -d --remove-orphans`.
5. Waits 10s for the stack to settle, then probes `/healthz` on the
   router up to 12 times (every 5s, 60s total).
6. On success: prunes dangling images, logs the new SHA, posts to Slack.
7. On failure: auto-reverts (see below).

The log of every cycle is in `journalctl -u ai-dev-team-deploy.service`.

## Files

| Path | Purpose |
| --- | --- |
| `scripts/deploy-pull.sh` | The deploy cycle itself. One file, runnable by hand for debugging. |
| `systemd/ai-dev-team-deploy.service` | `Type=oneshot` unit that runs the script. |
| `systemd/ai-dev-team-deploy.timer` | 2-minute interval timer. |
| `scripts/install-deploy-daemon.sh` | Installs the unit files + env file, enables the timer. |
| `/etc/ai-dev-team-deploy.env` | Mode-600 env file with `REPO_DIR`, `HEALTH_URL`, `SLACK_WEBHOOK_URL`, etc. Written by the installer. |
| `router/healthz.py` | The `/healthz` endpoint the daemon probes. |

## Base-image rebuilds

Every agent service (Lisa, etc.) declares `image: ai-dev-team-base:latest`
in `docker-compose.yml` with **no `build:` key** — there's a single base
image, built once from `docker/Dockerfile.base`, and referenced by every
service. `docker compose build` only rebuilds services that *have* a build
context, so it never touches `ai-dev-team-base:latest` on its own.

To keep `docker/Dockerfile.base` and `docker/entrypoint.sh` changes from
silently getting stuck on stale layers (issue [#703](https://github.com/bramveen1/ai-dev-team/issues/703)),
`deploy-pull.sh` rebuilds the base image itself, before `docker compose
build` / `docker compose up -d`, whenever needed:

- It diffs `docker/Dockerfile.base` and `docker/entrypoint.sh` between the
  last successfully-deployed SHA (`.deployed-sha`) and the SHA it's
  deploying. If either file changed, it runs
  `docker build -t ai-dev-team-base:latest -f docker/Dockerfile.base docker/`
  before continuing.
- If the last-deployed SHA is unknown or the diff can't be computed, it
  rebuilds unconditionally — conservative by design, so a base-image change
  is never silently skipped.
- A redeploy of a SHA whose base-image inputs are unchanged skips the
  rebuild (the deploy stays idempotent).
- If the base-image build fails, the deploy aborts before `docker compose
  up -d` and `.deployed-sha` is **not** written — same "fail loud, keep the
  old container running" contract as every other build step in this script.
- The auto-revert path does the same check in the opposite direction (bad
  SHA → good SHA) before restarting on the reverted source, so a revert
  can't leave the box running reverted code on top of the bad SHA's
  base-image layers.

**Break-glass:** if you ever need to force a base-image rebuild by hand
(e.g. while debugging on the box, or before this mechanism existed),
the manual sequence still works:

```bash
docker build -t ai-dev-team-base:latest -f docker/Dockerfile.base docker/
docker compose up -d --force-recreate
```

## First-time install on the box

```bash
sudo git clone https://github.com/bramveen1/ai-dev-team /opt/ai-dev-team
cd /opt/ai-dev-team
# Populate .env with your Slack credentials (see .env.example).
sudo REPO_DIR=/opt/ai-dev-team \
     SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ \
     scripts/install-deploy-daemon.sh
```

That writes `/etc/ai-dev-team-deploy.env`, installs the systemd units
to `/etc/systemd/system/`, and enables the timer. Confirm:

```bash
systemctl list-timers ai-dev-team-deploy.timer   # shows NEXT firing time
curl http://localhost:8080/healthz               # 200 once the stack is up
journalctl -u ai-dev-team-deploy.service -f      # tail the deploy log
```

## Day-to-day operations

### Normal deploy

Merge to `main`. Within ~2 minutes the journal shows the deploy, and
the configured Slack channel gets:

> :rocket: deployed `<short-sha>` — `<commit subject>`

### Normal rollback

`git revert <bad-sha>` on `main` and push. The daemon converges on the
next tick — the revert is just another commit. **This is the primary
rollback path.** No SSH, no manual touch of the box.

### Pause deploys

```bash
sudo systemctl stop ai-dev-team-deploy.timer
```

New merges will not deploy until the timer is started again
(`systemctl start ai-dev-team-deploy.timer`). The box continues to
serve whatever SHA it was last on.

### Run a deploy immediately

```bash
sudo systemctl start ai-dev-team-deploy.service
```

This runs one cycle out-of-band without waiting for the next timer tick.

## Workers app channel membership

**Invariant:** The `ai-dev-team-workers` Slack app must be a member of every
channel where workers post. Without it, Slack rejects `chat.postMessage` with
`not_in_channel` — a hard error that propagates to the dispatching thread rather
than silently falling back to the agent token.

This was validated empirically on 2026-06-07: cross-app `app_mention` delivery
only worked once the receiving app was invited to the channel (the
"Dave-in-channel" incident). Writing it here so we don't re-debug it in three
months.

### When to add membership

- **New Slack channel.** Any time you create (or rename) a channel where workers
  should post, invite the workers app before dispatching anything there.
- **First-time `ai-dev-team-workers` setup (Step 0 of #253).** Invite the app to
  every channel where agents already operate — at minimum all channels listed in
  `data/agents.yml`.

### How to invite

Open the channel in Slack and run:

```
/invite @ai-dev-team-workers
```

### Diagnosing a missing-membership failure

Worker logs (inside the agent container) will contain `not_in_channel`. The
error is also forwarded to the dispatching Slack thread by the existing
dispatch-error path. Invite the app to the channel and re-dispatch; no restart
required.

## Auto-revert behaviour

Every deploy runs a health check after the restart. If the new SHA fails
the health check (12 attempts × 5s = 60s probe window by default), the
daemon:

1. `git reset --hard $PREVIOUS_SHA` (read from `.deploy-previous-sha`
   in `REPO_DIR`).
2. `docker compose up -d --remove-orphans` to restart on the previous SHA.
3. Re-probes the health endpoint.
4. If the revert also passes: Slack gets
   `:rotating_light: auto-reverted to <sha> — health check failed on <bad-sha>`.
5. **If the revert _also_ fails**: the timer is stopped automatically
   (so we don't flap), Slack gets a `:fire:` message, and the operator
   needs to intervene manually. The box is in a known-bad state at this
   point; better to stop and ask for help than to keep cycling.

To disable auto-revert, set `HEALTH_RETRIES=0` in
`/etc/ai-dev-team-deploy.env` and restart the timer. The daemon will
still probe but will treat every deploy as successful.

## In-flight task interruption

`docker compose up -d` restarts the router and agent containers, which
kills any in-flight conversations. Before the restart, the deploy
script logs the running compose services (best effort) to the journal
along with the SHA we're moving to, so there's a paper trail of what
got cut off. There is **no task resumption** today — if a thread was
mid-conversation, the user will need to nudge the bot again after the
deploy. Task durability is tracked separately.

## Slack notifications

The script posts to `SLACK_WEBHOOK_URL` (set in
`/etc/ai-dev-team-deploy.env`) on every deploy outcome:

| Outcome | Message |
| --- | --- |
| Deploy succeeded | `:rocket: deployed <sha> — <subject>` |
| Auto-revert succeeded | `:rotating_light: auto-reverted to <sha> — health check failed on <bad-sha>` |
| Auto-revert also failed | `:fire: auto-revert FAILED — both <bad-sha> and <prev-sha> are unhealthy. Timer stopped; manual intervention required.` |

A failed `curl` to the webhook is logged but never blocks the deploy
outcome. To disable notifications entirely, unset `SLACK_WEBHOOK_URL`
in the env file (leave it as `SLACK_WEBHOOK_URL=`) and restart the
timer.

## The `/healthz` endpoint

The router exposes `GET /healthz` on port `8080` inside the container.
It returns:

- `200 {"status":"ok"}` once the router has finished initial setup
  (auth.test calls completed, scheduled-task scheduler running, Socket
  Mode handlers about to start) **and** at least one Slack bot token
  env var is loaded.
- `503 {"status":"<reason>"}` otherwise.

The probe is local-only — no DB calls, no external HTTP. Latency is
well under 100 ms. Compose publishes the port to **loopback only**
(`127.0.0.1:${HEALTHZ_PORT:-8080}:8080`) so the deploy daemon (running
on the same host, outside the container) can curl `127.0.0.1:8080`
while the endpoint stays invisible to anything off-box. To pick a
different host-side port (e.g. when running a second stack on the same
machine), set `HEALTHZ_PORT=9090` in the env file — the container side
stays 8080.

## Safety properties

- **No exposed attack surface.** Nothing on the box is reachable from
  GitHub or from any forked PR. `git fetch` is outbound only.
- **No deploy tokens on the box.** Fetching a public repo over HTTPS
  needs no auth. The only credential on disk is `SLACK_WEBHOOK_URL` —
  the worst case is that an attacker who already has root on the box
  posts in `#deploys`.
- **No interactive sessions.** `Type=oneshot` units don't keep a shell
  open and have no listening ports.
- `/etc/ai-dev-team-deploy.env` is mode 600, root-owned.
- The single port published by compose (`8080`, `/healthz`) returns
  no secrets and is read-only.

## Out of scope

- **Destructive migrations.** `git revert` and auto-revert restore
  code only. They do not undo a destructive DB migration, an outbound
  email, or data that was wiped. Guard rails for that class of change
  will be filed as a separate issue.
- **Task resumption.** A deploy interrupts any in-flight router
  conversation. Re-engage the bot after the deploy.
- **Multi-box deploys.** This daemon converges one host to `origin/main`.
  Running it on N boxes works (each just polls independently) but
  there's no coordination between them; staggered deploys are out of
  scope.
