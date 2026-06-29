# Full-auto dispatch (require_always → false)

*Decision date: 2026-06-29 · Owner: Sam (TL) · Approver: Bram*

## What changed

`config/dispatch.yaml`:

| Key | Before | After |
|---|---|---|
| `approval.require_always` | `true` | `false` |
| `auto_dispatch.rate_per_hour` | `2` | `1` |
| `auto_dispatch.daily_cap` | `6` | `12` |

This trips a Design-First Trigger (**safety boundary**), so this note ships
with the commit per standing policy.

## What "full auto" now means

With `require_always: false`, the auto-dispatch loop runs the full chain
**unattended**: detect bug → dispatch worker → fix → open PR → label
`auto-merge` → `merge_queue.py` squash-merges to `main` under `aidt-merge`.
No human tap on the safe path. Up to 12 such cycles/day, ≤1/hour.

## Permission boundary

No new identity or scope. Merge continues under existing `aidt-merge`.
Dispatch under existing dispatch path. Net change is *who decides to
proceed* (now the smart gate, not a human card), not *what can act*.

## What still holds the line (the brakes that survive the gate flip)

The safe path is deliberately narrow. A change auto-merges ONLY if ALL hold:

1. **`multi_file_threshold: 1`** — any diff touching >1 file → held for human.
2. **Triage deny-list** — auth, billing, DB migrations, deploy/CI config,
   secrets → held for human (these are the 1-pager-class changes).
3. **Smart gate** (active when `require_always:false`) — destructive
   keywords (`destructive`/`delete`/`drop`/`migration`/`reset`) → held.
4. **Spend gate** — >$50 / 5h window → held.
5. **Sam review verdict must pass.**
6. **All CI checks green + `mergeable_state: clean`.**

So full-auto only ever touches small (≤1 file), non-sensitive, reviewed,
green changes. Anything with real blast radius still stops for a human.

## Failure modes & detection

- **Bad fix merges to main** — caught by CI gate (won't merge red) + Sam
  review; residual risk is a logic bug CI doesn't cover, scoped to ≤1 file.
- **Runaway dispatch / cost** — capped at 12/day, 1/hr, plus $50/5h spend
  gate. Detect via router logs (`ops-diag`) and merge-queue activity.
- **Gate misconfig** (e.g. deny-list regression) — would widen the safe
  path silently; mitigated by keeping these brakes in committed config,
  reviewable in diff.

## Health / smoke probe

Watch after deploy: router log shows `auto_dispatch` ticks honouring the
new `rate_per_hour: 1` cap; first auto-merge appears in merge-queue logs
under `aidt-merge` with the `auto-merge` label, on a ≤1-file PR only.

## Rollback

One-line revert: `approval.require_always: false → true` and re-deploy
(durable via repo commit; in-container copy is wiped on redeploy). Caps
can revert independently. Fully reversible, no data migration.

## Notes

- `rate_per_hour: 1` is the hard cadence cap; the scheduled tick still
  polls every 30 min but won't exceed 1 dispatch/hr. Making the literal
  tick 60 min is cosmetic (needs the scheduled task re-registered) and is
  not done here.
