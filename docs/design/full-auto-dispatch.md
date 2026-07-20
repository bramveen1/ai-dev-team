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

## What holds the line — two distinct layers

There are two independent gates. It's worth being precise about which one does
what — the original draft of this note conflated them.

### Layer A — dispatch selection (which issues auto-fire *unattended*)

These live in `router/auto_dispatch/triage.py` and decide whether the loop picks
an issue up **without a human dispatching it**, and whether the resulting PR
takes the auto-label path. They never touch merge:

1. **Triage deny-list** — auth, billing, DB migrations, deploy/CI config,
   secrets → held (these are the 1-pager-class changes). Runs over the issue
   metadata pre-dispatch and over the PR diff post-dispatch.
2. **Smart gate** (active when `require_always:false`) — destructive
   keywords (`destructive`/`delete`/`drop`/`migration`/`reset`) → held.
3. **Spend gate** — >$50 / 5h window → held.

**File count is not a gate at any layer.** Blast radius is judged by *what* a
diff touches (the deny-list), not *how many* files — a large, non-sensitive diff
takes the same path as a small one, because a Sam review + green CI (Layer B) is
the real safety net regardless of size.

### Layer B — the merge gate (what actually merges *any* PR)

`merge_queue._is_pr_approved` + the mergeability check merge a PR when **all** of
these hold. It does **not** consult file-count or the deny-list:

5. **A non-author approving review** (Sam's `aidt-tl-sam`) OR the `auto-merge` label.
6. **`mergeable_state: clean`** — no conflicts, branch not behind.
7. **All five required CI checks green.**

> **Decision (2026-07-20, Bram):** the merge bar is Sam's review + green CI,
> **regardless of file count or blast radius**. CI is robust and a Sam review is a
> real review, so multi-file and boundary PRs merge on the same bar as anything
> else. This is intentional — the earlier "≤1-file at merge" framing was
> aspirational and never matched the code. File count is **not** a gate at any
> layer: the former `multi_file_threshold` (which only ever gated the post-PR
> auto-label decision, never dispatch selection or merge) was removed — the
> deny-list guards the sensitive classes whatever the diff size.
>
> Deploy is covered downstream by the pull daemon's health-check + auto-revert
> (`docs/cd-deployment.md`), not by a human merge tap. The one class still worth a
> deliberate human eye is a change to the **deploy machinery itself**
> (`scripts/deploy-pull.sh`, systemd units): CI can't exercise the deploy cycle and
> a broken deploy script can't self-revert.

## Failure modes & detection

- **Bad fix merges to main** — caught by CI gate (won't merge red) + Sam
  review; residual risk is a logic bug CI doesn't cover. The pull daemon's
  health check + auto-revert is the backstop if a merged change breaks the
  running stack.
- **Runaway dispatch / cost** — capped at 12/day, 1/hr, plus $50/5h spend
  gate. Detect via router logs (`ops-diag`) and merge-queue activity.
- **Gate misconfig** (e.g. deny-list regression) — would widen the safe
  path silently; mitigated by keeping these brakes in committed config,
  reviewable in diff.

## Health / smoke probe

Watch after deploy: router log shows `auto_dispatch` ticks honouring the
new `rate_per_hour: 1` cap; auto-merges appear in merge-queue logs under
`aidt-merge` once CI is green and a Sam review (or the `auto-merge` label) lands.

## Rollback

One-line revert: `approval.require_always: false → true` and re-deploy
(durable via repo commit; in-container copy is wiped on redeploy). Caps
can revert independently. Fully reversible, no data migration.

## Notes

- `rate_per_hour: 1` is the hard cadence cap; the scheduled tick still
  polls every 30 min but won't exceed 1 dispatch/hr. Making the literal
  tick 60 min is cosmetic (needs the scheduled task re-registered) and is
  not done here.
