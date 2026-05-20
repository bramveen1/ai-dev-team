# Dispatch supervisor kill channel

**Status:** implemented (fixes #213)  
**Decision:** Approach B — halt-marker as the only signal channel

## Problem

`supervision.py`'s halt and budget-exceeded paths called `os.killpg(pid, SIGTERM)` from
the **router container's PID namespace**. The babysit subprocess runs in the **agent
container's PID namespace**. The two namespaces do not share PIDs, so the signal either
no-ops or lands on an unrelated process in the router container — the same root cause
that PR #172 fixed for the discovery-loop `kill -0` liveness check.

The supervisor then posted a confident "killed" / "timed out" Slack message and wrote a
synthetic exitcode, making the dispatch *look* dead while the babysit continued consuming
tokens in the agent container.

## Fix — marker-file signal channel

The supervisor writes a file into the shared dispatch workspace volume (already mounted
r/w by both containers) instead of sending a cross-namespace signal:

- **halt path** (`/kill`): `halt_marker` is written by `kill_command.py` (unchanged).
  Supervisor detects it, writes `cancel_reason=stuck_guard_kill`, and waits up to 60 s
  for babysit to write its own exitcode.
- **budget-exceeded path**: Supervisor writes a new `timeout_marker` file, writes
  `cancel_reason=runtime_timeout`, and waits up to 60 s for babysit's exitcode.

Babysit polls for both markers every `HEARTBEAT_INTERVAL` (15 s) in a background
thread. On detection it calls `proc.terminate()` on its own child process — valid
because babysit and the `claude -p` subprocess share the same PID namespace (both run
inside the agent container). The main `run()` loop then writes the real exitcode and
exits normally.

If babysit does not respond within 60 s (unresponsive or already dead), the supervisor
falls back to `_write_synthetic_exitcode_if_absent` as before, preserving the
state-before-cleanup invariant from #171.

## Runtime contract change for babysit

| Before | After |
|--------|-------|
| Babysit ignores halt/timeout markers; relied on SIGTERM from router | Babysit polls `halt_marker` and `timeout_marker`; self-terminates child on detection |
| Supervisor sends `os.killpg` (cross-namespace, broken) | Supervisor writes marker file; waits for babysit's exitcode |
| "killed" / "timed out" posted immediately, before confirmation | Posted after exitcode confirmed (or hard 60 s fallback) |

## Not changed

- `dispatch_cancel` (`docker exec` from #171): left as-is; separate cleanup tracked
  elsewhere.
- Heartbeat-based orphan detection (path 4): already namespace-safe via mtime check.
- Slot release and `_write_synthetic_exitcode_if_absent` race protection from #171/#211.
