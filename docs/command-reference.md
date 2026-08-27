# Command Reference

Day-to-day cheat sheet for driving the ai-dev-team stack. Every command on
one page, grouped by where you type it. For background and design, follow
the links at the end.

> **Three places you type commands:**
> 1. **Slack — DM the *router* bot (or `@router` in a channel).** Pack
>    grants, kills, scheduled tasks live here.
> 2. **Slack — DM/mention an *agent* (Sam, Lisa, Maya…).** Dispatching
>    dev workers and asking an agent to do anything else.
> 3. **The host shell (your laptop / the deploy box).** `make`, `docker
>    compose`, and the raw pack handlers — used for setup, smoke checks,
>    and emergency recovery.

---

## 1 — Slack commands to the router

The router is the dispatcher and grants broker. **Agent bots don't accept
these** — they'll politely shrug.

### Pack grants (services like GitHub, Zoho Mail, ops-diag)

| Command | What it does |
|---|---|
| `list packs` | All packs that exist in `packs/`. |
| `who has <pack>` | Which agents currently have it. |
| `grant <agent> <pack>` | Walk through auth and add the pack to the agent. |
| `revoke <agent> <pack>` | Remove the pack from the agent. |

After `grant` or `revoke`, the router tells you to run
`docker compose restart <agent>` — that pickup is still manual.

Example:

```
@router grant sam github
```

Full walkthrough, failure modes, what gets written where:
[managing-agents-from-slack.md](managing-agents-from-slack.md).

### Killing a stuck agent

Registered as a slash command on every agent's Bolt app.

| Command | What it does |
|---|---|
| `/kill` | Kill the current thread's last-active agent. |
| `/kill <agent>` | Kill `<agent>` in this thread. |
| `/kill <agent> all` | Kill `<agent>` in **every** thread the router is tracking. Escape hatch — don't reach for this by default. |

A kill honours regardless of guard mode, writes a `manual_kill`
post-mortem, and posts a Slack note. There is no resume — re-ping the
agent with a fresh task to continue.

### Scheduled tasks (`/tasks`)

Each agent has its own slash command: `/tasks` is registered per-agent
and scoped to the caller — Sam can't see or edit Lisa's tasks.

| Subcommand | Action |
|---|---|
| `/tasks` *(or `/tasks list`)* | Show the calling agent's tasks. |
| `/tasks create` | Open a modal: name, prompt, 5-field cron (UTC), destination channel. |
| `/tasks pause <task_id>` | Disable a task. |
| `/tasks resume <task_id>` | Re-enable a task. |
| `/tasks delete <task_id>` | Remove a task. |

Cron is standard 5-field POSIX (`minute hour dom month dow`) supporting
`*`, `N`, `N-M`, `A,B,C`, `*/N`, `A-B/N`. Times are UTC. Scheduler
polls every 30 s.

Full model and behaviour: [scheduled-tasks.md](scheduled-tasks.md).

---

## 2 — Slack: dispatching a dev worker (Sam)

Sam is the agent that spawns headless `claude -p` workers to take a
GitHub issue → PR. The flow is **conversational**, not a slash command:

1. DM Sam (or @-mention in a channel) with the work. Best shape is "pick
   up issue #N" or "implement #N — scope is X, leave Y alone".
2. Sam writes a 1-pager if the task hits a Design-First Trigger,
   otherwise replies with a `dispatch-approval` fenced block. The
   router renders it as an **approval card** in the same thread.
3. Click **Approve** on the card. The router re-invokes
   `dispatch_issue --approved`; click **Decline** and Sam posts
   "dispatch declined".
4. Worker runs in the background. The supervisor polls every ~120 s and
   posts deltas + a terminal summary into the thread.

**Defaults** (you don't need to specify these unless you want to override):

| Setting | Default | Where it lives |
|---|---|---|
| Model | `sonnet` | `handler.py` — `DEFAULT_DISPATCH_MODEL` |
| Persona | `dev` | `handler.py` — `DEFAULT_DISPATCH_PERSONA` |
| Budget | 1 800 s (30 min) | `handler.py` — `DEFAULT_BUDGET_SECONDS` |
| Supervision | `poll` (router polls every ~120 s) | `handler.py` — `DEFAULT_SUPERVISION_MODE` |
| Pool size | 3 concurrent workers | `handler.py` — `POOL_SIZE` |

**Approval gate** is `require_always: true` in pilot mode
(`config/dispatch.yaml`). Once it's flipped to `false`, the gate fires
only on (a) `model=opus` + destructive keyword in the issue, or (b) the
5h cost window crossing `DISPATCH_APPROVAL_COST_USD` (default \$15).

**`/kill <agent>`** above is the kill switch for a worker that's looping.

Background: [`packs/dispatch/README.md`](../packs/dispatch/README.md) and
[`docs/design/dispatch-pack.md`](design/dispatch-pack.md).

---

## 3 — Host shell: `make` targets

Run from the repo root on the host that holds the compose stack.

| Target | Purpose |
|---|---|
| `make compose` | Render `docker-compose.yml` from `config/agents/*/agent.yaml`. |
| `make compose-check` | Fail if `docker-compose.yml` is stale vs the manifests (CI uses this). |
| `make up` | Render + `docker compose up -d --build` (the `--build` is load-bearing). |
| `make down` | `docker compose down`. |
| `make test` | Run unit + integration test suites. |
| `make lint` | `ruff check .` **and** `ruff format --check .` (both are CI gates). |
| `make format` | Auto-fix with `ruff format .`. |
| `make add-agent` | Run the add-agent wizard. |
| `make seed-config` | Sync tracked defaults from `config.example/` → `config/`; secrets/identity files (e.g. `agent.yaml`, `secrets/*`) are seeded once and never overwritten. |
| `make fix-permissions` | Reset `config/agents/*/memory` ownership to uid 1000 + 0700/0600 (issue #116). |

CI **runs both** `ruff check .` and `ruff format --check .` — a clean
`check` with a failing `format --check` still fails CI, so `make lint`
is the safe local check.

---

## 4 — Host shell: pack handlers directly

Useful for smoke checks and emergency recovery. Run them **inside the
target agent container** (the agent is what has `~/.claude/`).

```bash
# Dispatch pack smoke probe — does claude work end-to-end?
docker exec sam python /config/packs/dispatch/handler.py dispatch_health

# Read the current state of a dispatch.
docker exec sam python /config/packs/dispatch/handler.py dispatch_status \
  --dispatch-id dispatch-20260607T120000-abc123

# Cancel a running dispatch (SIGTERM → 5 s grace → SIGKILL, then wipe workspace).
docker exec sam python /config/packs/dispatch/handler.py dispatch_cancel \
  --dispatch-id dispatch-20260607T120000-abc123

# Fetch recent router log lines (requires ops-diag grant).
docker exec sam python /config/packs/ops-diag/handler.py router_logs --tail 100
```

**Emergency bypass.** `dispatch_issue` accepts a suppressed `--approved`
flag that skips the approval card. The router uses this internally
after a click on Approve — use it from a shell **only** when the
approval card has failed to render and you can't otherwise unstick a
job. The flag is intentionally not in `--help`.

```bash
docker exec sam python /config/packs/dispatch/handler.py dispatch_issue \
  --issue-url https://github.com/bramveen1/ai-dev-team/issues/NNN \
  --approved
```

The supervision contract is documented in
[scheduled-tasks.md § System Tasks](scheduled-tasks.md#system-tasks).

---

## 5 — Where commands live in the code

For when this cheat sheet drifts and you want the source of truth.

| Surface | Implementation |
|---|---|
| Pack commands (`grant`, `revoke`, `list packs`, `who has`) | `router/packs/grants.py` — `parse_command()` |
| `/kill` slash command | `router/kill_command.py` |
| `/tasks` slash command | `router/scheduled_tasks/handlers.py` |
| Dispatch verbs | `packs/dispatch/handler.py` — `dispatch_health` / `dispatch_issue` / `dispatch_status` / `dispatch_cancel` |
| ops-diag verb | `packs/ops-diag/handler.py` — `router_logs` |
| Approval card buttons | `router/approvals/handlers.py` |
| `make` targets | `Makefile` |

---

## 6 — Related docs

- [Managing agents from Slack](managing-agents-from-slack.md) — full grant/revoke walkthrough.
- [Scheduled tasks](scheduled-tasks.md) — `/tasks`, cron syntax, system tasks.
- [Agents roster](agents.md) — who's on the team.
- [Add a new agent](add-a-new-agent.md) — runbook.
- [Authoring a pack](authoring-a-pack.md) — wiring a new external service.
- [`packs/dispatch/README.md`](../packs/dispatch/README.md) — worker contract, approval gating, smoke check.
- [`packs/ops-diag/README.md`](../packs/ops-diag/README.md) — router log access.
