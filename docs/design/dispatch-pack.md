# `dispatch` Pack v1 — Design 1-pager

_Status: draft, pending Bram sign-off · Owner: Sam · Related: [headless-dispatch.md](headless-dispatch.md) (parent design), [headless-dispatch-spike.md](headless-dispatch-spike.md) (spike report) · Closes #150 by reference_

## Goal

Ship the `dispatch` pack that turns the spike recipe into a real Sam verb:
Sam takes a GitHub issue URL, runs a headless `claude -p` subprocess that
opens a PR, and surfaces the result back into the Slack thread. Bridge
contract from the spike holds; this 1-pager fills in the dispatcher.

## What's locked (from spike open questions)

1. **Process model.** Subprocess inside Sam's container. No sibling
   container, no docker socket exposure.
2. **Workspace.** Dedicated Docker named volume `dispatch_workspaces`
   mounted at `/var/lib/dispatch/` inside Sam.
3. **Concurrency.** Hard cap **N=3**. Requests 4+ are queued FIFO and
   surfaced as `queued — slot N of 3 in use`.
4. **Cost ceiling per dispatch.** Phase 2. Bram accepts the quota-burn
   risk for the pilot (worst case: 5h Max-sub lockout). Dispatcher still
   logs `total_cost_usd` per session for telemetry.
5. **Workspace pre-population.** Phase 2. v1 is `git clone --depth=1`
   per dispatch.
6. **PR-author identity.** Bram's PAT, inherited from Sam's existing
   `gh` auth. Dev-agent identity stays as the existing tech-debt issue.
7. **Default model.** Opus (`claude-opus-4-7[1m]`) for feature work,
   Sonnet for small fixes. Override via the verb's `model` arg. The
   spike's "pin Sonnet" advice was a quota-conservation default; Bram
   has chosen to spend Opus tokens for code quality.

## Architecture

```
Slack thread
   │
   ▼
Sam ── dispatch.dispatch_issue(issue_url, persona="dev", model="opus")
        │
        ▼
   DispatchManager (in-process, asyncio)
   ┌──────────────────────────────────────────────────────┐
   │ Slot pool (N=3)        FIFO queue                    │
   │ ├─ slot 1: dispatch_a  ├─ dispatch_d (waiting)       │
   │ ├─ slot 2: dispatch_b  └─ dispatch_e (waiting)       │
   │ └─ slot 3: dispatch_c                                │
   └──────────────────────────────────────────────────────┘
        │
        ▼  (per dispatch, asyncio.subprocess, new session/pgid)
   ┌──────────────────────────────────────────────────────┐
   │ Workspace: /var/lib/dispatch/<dispatch_id>/repo/     │
   │ Auth:      CLAUDE_CONFIG_DIR=/var/lib/dispatch/      │
   │            <dispatch_id>/claude/  (seeded from       │
   │            ~/.claude/ via copy, not symlink)         │
   │ Command:   claude -p "$PROMPT" --model "$MODEL"      │
   │            --output-format stream-json                │
   │            --add-dir <workspace> --permission-mode   │
   │            acceptEdits                               │
   │ Lifetime:  tracked in registry by (dispatch_id, pgid)│
   └──────────────────────────────────────────────────────┘
        │
        ▼
   On completion → parse JSON → Slack reply with PR link + cost + turns
   On timeout/cancel → SIGTERM to pgid → workspace teardown → Slack reply
```

### Verbs

- `dispatch_issue(issue_url, persona="dev", model="opus")` — primary verb.
  Returns `{dispatch_id, status: "queued"|"running", slot}`.
- `dispatch_status(dispatch_id)` — read-only. Returns latest JSON event
  from the stream plus running `total_cost_usd`.
- `dispatch_cancel(dispatch_id)` — SIGTERM the process group, mark
  cancelled, tear down workspace.
- `dispatch_health` — smoke probe (see *Health / smoke probe*).

### Approval gating

Same as the parent design: first 5 dispatches gated via `draft-approval`,
flip via config flag after Bram's call. Re-dispatch ("address review on
PR #N") never gated. `dispatch_cancel` is ungated — cancelling is
always safe.

## Workspace lifecycle

### Layout

```
/var/lib/dispatch/                          ← named volume mount
├── <dispatch_id>/                          ← one per dispatch
│   ├── repo/                               ← `git clone --depth=1` here
│   ├── claude/                             ← CLAUDE_CONFIG_DIR (auth)
│   ├── prompt.txt                          ← exact prompt sent
│   ├── stream.jsonl                        ← captured stdout JSON stream
│   └── result.json                         ← final event, post-mortem use
└── _orphans/                               ← janitor sweep target
```

`<dispatch_id>` is `dispatch-<utc-iso-compact>-<6hex>` for sortability +
uniqueness. Example: `dispatch-20260517T143022-a3f9c1`.

### Creation

Per dispatch, in this order:

1. `mkdir -p /var/lib/dispatch/<id>/{repo,claude}` with mode 0700.
2. `cp -a ~/.claude/. /var/lib/dispatch/<id>/claude/` (copy, not symlink;
   prevents the concurrent-refresh race called out in the locked-in
   decisions).
3. `git clone --depth=1 <repo_url> /var/lib/dispatch/<id>/repo/`.
4. `git checkout -b dispatch/<issue_number>-<short_slug>` inside `repo/`.

Per-dispatch overhead: ~50ms for the auth copy, ~2–5s for the shallow
clone. Acceptable for the dispatch-issue path; the smoke probe uses a
pre-seeded sentinel workspace (see *Health*).

### Cleanup

- **Happy path:** `finally`-block in the dispatcher entrypoint runs
  `rm -rf /var/lib/dispatch/<id>/` after the result is captured and
  posted back to Slack. The `exitcode` file is written before teardown
  so post-mortems survive.
- **Crash path:** if the dispatcher itself dies mid-run, the workspace
  stays behind. Picked up by the janitor.
- **Janitor** (`packs/dispatch/janitor.py`): runs once per Sam container
  start, lazily on the first non-`dispatch_health` verb invocation (wired
  via a `threading.Lock` sentinel in `handler.py`).  Sweeps
  `/var/lib/dispatch/` for any workspace whose `exitcode` file is present
  (terminal-but-uncleaned) or whose `mtime` is at least
  `STARTUP_GRACE_SECONDS` (60 s) old without an `exitcode` (crashed
  in-flight).  Orphans are renamed into `_orphans/<UTC-ts>-<id>/`
  (same volume — no copy).  Entries in `_orphans/` with `mtime` older
  than `ORPHAN_TTL_DAYS` (7 days) are deleted with `shutil.rmtree`.

### Volume declaration

Adds to `docker-compose.yml` (renderer-generated, so the change lands in
`config/agents/sam/agent.yaml` or the renderer template — picked during
implementation, not here):

```yaml
sam:
  volumes:
    - dispatch-workspaces:/var/lib/dispatch
volumes:
  dispatch-workspaces:
```

No host bind-mount on purpose — keeps the workspace off the host project
tree, survives `docker compose down` for post-mortems, wipeable with
`docker volume rm dispatch-workspaces`.

## Concurrency & queue

- **Hard cap N=3.** Configurable via `config/dispatch.yaml: max_concurrent`,
  but defaulted and shipped at 3.
- **Queue is FIFO, in-memory.** Lost on Sam restart by design — a restart
  during a queued dispatch will surface as "Sam restarted, please
  re-dispatch" in the Slack thread. Persistent queue is over-engineering
  for the pilot volume.
- **Slot reservation** happens before workspace creation. Reserve →
  create workspace → spawn subprocess → release on completion. Failure
  during creation releases the slot immediately.
- **Queue surface in Slack:** when a dispatch enters the queue, post one
  message: `Queued — N requests in flight, position M in queue.` On
  promotion to slot: `Slot acquired, dispatch starting.` No spam pings
  while waiting.

### Auth isolation at N>1

The spike noted that concurrent `claude -p` invocations all read
`~/.claude/`. At N=3 this is a real race during token refresh. The fix:

- Copy (not symlink) `~/.claude/` into `<workspace>/claude/` at dispatch
  start.
- Spawn the subprocess with `CLAUDE_CONFIG_DIR=<workspace>/claude/`.
- If the subprocess refreshes the token, it mutates the copy, not the
  canonical creds. Refreshes are read-only against the canonical creds.
- On token expiry of the canonical creds (rare — Max sub keychain is
  long-lived), all subsequent dispatches inherit the stale copy until
  Sam restarts. Tech-debt follow-up: a background refresher in Sam
  that re-copies `~/.claude/` on a schedule. Not v1.

This is a deliberate copy-on-dispatch model. Adds ~50ms; eliminates the
race entirely.

## Kill semantics

- Spawn with `start_new_session=True` so each dispatch gets its own
  process group.
- Track `(dispatch_id, pgid)` in the in-process registry.
- `dispatch_cancel` → `os.killpg(pgid, SIGTERM)` → wait 5s →
  `os.killpg(pgid, SIGKILL)` if still alive. Then run workspace cleanup.
- Cancellation surfaces in Slack as `dispatch <id> cancelled by user`.
- The same path fires on a global timeout (see *Failure modes*).

## Quota telemetry (day 1)

Three parallel Opus dispatches can chew the 5h window in minutes. The
dispatcher MUST surface this from launch:

- **Per-dispatch:** parse `total_cost_usd`, `num_turns`, and per-model
  token counts from the streamed JSON. Post a one-line summary to the
  Slack thread on completion: `dispatch <id> done · $X.XX · N turns ·
  model: <model>`.
- **Per-window estimate:** maintain a rolling 5h sum of `total_cost_usd`
  across all dispatches in `/var/lib/dispatch/_telemetry.json`. Expose
  via `dispatch_health`. Surface a `Heads up: ~$X spent in last 5h,
  approaching window cap` warning at 80% of an empirically-derived
  threshold (we'll tune after the first 3–5 dispatches; placeholder
  threshold: $40 / 5h).
- **No hard cap.** Bram has accepted the lockout risk. If we get
  locked out, the next dispatch will fail with a structured error from
  the JSON envelope; the dispatcher reports it and stops trying for
  the rest of the window.

## Slack surface

Per dispatch, one Slack thread. Messages posted by Sam in that thread:

1. **Approval card** (if gated) — `draft-approval` block with
   `{issue_url, persona, model, prompt_preview}`.
2. **Queued** (only if all slots full) — `Queued — position N in queue`.
3. **Started** — `Dispatch <id> started · slot <n> of 3 · model
   <model>`.
4. **Completed** — `Dispatch <id> done · PR <url> · $X.XX · N turns`,
   with `result.json` attached as a thread file.
5. **Failed** — `Dispatch <id> failed: <error>`, with `result.json`
   attached if any was captured.
6. **Cancelled** — `Dispatch <id> cancelled by user`.

No periodic progress pings during the run. The user can call
`dispatch_status <id>` to poke for an update.

## Health / smoke probe

`dispatch_health` runs three checks in order, fails fast:

1. **Volume writable.** `touch /var/lib/dispatch/.health` succeeds.
2. **`claude` CLI alive.** `claude --version` exits 0 and prints a
   version ≥ 2.1.142.
3. **End-to-end bridge probe.** `claude -p "echo: hello"
   --model sonnet --output-format json --permission-mode acceptEdits`
   with a 30s timeout. Pinned to Sonnet so the probe never burns Opus
   quota. Expect `is_error: false` and `result` containing `hello`.

Probe runs on Sam container start and emits a one-line Slack heartbeat
on success/failure. Failure = Sam refuses `dispatch_issue` until the
probe passes.

## Failure modes & rollback

1. **`claude` subprocess hangs.** Global per-dispatch timeout
   (`config/dispatch.yaml: max_runtime_seconds`, default 30 min).
   Triggers the kill path. Slack reports `dispatch <id> timed out
   after Nm`.
2. **`claude` subprocess crashes (non-zero exit OR `is_error: true`).**
   Capture `stream.jsonl`, parse the last event, post `Dispatch <id>
   failed: <error>` to Slack. No retry.
3. **Quota window exhausted.** Manifests as `is_error: true` in the
   JSON envelope. Treated as failure-mode #2 plus an additional
   "quota window exhausted, future dispatches will fail until reset"
   note in the thread. The dispatcher sets a soft-lock for 1h and
   refuses new dispatches with a clear message during that window.
4. **Workspace volume full.** Janitor sweep at startup helps; if
   `mkdir` fails for ENOSPC, dispatcher reports `Workspace volume
   full — run docker volume prune or extend the volume`.
5. **Sam container restart mid-dispatch.** In-flight subprocesses are
   reaped by Docker. Workspaces survive (named volume) and are picked
   up by the janitor. Slack threads get no update — the queued
   message stands until the user re-dispatches. Documented limitation.
6. **Token refresh race at N=3.** Eliminated by per-dispatch
   `CLAUDE_CONFIG_DIR` copy (see *Auth isolation*).
7. **GitHub push fails inside the session.** Surfaces as a non-zero
   tool result inside the streamed JSON; the dispatched session
   reports it in its final message, dispatcher relays to Slack. No
   PR opened, no retry, workspace gets cleaned normally.

**Rollback for the whole feature:** revert the `dispatch` pack and
remove `dispatch` from `config/agents/sam/agent.yaml`. Drop the named
volume (`docker volume rm dispatch-workspaces`). No data migration, no
external state to unwind.

## Out of scope (Phase 2 + tech-debt)

Tracked as separate follow-up issues once #D ships:

- Per-dispatch cost circuit breaker (terminate at threshold).
- Persistent worktree reuse for iterative dispatches on the same PR.
- Background refresher for the canonical `~/.claude/` creds copy.
- Daily janitor cron for `_orphans/`.
- Persona registry beyond `dev` (`security-review`, `docs`).
- Dispatcher-layer enforcement of `persona/allowlist.yaml`.
- Dev-agent's own GitHub identity (existing tech-debt thread).
- Webhook-driven PR-opened pings (parent design's #E — separate issue,
  not blocking on #D).

## Local smoke check (before PR merges)

Per Sam's responsibility on every new runtime surface:

1. `docker build -t ai-dev-team-base:latest -f docker/Dockerfile.base docker/`
2. `docker compose up -d --force-recreate sam`
3. `docker exec sam bash -lc 'dispatch_health'` exits 0.
4. Manually trigger one dispatch against a throwaway issue in
   `bramveen1/ai-dev-team-spike` and confirm a PR opens, the Slack
   thread gets the four expected messages, and the workspace gets
   cleaned up.

This must pass before #D's PR is marked ready for review.

## Issue plan

To be filed as the implementation epic; numbering reflects
dependency/implementation order per Bram's convention:

- **#D-1** — `dispatch` pack scaffold (`packs/dispatch/{pack.yaml,
  handler.py, prompt.md, README.md}`), named volume in compose,
  `dispatch_health` verb. Smoke-checkable on its own.
- **#D-2** — `dispatch_issue` happy path: workspace creation, subprocess
  spawn, JSON parsing, Slack reply on completion. Single-slot only
  (N=1) for first integration test.
- **#D-3** — Concurrency: slot pool (N=3), FIFO queue, per-dispatch
  `CLAUDE_CONFIG_DIR`, queue-position Slack surface.
- **#D-4** — Kill path: `dispatch_cancel` verb, process-group SIGTERM,
  global runtime timeout, workspace cleanup on cancel.
- **#D-5** — Telemetry: per-window cost rollup, soft-lock on quota
  exhaustion, heads-up warning at 80% threshold.
- **#D-6** — Janitor on Sam startup: orphan sweep into `_orphans/`.
- **#D-7** — Approval gating wiring + the 5-dispatch flip flag.

Lisa schedules. Pilot retro after the first 5 dispatches feeds the
Phase-2 backlog.
