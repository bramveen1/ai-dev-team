# Epic orchestrator — auto feature-work behind staged flags

*Design date: 2026-07-20 · Owner: Sam (TL) · Approver: Bram · Status: DRAFT — awaiting sign-off on the DAG layer. Merge/deploy model settled (Bram, 2026-07-20): the current automated path — Sam review + green CI → merge → auto-deploy — is the intended setup for feature work.*

Trips Design-First Triggers: **new runtime contract** (sub-issue DAG + parent-merged
gate) and **2nd instance of a pattern** (auto-bug → auto-feature). 1-pager before
code, per policy. This is **not** a new permission boundary — per Bram's 2026-07-20
decision, multi-file/boundary PRs already merge on the standing bar (Sam review +
green CI) and auto-deploy via the pull daemon; the epic layer adds ordering on top
of that unchanged path, it does not widen the merge surface.

## Goal

Point at a GitHub epic → the system dispatches its sub-issues in dependency
order, reviews the PRs, and (eventually, behind its own gate) merges + deploys.
Reuse the per-issue bug primitive; add only the epic layer on top.

## What reuses cleanly (no change)

- `dispatch.draft` → worker PR → `aidt-tl-sam` review (`packs/dispatch/handler.py`, `pr_review.py`).
- CI gate, spend gate (`config/dispatch.yaml quota`), review identity flow.

## What is genuinely new (the epic layer)

1. **Sub-issue DAG.** No epic/sub-issue/dependency code exists today — the
   auto-dispatch loop (`router/auto_dispatch/`) operates on a *flat* bug
   backlog. We must build: (a) read an epic's sub-issues, (b) read edges
   ("contract issue before implementers"), (c) a gate that will not dispatch a
   child until every parent's PR is **merged to main** (verified via `gh`, not
   assumed).
2. **Merge — reuses the standing path unchanged (no new work).**
   `merge_queue._is_pr_approved` (`router/merge_queue.py:108-116`) merges any PR
   with **a non-author approving review** (Sam's `aidt-tl-sam`) plus
   `mergeable_state: clean` + green CI. It does not consult file-count or the
   deny-list — and per Bram's 2026-07-20 decision **that is the intended bar for
   feature PRs too**. The earlier read of #749 / #712 / #740 as "auto-merge
   anomalies" is retired: those were the system behaving as designed. Feature PRs
   merge exactly like bug PRs — Sam reviews, CI must be green, then it merges.
3. **Deploy — already automated (no new work).** Every merge to `main`
   auto-deploys within ~2 min via the pull daemon (`docs/cd-deployment.md`) with a
   health check + auto-revert. There is no separate human deploy step to build.

## No merge-gate change needed

An earlier draft proposed closing a "merge-gate hole" so review ≠ auto-merge for
large/boundary PRs. Per Bram's 2026-07-20 decision that is **not** wanted: robust
CI + a Sam review is the intended merge bar for feature PRs, same as bugs. The
epic layer therefore adds **only** dependency-ordered dispatch; merge and deploy
stay on the existing automated path, untouched.

## Staged rollout (mirrors auto-bug: flags, default-off, one gate flip at a time)

New flags follow the established pattern: `Setting()` entries in
`router/settings.py` (`default=False`, `reload="hot"`, category "Features" — cf.
`DISCORD_ENABLED` L255, `SLACK_VIA_ADAPTER` L264), read via `settings.get(...)`.
Epic-loop tunables (rate/caps) live in a `config/epic.yaml` block mirroring
`config/dispatch.yaml`. Every flag default-off.

| Stage | `EPIC_ORCHESTRATOR` | dispatch | merge + deploy | Bram's role |
|---|---|---|---|---|
| 0 (now) | off | — | — | **approve this design** |
| 1 | on | **per-issue approval card** per sub-issue | standing path (Sam review + CI → merge → auto-deploy) | approves every dispatch |
| 2 | on | auto (DAG-gated, ≤1/hr) | standing path (unchanged) | monitors |

Merge and deploy are the existing automated path at **every** stage — the only
thing the flags gate is whether sub-issue *dispatch* is carded or automatic,
mirroring how auto-bug rolled `require_always` from true → false.

Flags: `EPIC_ORCHESTRATOR` (master), `EPIC_AUTO_DISPATCH` (Stage 2). Each
reversible by one config line + redeploy (durable via commit; in-container wiped).

## DAG source — decision needed (pick one)

- **a) Task-list + `Depends-on:` lines.** Parse epic body `- [ ] #123`; read a
  `Depends-on: #x` line in each sub-issue body for edges. No new dep (uses
  `gh_cli.py`). Single source of truth = GitHub. **Recommended.**
- **b) Committed manifest `epics/<n>.yaml`.** Explicit DAG in repo, most
  auditable, but duplicates GitHub and drifts.

Default with no declared edges = linear by list order (safe, conservative).

## Runtime deps

None new. Uses existing `gh` CLI (`packs/dispatch/gh_cli.py`), existing dispatch
+ review + merge_queue machinery, existing PATs. No new library/service/binary.

## Permission boundaries

- **No new act-surface at any stage.** Merge and deploy already run on the
  standing automated path (Sam review + CI → `merge_queue` → pull-daemon deploy);
  the epic layer only sequences *dispatch*. This is why it isn't a new permission
  boundary despite touching feature-sized PRs.
- Identities unchanged: push `aidt-dev-worker`, review `aidt-tl-sam`, merge
  `aidt-merge`. Never push-and-approve with one identity (standing rule).
- The one change class still worth a deliberate human eye is the deploy machinery
  itself (`scripts/deploy-pull.sh`, systemd units) — CI can't exercise the deploy
  cycle and a broken deploy script can't self-revert.

## Health / smoke probe

- Local smoke before PR: base-image build + exercise the DAG builder on a fixture
  epic (parents-before-children ordering, child held while parent PR open).
- Post-deploy: router log shows `epic_orchestrator` reading sub-issues, honouring
  the DAG gate (child dispatch deferred with reason `parent_pr_open`), ≤1/hr cap.

## Failure modes

- **Child dispatched before parent merged** → wrong-base PR / merge conflicts.
  Mitigation: parent-merged check reads live `gh` PR state, biased-to-hold.
- **Cycle in DAG** → deadlock. Mitigation: detect cycles at build, refuse + report.
- **A merged feature PR breaks the running stack** → the pull daemon's health
  check + auto-revert (`docs/cd-deployment.md`) is the backstop; `git revert` on
  `main` is the durable rollback.
- **Runaway dispatch** → same 12/day, 1/hr, $50/5h caps apply to the epic layer.
- **Cross-PR inconsistency** (impl PR assumes a contract that changed) → v1 proves
  ordering + review on ONE real epic before any auto-merge.

## Rollback

Any stage: flip its flag off + redeploy (one line, durable via commit). No data
migration. Master `EPIC_ORCHESTRATOR=off` disables the whole layer; the bug flow
is untouched.

## Resolved (2026-07-20)

The #749/#712/#740 "auto-merge anomaly" is resolved as **intended behavior**, not
a hole to fix: per Bram, robust CI + a Sam review is the merge bar for feature PRs,
and the pull daemon's health-check/auto-revert covers deploy. No merge-gate change
ships with this epic; the remaining open work is the DAG/ordering layer only.
