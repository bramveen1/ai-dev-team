# Cross-namespace dispatch liveness — Design 1-pager

_Status: draft, pending Bram sign-off · Owner: Sam · Closes #172 by reference · Related: #156 (D-3), #159 (D-6), #163 (supervision design), #171 (D-4 kill ladder)_

## Goal

Replace `os.kill(pid, 0)` as the supervisor's liveness signal so the
router can correctly tell "this dispatch is still running" from "this
dispatch is dead" across PID namespaces. Eliminate the false-orphan
ghost messages and missing completion notices observed on the #171
dispatch.

## Problem (recap)

The router (`router` container) and the agent (`sam` container) live
in **separate PID namespaces**. The pid the handler writes into
`/var/lib/dispatch/<id>/pid` is the babysit's pid *inside the agent's
namespace*. From the router's namespace that integer either does not
exist (so `os.kill(pid, 0)` raises `ProcessLookupError` → "orphan") or
worse, collides with an unrelated router-side process (so the
supervisor declares the dispatch alive when it isn't).

Two symptoms, one root cause:

1. The supervisor's orphan path (`supervision.py:307`) fires on every
   long-running dispatch — that's the `:ghost:` notice we keep seeing.
2. The terminal path can race: if the supervisor writes synthetic
   `exitcode=-1` from the orphan branch *before* the babysit writes
   the real `exitcode=0`, the Slack thread shows "orphaned" forever
   even though the babysit later corrects the file. No completion
   notice is posted because the in-process state already deregistered
   the system task.

This is also a structural blocker for **#156 (D-3 concurrency)** —
running N=3 dispatches on top of an unreliable liveness check
multiplies the same bug by N and tangles the slot-pool's
"is-this-slot-free?" logic with bogus orphans.

## Proposal: heartbeat file as namespace-agnostic liveness

The babysit (which lives in the agent container, has direct access to
the subprocess, and already writes per-tick state) touches a
`heartbeat` file under `/var/lib/dispatch/<id>/` on every event loop
iteration. The supervisor (router-side) reads `heartbeat` mtime
instead of probing the pid.

### Contract

| Field        | Writer  | Cadence                          | Semantics                                    |
|--------------|---------|----------------------------------|----------------------------------------------|
| `heartbeat`  | babysit | every stream-json event + every  | `mtime` is the babysit's "I'm alive" timestamp |
|              |         | `HEARTBEAT_IDLE_SECONDS=15` idle |                                              |
|              |         | tick (whichever comes first)     |                                              |

The babysit's existing event loop already wakes on every JSON line
from the `claude -p` subprocess. We add a `select`/`asyncio.wait_for`
on stdout with a 15s timeout so the babysit can refresh `heartbeat`
even when the worker is quiet (long tool call, sub-agent thinking).

The supervisor classifies a dispatch as alive iff:

```
heartbeat exists AND now - mtime(heartbeat) < HEARTBEAT_STALE_SECONDS
```

Default `HEARTBEAT_STALE_SECONDS = 45` (3× idle interval). Configurable
via env so we can tune on real telemetry without a code change.

### Supervisor changes

Replace the orphan branch in `supervision.py`:

```python
# before (cross-namespace-blind)
if pid_int > 0 and not dstate.pid_alive(pid_int):
    ... orphan ...

# after
if dstate.heartbeat_stale(dispatch_id, root=dispatch_root, now=now):
    ... orphan ...
```

`dstate.pid_alive` stays in the module but is marked deprecated and
unused. Removing it entirely is a follow-up cleanup once nothing else
imports it (currently nothing does).

### Why not the alternatives

- **Shared PID namespace** (`pid: "service:router"` in compose) —
  cleanest from a code POV but couples container lifecycles
  (`docker compose restart sam` would tear down the router), breaks
  the single-directory-copy portability story, and silently fails for
  anyone running these outside Compose. Rejected.
- **Exit-only signalling** (no liveness check, just wait for
  `exitcode`) — simpler but loses the orphan detection entirely. A
  babysit crash that kills both processes before writing `exitcode`
  would leave the supervisor polling forever. Heartbeat catches that
  case via mtime staleness.
- **`docker exec` probe** — the router would shell into the agent
  container to run `kill -0`. Adds a Docker dependency to the
  supervisor, requires the router to know each agent's container name
  (currently it doesn't), and breaks on non-Compose deployments.
  Rejected.

## Acceptance criteria

**Positive**
- A 10+ minute dispatch produces zero `:ghost: orphaned` Slack lines
  while it's still running.
- Completion notice (`:white_check_mark: dispatch <id> done (exit 0)`)
  posts to the originating Slack thread within one supervision tick
  of the babysit writing `exitcode`.
- Unit test: `heartbeat` mtime within stale window → supervisor stays
  in the `delta` branch. Stale heartbeat → supervisor falls through to
  the orphan branch and posts `:ghost: ...`.
- Unit test: babysit `heartbeat` writer fires on a quiet 30s window
  (no stream-json events), proving the idle tick path.

**Negative**
- Babysit crash that takes the workspace down (rm -rf races) → next
  supervision tick sees `dispatch_dir` missing and returns
  `workspace_gone` (existing path, no change).
- Babysit hang (subprocess alive, stuck in a tool call >45s) → does
  NOT trip the orphan branch, because the idle-tick writer keeps
  `heartbeat` fresh. This is the case where the existing `budget`
  timeout (path #3 in supervision.py) is the correct backstop, not
  liveness.
- Filesystem clock skew between agent and router containers — both
  read `mtime` from the same shared volume, so the kernel timestamp
  is consistent regardless of either container's wall clock. No
  per-container time sync required.

## Local smoke check

Required before PR:

```bash
# Boot the stack
docker build -t ai-dev-team-base:latest -f docker/Dockerfile.base docker/
docker compose up -d --force-recreate sam router

# Fire a long-ish dispatch (a no-op sleep wrapper is fine — point is
# duration, not output). Watch the workspace from the host:
docker compose exec sam bash -c 'ls -la --time-style=full-iso /var/lib/dispatch/*/heartbeat'

# In a separate terminal, tail router logs and confirm:
#   - no `:ghost:` posts during the run
#   - one `:white_check_mark:` post at exit
docker compose logs -f router | grep -E "(ghost|check_mark|dispatch)"
```

Test harness: `tests/router/dispatch/test_supervision_heartbeat.py`
covers the four AC unit cases above with `tmp_path` for the workspace
root and `monkeypatch` on `time.time` for mtime simulation.

## Failure modes

| Mode                                    | Detection                      | Recovery                                  |
|-----------------------------------------|--------------------------------|-------------------------------------------|
| Babysit alive, heartbeat write fails    | Stale mtime → orphan path      | Operator sees `:ghost:`, investigates     |
| Babysit dead, subprocess alive          | Stale mtime → orphan path      | Existing orphan synth-exitcode flow       |
| Babysit alive, idle tick thread crashes | Stale mtime → false orphan     | Acceptable; rare, surfaces as :ghost:     |
| Workspace volume unmounted              | `dispatch_dir` missing         | Existing `workspace_gone` path            |
| Clock skew between containers           | N/A — kernel mtime is shared   | Not applicable                            |

## Rollback

Single-file revert. The supervisor change is one branch swap
(`heartbeat_stale` → `pid_alive`). The babysit's idle-tick writer is
additive — leaving it in place after a revert is harmless (writes a
file nobody reads). No data migration, no schema change, no compose
edit.

If we observe a regression in production:

1. Revert the supervisor commit.
2. Keep the babysit commit (harmless).
3. We're back to the pre-#172 behaviour — false orphans return, but
   nothing else breaks.

## What this does NOT do

- Does not move toward shared PID namespaces, container privilege
  changes, or a Docker dependency in the router.
- Does not change `dispatch_cancel` (#171) — the kill ladder still
  uses `os.killpg` from inside the agent container where the pid
  resolves correctly.
- Does not solve the #159 (D-6) janitor's "is this stale workspace
  safe to move?" question. That's a separate concern (mtime-based
  age-out vs liveness) and the janitor can adopt the same heartbeat
  signal in a follow-up.

## Open questions for Bram

1. **`HEARTBEAT_STALE_SECONDS = 45`** — comfortable with the default,
   or want it tighter (15-20s for snappier orphan detection at the
   cost of more false positives on slow tool calls)?
2. **Babysit idle-tick implementation** — `asyncio.wait_for` on
   stdout vs a separate `threading.Timer`. The former keeps the
   babysit single-threaded; the latter is simpler to reason about.
   My recommendation: `asyncio.wait_for`, because the babysit is
   already async-shaped.
3. **Deprecation of `pid_alive`** — delete it in the same PR (it has
   no other callers today) or leave it with a `DeprecationWarning`
   for a release? My recommendation: delete in same PR.
