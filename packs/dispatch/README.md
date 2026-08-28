# dispatch pack

Headless `claude -p` dispatcher for Sam. Takes a GitHub issue URL,
spins up an isolated workspace under `/var/lib/dispatch/`, runs a
fresh Claude Code session that opens a PR, and reports the result
back into the originating Slack thread.

The full design lives in [`docs/design/dispatch-pack.md`](../../docs/design/dispatch-pack.md).
This README only covers what ships in the v1 scaffold (#D-1) and how
to smoke-check it.

## Planned verbs

| Verb              | Status       | Lands in | Purpose                                                  |
| ----------------- | ------------ | -------- | -------------------------------------------------------- |
| `dispatch_health` | implemented  | D-1      | smoke probe — cli, workspace, Sonnet round-trip          |
| `dispatch_issue`  | implemented  | #163     | spawn a headless `claude -p` and return launched <3s     |
| `dispatch_status` | not yet      | D-2      | read latest JSON event for an in-flight `dispatch_id`    |
| `dispatch_cancel` | not yet      | D-4      | SIGTERM the process group, tear down workspace          |

Concurrency (#D-3), quota telemetry (#D-5), the janitor (#D-6), and
approval gating (#D-7) follow in their own issues.

## `dispatch_issue` (poll-based supervision, #163)

Launches a `claude -p` subprocess via a detached **babysit** sidecar
(see [`babysit.py`](babysit.py)) and returns immediately. Wallclock
target: <3s. Supervision is router-side polling — the handler does NOT
block on completion.

```bash
python /config/packs/dispatch/handler.py dispatch_issue \
  --issue-url https://github.com/o/r/issues/42 \
  --channel C123456 \
  --thread-ts 1700000000.000100 \
  --agent sam \
  --budget-seconds 1800 \
  --model sonnet
```

Returns JSON:

```json
{
  "status": "launched",
  "dispatch_id": "dispatch-20260517T143022-a3f9c1",
  "workspace": "/var/lib/dispatch/dispatch-20260517T143022-a3f9c1",
  "pid": 12345,
  "budget_seconds": 1800,
  "model": "sonnet",
  "persona": "dev"
}
```

The router-side supervisor (`router.dispatch.supervision:check_dispatch`)
polls every ~120s, posts deltas to the originating Slack thread, and
posts the terminal summary tagged with `<@<agent>>` so the launching
agent's next turn sees the outcome in context. See
`docs/scheduled-tasks.md` ("System Tasks") for how the polling task is
registered.

For tests and smoke probes, pass `--exec` to override the `claude -p`
command:

```bash
python /config/packs/dispatch/handler.py dispatch_issue \
  --issue-url https://example.com --channel C1 --thread-ts 1.0 --agent sam \
  --exec sleep 30
```

## Liveness

v1 supervision tracks process liveness via a heartbeat file that the dispatch
process writes on a 15 s cadence. The router treats a heartbeat stale for >45 s
as evidence the process is gone; cross-namespace `kill -0` is unreliable and
was the source of false-orphan detections fixed in #172.

## `dispatch_health`

Returns four fields the operator cares about:

| Field                       | Type            | Meaning                                                    |
| --------------------------- | --------------- | ---------------------------------------------------------- |
| `cli_version`               | str             | `claude --version` output                                  |
| `claude_path`               | str             | Absolute path to the resolved `claude` binary              |
| `workspace_volume_writable` | bool            | Did a `touch /var/lib/dispatch/.health` succeed?           |
| `sonnet_probe_ok`           | bool            | Did a 30s `claude -p` against Sonnet round-trip cleanly?   |

On failure:

- Missing volume mount → `workspace_volume_writable: false` (not a 500).
- Sonnet probe failure → `sonnet_probe_ok: false` plus the CLI exit
  code under `sonnet_probe_exit_code` and a one-line diagnostic under
  `sonnet_probe_detail`.

The probe is **pinned to Sonnet**: health checks fire on every Sam
container start and we'd rather not burn the Opus 5h quota on
liveness.

## Smoke check

Per the design doc, this must pass before the pack's PR is marked
ready for review:

```bash
docker build -t ai-dev-team-base:latest -f docker/Dockerfile.base docker/
docker compose up -d --force-recreate sam
docker exec sam python /config/packs/dispatch/handler.py dispatch_health
# Expect: JSON with all four fields true, exit code 0.
```

Then verify the named volume survives a restart:

```bash
docker exec sam touch /var/lib/dispatch/.survives
docker compose restart sam
docker exec sam ls -l /var/lib/dispatch/.survives
# Expect: the file is still there, owned by uid 1000 (claude).
```

## Volume

Sam's compose entry mounts the `dispatch-workspaces` named volume at
`/var/lib/dispatch/`. Layout, lifecycle, and the auth-isolation copy
model are documented in the design doc. No host bind-mount on
purpose — wipeable with `docker volume rm dispatch-workspaces`.

## Worker contract

Every dispatched `claude -p` worker is briefed with these standing
rules (rendered into the prompt by `_build_claude_command` in
`handler.py`). They are not per-task overrides — they apply to every
dispatch.

1. **Push before you verify.** As soon as the change compiles and the
   worker's *new* tests pass, commit and push the branch. Open the
   PR as a draft if the work isn't done yet. Only run broader
   integration tests **after** the branch is pushed. Draft is a
   **mid-flight state only** — the moment the work is done (rule 4),
   run `gh pr ready <pr-url>` to flip it back (the `create-pr` skill
   does this for you). A finished PR left in draft reports
   `mergeStateStatus: UNKNOWN` and is silently skipped by the merge
   queue, stalling all automation (#825). Two independent backstops
   also fire `gh pr ready` — babysit's in-stream capture and the
   router-side terminal supervision tick — so a killed worker can't
   strand a green draft, but the worker flipping it itself is the
   primary path.

   *Rationale:* if the dispatch is killed mid-loop (stuck guard,
   budget timeout, runtime timeout), the work survives in git
   instead of being stranded in `/var/lib/dispatch/<id>/`. We
   learned this the hard way on dispatches `ccc4ec` (#203) and
   `7e1e4a` (#154).

2. **Ignore pre-existing test failures unrelated to the change.**
   The dispatch's job is to land its own change, not to repair the
   main branch. Confirm the failure exists on `main` and move on —
   do not loop trying to verify or fix it. The stuck guard from #112
   exists because this loop is the single most common way dispatches
   burn quota.

3. **Scope discipline.** Touch only what the issue asks for. If the
   issue body says "do not touch X", that constraint is binding.

4. **CI green is the definition of done.** Before declaring the PR
   ready (or claiming "done" in the dispatch report), run the repo's
   lint and format checks locally and fix any failures. For Python
   repos in this org that means **both** `ruff check .` **and** `ruff
   format --check .` — CI runs both and a passing `check` with a
   failing `format --check` will still fail the lint job. Tests
   passing is not enough; lint is part of the contract.

   *Rationale:* PR #210 shipped with three E501 long-line warnings
   that blocked merge after the dispatch had already returned
   "success". The worker's own tests passed; the worker simply didn't
   run lint. Catch it on the worker side so the inviting agent
   doesn't have to chase a follow-up commit.

When updating this section, keep the inline prompt in
`_build_claude_command` in sync — they are the same contract, served
to two audiences (humans here, workers there).

## Verify-then-file (#418)

Two verbs that enforce an anti-fabrication rule: neither reports success
unless the **live consumer surface** confirms the side-effect.

### `verify.issue_create`

Creates a GitHub issue and verifies it via `gh issue list` (the board
view a human sees), **not** by re-GETting the URL returned at creation.

```bash
python /config/packs/dispatch/handler.py verify.issue_create \
  --repo bramveen1/ai-dev-team \
  --title "Bug: widget crashes" \
  --body "Steps to reproduce…" \
  --label bug
```

Returns on success:

```json
{
  "status": "verified",
  "number": 419,
  "url": "https://github.com/bramveen1/ai-dev-team/issues/419",
  "receipts": {
    "create_stdout": "…", "create_stderr": "", "create_exit": 0,
    "list_stdout":   "…", "list_stderr":   "", "list_exit": 0
  }
}
```

On 403/429: `{"status": "error", "reason": "gh_api_error", "http_status": 403, …}`

On list-readback miss (fabrication catch): `{"status": "error", "reason": "unverified_side_effect", …}`

### `verify.pr_review`

Submits a PR review and verifies it appears in `gh pr view --json reviews`.

```bash
python /config/packs/dispatch/handler.py verify.pr_review \
  --repo bramveen1/ai-dev-team \
  --pr 420 \
  --body "LGTM — all ACs verified." \
  --event COMMENT   # or APPROVE / REQUEST_CHANGES
```

Returns on success:

```json
{
  "status": "verified",
  "event": "COMMENT",
  "receipts": { "review_stdout": "…", "view_stdout": "…", … }
}
```

### `settings.json` enforcement clamp (Sam container only)

To make `verify.issue_create` / `verify.pr_review` the **only** mutation
path, apply the deny rule from `config.example/agents/sam/settings.json`
to the Sam container's `~/.claude/settings.json`:

```bash
docker exec -u claude sam mkdir -p /home/claude/.claude
docker exec -u claude sam tee /home/claude/.claude/settings.json <<'EOF'
{
  "permissions": {
    "deny": [
      "Bash(gh issue create*)",
      "Bash(gh pr review*)"
    ]
  }
}
EOF
```

**Rollback:** remove the `deny` block (or delete the file entirely).
Without the deny rule the verbs remain callable — they just become opt-in
rather than enforced.

## Approval gating (D-7)

`dispatch_issue` is approval-gated. When the gate fires the handler
returns `{status: "approval_required", draft_id, preview}` and the
agent emits a `draft-approval` fence (see `prompt.md`). The router
posts a Slack approval card; clicking **Approve** re-invokes
`dispatch_issue --approved`, clicking **Decline** posts "dispatch
declined" in the originating thread.

Gate policy lives in `config/dispatch.yaml` under the `approval:` key:

```yaml
approval:
  require_always: true              # pilot default
  destructive_keywords:             # used only when require_always is false
    - destructive
    - delete
    - drop
    - migration
    - reset
```

Config is read on every `dispatch_issue` call — no daemon restart
needed to flip the flag.

### Pilot retro gate

After **5 dispatches with no surprises**, Bram + Sam jointly flip
`require_always` to `false` in `config/dispatch.yaml`. This is a
**manual step**, not automatic — the flip is never triggered by code.
After the flip, the smart-gate predicate takes over:

- `model=opus` **and** issue text contains a destructive keyword → gate
- 5h window cost ≥ `DISPATCH_APPROVAL_COST_USD` (default \$15) → gate
- Otherwise → run directly, no approval card

### Cost-gate env var

`DISPATCH_APPROVAL_COST_USD` sets the USD trigger for the 5h-window
cost gate. Default `15.0`. To change it:

1. Edit the `sam` service env in `docker-compose.yaml` (or `.env`):
   ```
   DISPATCH_APPROVAL_COST_USD=25
   ```
2. Restart the agent: `docker compose restart sam`

No code change or config-file edit required. Unparseable values (e.g.
`"abc"`) log a warning and fall back to the \$15 default (fail-safe —
cost gate is one of three triggers, not the only safeguard).

`dispatch_cancel`, `dispatch_status`, and `dispatch_health` are
**never** approval-gated regardless of the `approval:` config.
