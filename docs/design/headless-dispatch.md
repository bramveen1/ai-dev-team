# Headless Dev Agent Dispatch — Design 1-pager

*Status: draft, pending Bram sign-off · Owner: Sam · Related: PRD (Notion)*

## Goal

Let Sam dispatch a GitHub issue to a headless Claude Code session ("the dev
agent"), receive the resulting PR back in Slack, review it, and iterate —
without Bram in the inner loop on every cycle.

## Non-goals (v1)

- Multiple personas. Ship `dev` only. Registry shape is locked so
  `security-review` / `docs` are config commits later.
- Per-agent GitHub identity. The dev agent pushes as Bram for v1. Tracked
  in the existing tech-debt thread.
- Mid-session resume. v1 always dispatches fresh on iteration ("address
  review on PR #N"). Resume-vs-fresh decision revisited after pilot.
- iOS / mobile dispatch surface. Slack-from-laptop only.

## Architecture

### Dispatch path

1. Sam invokes `dispatch.dispatch_issue(issue_url, persona="dev")`.
2. Verb fetches issue body + acceptance criteria via `gh`, loads
   `config/personas/dev/{system_prompt.md, allowlist.yaml}`, formats the
   task prompt.
3. Verb emits a lightweight approval card (see *Approval gating*) for the
   first 5 dispatches; auto-approves after the gate is flipped.
4. On approval, verb runs the bridge command (see *Bridge*) and returns
   `{task_id, monitor_url}`.
5. Headless session runs autonomously on the cloud VM, opens a PR against
   the repo's main branch.

### Notification path

1. GitHub webhook (`pull_request.opened`, labeled `headless-dispatch`) →
   router endpoint `/webhooks/github`.
2. Router pings Sam in the dispatch thread: PR link + diff summary.
3. Sam reviews (see *Review*). LGTM → requests Bram as second reviewer.
   Comments → re-dispatch with "address review on PR #N".

### Escalation

- Sam classifies stuck-state from agent output: `code` vs `product`.
- `code` → Sam iterates with the agent (re-dispatch).
- `product` → Sam DMs Dave with the question + PR link.
- Dave → Bram only when Dave can't resolve.

## Bridge (the hard part — gated on the spike)

Two candidates, decided by the spike issue before any further code:

- **Candidate A — `claude --remote -p <prompt> --output-format json`**
  from Sam's container. Anthropic-hosted VM, clones repo from GitHub, runs
  on Bram's Max sub. claude.ai cloud agents already have GitHub creds —
  spike confirms PR-opening identity end-to-end.
- **Candidate B — headless `claude` in a sibling Docker container**, repo
  checked out into a workspace volume, draws from the same Max sub.
  Heavier but no dependency on cloud-VM behavior.

Spike output is a known-good invocation recipe + a short write-up of what
each candidate costs us in portability, quota, and identity terms.
**No production code lands before the spike report.**

## Auth (v1 — simplest thing)

- Mount Bram's existing local Claude credentials into Sam's container
  read-only, exactly as `gh` auth is mounted today. The agents "run as
  Bram" against Anthropic. No new secrets infrastructure.
- **Phase 2** (tech-debt issue, not blocking): move credential to
  age-encrypted blob under `/config/secrets/anthropic/`, mounted only into
  the dispatch sidecar. Same pattern as `browser_use`.
- Decision: don't spend cycles on auth design now. Get E2E working, then
  harden.

## Permission boundary

- **New:** Sam's container can initiate a cloud session on Bram's
  Anthropic account. That's the new boundary.
- **New:** cloud VM pushes to GitHub as Bram (inherited via the cloned
  remote).
- **Unchanged:** Sam still can't merge — PR merge stays approval-gated
  via the existing `github` pack.
- Persona `allowlist.yaml` is advisory in v1 (cloud VM doesn't enforce
  it). Enforcement at the dispatch layer is a Phase-2 tech-debt issue.

## Approval gating

- First 5 dispatches: `dispatch_issue` emits a `draft-approval` card
  showing `{issue_url, persona, prompt_preview}`. Bram approves in Slack,
  dispatch fires.
- After 5 clean runs (Bram's call): config flag
  `dispatch.approval_required = false` flips it off. Sam dispatches
  autonomously.
- Re-dispatches for review iteration are **never** gated — too much
  friction.
- `dispatch_issue` for a non-`dev` persona stays gated indefinitely until
  that persona's pilot completes.

## Sam's review checklist

Committed to `config/agents/sam/review_checklist.md`, loaded into Sam's
persona on every PR-opened ping:

1. CI green (all required checks).
2. PR body maps to issue acceptance criteria item-by-item, explicitly.
3. No files outside the scope implied by the issue.
4. No new dependencies undeclared in the issue.
5. No skipped or `@pytest.mark.skip`'d tests.
6. **Plus a full PR review** — Sam reads the diff and posts review
   comments, not just a checklist pass. This is the "you often find issues
   that make the work better" requirement.

## Health / smoke probe

- `dispatch.health` verb pings the bridge: dry-run a tiny prompt against
  a throwaway test issue, expect `{task_id}` back within 30s.
- Run on container start; fail loud if the bridge isn't reachable.
- Webhook endpoint has a `/webhooks/github/health` returning 200 if router
  can reach GitHub API.

## Failure modes & rollback

- **Bridge unreachable** → `dispatch_issue` returns a structured error,
  Sam Slacks Bram, no PR opened. No retry loop.
- **Cloud session hangs / quota exhausted** → no PR-opened webhook fires.
  Sam's stuck-detection (#112 guards) catches it on his side; he reports
  back.
- **PR opens but CI red** → Sam treats it as a normal iteration: post
  review comments, re-dispatch.
- **PR opens and merges something destructive** → can't happen; merge
  stays gated.
- **Rollback for the whole feature**: revert the `dispatch` pack and
  remove `dispatch` from agent manifests. No data migration, no state to
  unwind. Webhook handler stays — it's harmless without dispatches.

## Issue sequence (numbered by dependency order)

- **#A** — This 1-pager, committed to `docs/design/`. Locks the contract.
- **#B** — Spike: headless invocation, both candidates, identity
  confirmation. Blocks everything below.
- **#C** — Persona registry skeleton (`config/personas/dev/`).
- **#D** — `dispatch` pack v1 with `dispatch_issue`, approval-gated,
  bridge from #B.
- **#E** — GitHub webhook → Sam Slack ping.
- **#F** — Sam's review checklist committed and wired into persona.
- **#G** — Pilot: 5 dispatches over 2 weeks, retro issue at the end.

### Tech-debt follow-ups (filed alongside, not blocking)

- Phase-2 auth (age-encrypted Anthropic creds).
- Persona allowlist enforcement at dispatch layer.
- Resume-vs-fresh-dispatch decision after pilot data.
- Dev-agent GitHub identity.
