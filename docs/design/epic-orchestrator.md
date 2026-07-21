# Epic orchestrator — auto feature-work behind staged flags

*Design date: 2026-07-21 · Owner: Sam (TL) · Approver: Bram · Status: Stage 0 — design-first, corrected. Nothing dispatches, merges, or deploys under this layer until Bram signs off.*

Automate feature-work the way we automated bug-fixes: point at an epic → dispatch
its sub-issues in dependency order → review → (eventually, behind its own gate)
merge + deploy. Reuses the per-issue bug primitive; adds only the epic layer, all
behind default-off flags, one gate flip at a time.

Filed as part of #751 (Epic: Auto feature-work orchestrator). This 1-pager lands
with the first impl PR, #753.

## Reuses cleanly (no change)

`dispatch.draft` → worker PR → `aidt-tl-sam` review; CI gate; $50/5h spend gate;
existing identities.

## Genuinely new

1. **Sub-issue DAG** — none exists today; auto-dispatch runs a flat bug backlog
   (`router/auto_dispatch/github.py` FIFO). Build: read epic sub-issues + edges,
   gate a child until every parent PR is **merged to main** (verified via `gh`).
2. **Merge-gate collision.** `merge_queue._is_pr_approved`
   (`router/merge_queue.py:108-116`) merges any PR with a non-author approving
   review OR the `auto-merge` label. Per the ratified merge bar (#752) that is
   **intended** for normal PRs — but it means the instant Sam approves a
   *feature* PR it merges, ignoring DAG order. Feature PRs require Sam's review
   → collision is unavoidable without a carve-out.
3. **Deploy** — stays Bram.

## Prerequisite fix (ships with Stage 1, #753)

**[CORRECTED 2026-07-21]** The original framing here ("honour
`multi_file_threshold` + deny-list") is **obsolete**: the file threshold was
ripped out end-to-end in the #752 follow-up, and a deny-list gate at merge-time
would contradict the ratified merge bar. The real prereq is **only**: exclude
`epic:*`-labelled PRs from auto-merge **unless** an explicit `epic-auto-merge`
label is present. Reversible, label-driven. Normal PRs unchanged. Retroactively
removes the DAG-ordering hazard for epic PRs.

Implementation (this PR): `merge_queue._is_pr_approved` now returns `False` for
any PR carrying an `epic:*` label — including the `auto-merge` fast-path —
unless the PR also carries `epic-auto-merge` (added later by the Stage-3 gate,
#757). A human can still merge such a PR directly via the GitHub UI; this gate
only governs the automated `merge_queue` tick.

## Staged rollout (mirrors auto-bug: flags default-off, one flip at a time)

| Stage | ORCHESTRATOR | dispatch | merge | deploy | Bram's role |
|---|---|---|---|---|---|
| 0 (now) | off | — | — | — | **approve this design** |
| 1 | on | per-issue approval card | Bram hand-merge | Bram | approves every dispatch + merge |
| 2 | on | auto (DAG-gated, ≤1/hr) | Bram hand-merge | Bram | approves merges only |
| 3 | on | auto | `epic-auto-merge` gate (own card) | Bram | approves deploy |
| 4 | on | auto | auto | auto | monitors |

Flags (`router/settings.py` `Setting()`, default False, hot-reload — cf.
`DISCORD_ENABLED`): `EPIC_ORCHESTRATOR` (master), `EPIC_AUTO_DISPATCH` (S2),
`EPIC_AUTO_MERGE` (S3), `EPIC_AUTO_DEPLOY` (S4). Loop tunables in
`config/epic.yaml`. Each reversible by one config line + redeploy.

## DAG source — decision: (a), proceeding pending Bram's final nod

- **(a) Task-list + `Depends-on:` lines** — parse epic body `- [ ] #123`, read
  `Depends-on: #x` per sub-issue. No new dep. GitHub = source of truth.
  **Chosen (reversible).**
- (b) Committed manifest `epics/<n>.yaml` — most auditable, but duplicates GH and
  drifts.

## Runtime deps

None new. Existing `gh` CLI, dispatch/review/merge_queue machinery, existing
PATs.

## Permission boundaries

Stages 1–2 add no new act-surface (merge + deploy stay human). Stage 3 is the
real boundary flip → own approval card + prerequisite merge-gate fix (#753) live
first. Identities unchanged; never push-and-approve with one identity.

## Health / smoke

Local: base-image build + DAG builder on a fixture epic (parents-before-children;
child held while parent PR open). Post-deploy: router log shows DAG gate
deferring children with reason `parent_pr_open`, ≤1/hr.

## Failure modes

Child-before-parent (→ live `gh` merged-check, bias-to-hold); DAG cycle
(→ detect + refuse); feature PR auto-merges on review (→ prerequisite fix #753);
runaway (→ same 12/day, 1/hr, $50/5h caps); cross-PR drift (→ v1 proves ordering
+ review on ONE real epic before any auto-merge).

## Rollback

Flip the stage flag off + redeploy (one line, durable via commit). Master off
disables the layer; bug flow untouched.

## Impl sub-issues (filed 2026-07-21, DAG order)

- [x] **#753 — A. Merge-gate: exclude `epic:*` PRs from auto-merge unless `epic-auto-merge`** *(no deps; unblocks C, E)*
- [ ] **#754 — B. Sub-issue DAG builder** (parse epic + `Depends-on:` edges, cycle-detect, parent-merged gate) *(no deps)*
- [ ] **#755 — C. Epic orchestrator loop (Stage 1, `EPIC_ORCHESTRATOR`)** *(depends: #753, #754)*
- [ ] **#756 — D. Stage 2 `EPIC_AUTO_DISPATCH`** *(depends: #755 + pilot)*
- [ ] **#757 — E. Stage 3 `EPIC_AUTO_MERGE` + `epic-auto-merge` gate** *(depends: #756, #753)*
- [ ] **#758 — F. Stage 4 `EPIC_AUTO_DEPLOY`** *(depends: #757)*
