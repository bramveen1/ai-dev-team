# Dispatch post identity — who speaks for what

**Status:** implemented (fixes #270)
**Epic:** #253
**Builds on:** #269/#270 (worker-origin post identity), #252 (workers-bot allowlist auto-seed)

## Problem

`WORKERS_BOT_TOKEN` (issue #269/#270) gave the **worker's own posts** and the
**slot tracker** a single runtime identity — they render as `U0B8SG0GUQN`
("ai-dev-team-workers"). But the **router-side lifecycle acks** still go out
through whichever agent's `AsyncApp` happened to handle the approval click or
own the supervision system task. So messages that are *about a dispatch* —

- `:rocket: dispatch … launched (approved)`
- `:white_check_mark: dispatch … done (exit 0) · <@AGENT>`
- `:x: Approved draft … missing issue_url; nothing executed.`

— render as the agent persona (e.g. Sam), and the done-post `@`-mentions the
agent. The agent's self-mention guard correctly drops the ping, so the thread
ends with the runtime telling the agent "you're done", the agent ignoring
itself, and the operator unable to tell runtime-emitted posts from
agent-emitted ones.

Observed in thread `1779436625.364079` during smoke dispatch
`dispatch-20260607T123203-136502` on 2026-06-07.

## Identity invariant

| Post class                                   | Speaks as     | Emitting client            |
| -------------------------------------------- | ------------- | -------------------------- |
| Slot tracker / worker echoes                 | workers bot   | pack handler (already #270)|
| Approval card render                         | agent persona | agent bolt client (kept)   |
| Dispatch lifecycle ack (launched/done/error) | workers bot   | `_workers_client()`        |
| Supervision delta / terminal / kill / timeout / orphan / quota warn | workers bot | workers bot (this change)  |
| Auto-review handoff (wakes the agent)        | workers bot   | workers bot (this change)  |
| Agent prose — 1-pagers, reviews, drafts      | agent persona | agent bolt client (kept)   |

The rule: **the workers bot speaks for anything reporting *on a dispatch*; the
agent personas speak only as themselves.**

## Post-site inventory

Producers migrated to the workers bot client:

1. **`router/app._execute_approved_draft`** — the `dispatch_issue` branch
   (the `docker exec … --supervision-mode poll` lane). Every ack in that
   branch is a dispatch lifecycle post: `missing issue_url`, `unknown agent`,
   `execution failed`, `non-JSON`, and the terminal
   `launched / done / failed / error` envelope. These now post via
   `_workers_client()`, falling back to the agent `client` when
   `WORKERS_BOT_TOKEN` is unset.

2. **`router/dispatch/supervision.check_dispatch`** — every post the
   supervisor emits (delta line, terminal summary, killed, timeout, orphan,
   quota heads-up, auto-review handoff) now routes through the workers bot.

   The identity choice lives at the **scheduler's resolver seam**, not inside
   `check_dispatch`. `supervision` stays identity-agnostic: it posts through
   whatever `slack_client` it is handed and never constructs one of its own.
   The scheduler grew a `system_client_resolver` (`run_forever` → `run_once` →
   `run_task` → `_run_system_task`); `router.app` wires it to
   `_system_task_client = _workers_client() or _client_for_agent(agent)`. So
   the dispatch-supervision **system task** posts as the workers bot, while
   **agent (cron) tasks** keep posting as their own agent via the regular
   `client_resolver`.

   This placement matters: an earlier draft built the workers client *inside*
   `check_dispatch` off `WORKERS_BOT_TOKEN`. That bypassed the injected-client
   seam — the supervision smoke test (and any caller that hands in a mock)
   would have its mock ignored and a real Slack call attempted whenever the
   token was present in the env (it is, via the integration conftest). Keeping
   the client a pure injection point keeps the seam intact and the smoke test
   honest.

Deliberately **left on the agent client** (out of scope):

- The approval card itself — it is *about a request the agent made*
  (`post_approval_message`).
- The agent CLI re-entry path's prose reply (the agent's own confirmation
  after a non-dispatch draft like `gh pr merge`) and any re-draft cards /
  parse-error surfacing — agent-emitted content stays agent-identity.

## Mention-target rule for the done-post

The supervision terminal summary used to append `<@AGENT>`. Now that the post
speaks as the *workers bot* — which is on the dispatch-bot allowlist (#252) —
keeping that mention would actively wake the agent on every "done / killed /
timed out / orphaned" line, turning a status update into a runtime→agent loop
(the very thing the agent's self-mention guard used to absorb).

So the terminal, killed, timeout, and orphan posts **drop the agent mention
entirely**. The requester (who clicked Approve) is not carried in the
supervision payload, so per the issue's acceptance criterion we drop rather
than re-target. These read as plain runtime status lines.

The **auto-review handoff keeps its mention** — that post *exists* to wake the
agent for review (#173/#207). Emitted by the workers bot (an allowlisted
sender), the `<@AGENT_USER_ID>` ping now lands instead of being self-dropped,
which is the intended handoff.

## Test strategy

Driven through the real producers (per #265's schema-drift rule — no
hand-crafted message dicts):

- `_execute_approved_draft` with `WORKERS_BOT_TOKEN` set: assert the launched /
  error acks post through a client constructed with `token=WORKERS_BOT_TOKEN`,
  not the agent `client`. With the token unset, the existing assertions on the
  agent `client` still hold (fallback path).
- The scheduler routes system tasks through `system_client_resolver` when
  provided, else falls back to `client_resolver` — asserted in
  `tests/unit/scheduled_tasks/test_scheduler.py`. `router.app._system_task_client`
  prefers the workers client when the token is set and falls back to the agent
  client otherwise.
- `check_dispatch` posts through whatever client it is handed and builds none
  of its own (even with the token present) — asserted in
  `tests/unit/dispatch/test_supervision.py::TestPostsThroughInjectedClient`.
- Terminal / killed / timeout / orphan summaries no longer contain `<@…>`; the
  auto-review handoff still mentions the agent user id. The supervision smoke
  test drives the full pipeline with the Slack client as its only seam.

## Rollback

Per-site and independent. Unsetting `WORKERS_BOT_TOKEN` reverts every migrated
site to the agent client automatically (the fallback path), so a bad rollout is
a single env change, not a code revert.
