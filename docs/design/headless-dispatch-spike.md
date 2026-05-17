# Headless Dispatch Spike — Report

_Status: complete · Owner: Sam · Issue: #150 · Related design: [headless-dispatch.md](headless-dispatch.md)_

## Recommendation

**Candidate B (headless `claude -p` as a subprocess) is the bridge for v1.**
Candidate A (`claude --remote`) is not currently available in Claude Code CLI
2.1.142 — the flag does not exist. Revisit when Anthropic ships a public
cloud-dispatch CLI surface; until then, `claude --remote` is a deferred path,
not a viable option.

Total spike spend: ~1h15m wall time, ~$0.11 in Sonnet usage against the
throwaway repo. Well under the 4–6h budget.

## Known-good invocation recipe

The dispatcher (`dispatch_issue`) should invoke the dev agent as follows.
This recipe is what landed PR #1 in `bramveen1/ai-dev-team-spike`
end-to-end in 24.6s.

```bash
# Workspace prep (per dispatch — ephemeral)
WORKDIR=$(mktemp -d -t dispatch-XXXXXXXX)
git clone --depth=1 "https://github.com/${OWNER}/${REPO}.git" "$WORKDIR"
cd "$WORKDIR"
git checkout -b "dispatch/${ISSUE_NUMBER}-${SHORT_SLUG}"

# Invocation (non-interactive, JSON output, scoped)
claude -p "$PROMPT" \
  --model sonnet \
  --output-format json \
  --add-dir "$WORKDIR" \
  --permission-mode acceptEdits \
  > "$WORKDIR/.dispatch-result.json"

# Parse result — exit code is NOT the source of truth, check JSON.is_error
python3 -c '
import json, sys
r = json.load(open(".dispatch-result.json"))
if r.get("is_error"):
    sys.exit(f"dispatch failed: {r.get(\"error\", \"unknown\")}")
print(json.dumps({
    "session_id": r["session_id"],
    "total_cost_usd": r["total_cost_usd"],
    "num_turns": r["num_turns"],
}))
'

# PR open (dev agent does this in-session via gh, or dispatcher does it after)
```

**Required environment:**
- `claude` CLI on PATH, version ≥ 2.1.142.
- Authenticated to claude.ai (Max sub) via keychain. Already present in
  Sam's container as of the spike — no mount, no env var.
- `gh` CLI authenticated (PAT). Already present.
- Network egress to api.anthropic.com and github.com.

**Required flags:**
- `-p` — non-interactive prompt mode.
- `--output-format json` — structured handle for parsing.
- `--model sonnet` — pin Sonnet; the CLI default selected Opus on probe 1.
  Do NOT omit this flag.
- `--add-dir` — restrict filesystem scope to the dispatch workspace.
- `--permission-mode acceptEdits` — required so the headless session can
  edit files without an interactive approval prompt.

**Do NOT use:**
- `--bare` — disables keychain auth, requires `ANTHROPIC_API_KEY`. We need
  keychain for the Max sub, so `--bare` is off.
- `--max-budget-usd` as a hard limit. It's a sanity stop, not a contract
  (see Failure Modes §1).

## Trade-off table

| Dimension          | A — `claude --remote`        | B — headless `claude -p` subprocess        |
|--------------------|------------------------------|--------------------------------------------|
| Availability       | **Not in CLI 2.1.142.** Flag absent. | Available, stable surface.                |
| Quota              | Anthropic-managed VM, draws from Max sub | Local subprocess, draws from same Max sub |
| Identity           | (untested) cloud VM clones via GitHub creds | PR opens as Bram (Bram's `gh` PAT in Sam's container) |
| Portability        | (untested) no local infra needed | Stays inside our stack, single-dir-copy intact |
| Blast radius       | (untested) cloud VM isolated from our FS | Subprocess on Sam's container FS — needs ephemeral workspace |
| Time-to-PR         | (untested)                   | ~25s for trivial task (measured)           |
| Cleanup            | (untested) cloud session lifecycle unknown | `SIGTERM` → clean process tree, no orphans (measured) |
| Concurrency        | (untested) parallel cloud sessions likely free | N concurrent dispatches = N processes on same 5h window |
| Cost telemetry     | (untested)                   | `total_cost_usd` + per-model breakdown in JSON output |

## Failure modes observed

**1. Budget cap is lazy, not hard.**
`--max-budget-usd 0.001` returned a structured error envelope
(`error_max_budget_usd`, `is_error: true`) but the run still spent
$0.035 and took ~30s before stopping. _Treat budget caps as sanity
stops only._ The dispatcher should also track `total_cost_usd` per
session and short-circuit at a higher layer if needed.

**2. Exit code is not the truth.**
Even on `is_error: true`, the process exited 0. The dispatcher must
parse the JSON and check `is_error` — never branch on `$?`.

**3. Mid-flight kill is clean.**
`SIGTERM` to the `claude` PID terminates the process tree cleanly.
No orphan processes. Side effects are filesystem-only (uncommitted
changes in the workspace), and the workspace is ephemeral, so
cleanup is `rm -rf $WORKDIR`.

**4. Default model is Opus.**
Without `--model sonnet`, probe 1 selected `claude-opus-4-7[1m]`
— the heaviest model. Spend was several multiples of the same prompt
on Sonnet. Pin Sonnet by default; require an explicit escalation
flag for Opus.

**5. Quota exhaustion not observed.**
Would require burning the 5h Max-sub window to trigger. Best the
dispatcher can do: log `total_cost_usd` per session, count concurrent
in-flight sessions, alert when nearing the window cap.

**6. `claude agents` is not cloud-dispatch.**
For the record: `claude agents` lists the in-process sub-agent
registry (Task tool sub-agents — Explore, Plan, etc.). It is _not_
a cloud-runner control surface. Don't confuse the two when scoping
later issues.

## Open questions for #D to answer

The spike deliberately stops at "the bridge works." These are the
decisions #D (the `dispatch` pack) needs to make:

1. **Subprocess inside Sam's container vs. sibling Docker container.**
   The spike used a subprocess. A sibling container is cleaner
   isolation but requires either (a) exposing the host docker socket
   into Sam, or (b) running the dev agent as its own long-lived
   compose service that Sam talks to over HTTP/Unix socket. Pick
   one in #D. Subprocess is the lower-risk v1.

2. **Where does the ephemeral workspace live?**
   `mktemp -d` works inside Sam's container today, but the workspace
   carries a full repo clone. Pick a path that has enough room and
   is `gitignore`'d / outside any bind-mount we don't want polluted.

3. **Concurrency policy.**
   Three dispatches in parallel = three `claude` processes on the
   same Max-sub quota window. Decide: hard cap N=1 for the pilot, or
   allow parallel and rely on quota-exhaustion telemetry?

4. **Cost ceiling per dispatch.**
   The CLI's `--max-budget-usd` is too lazy to enforce. The dispatcher
   needs its own cap: terminate the subprocess if `total_cost_usd`
   in the streamed JSON exceeds the threshold. What's the threshold?

5. **Workspace pre-population.**
   v1 is `git clone --depth=1` per dispatch. For larger repos or
   iterative dispatches on the same PR ("address review on PR #N"),
   a persistent worktree may be cheaper. Punt to Phase 2.

6. **PR-author identity.**
   The headless session opened PR #1 as Bram (via Bram's PAT). That's
   acceptable for the pilot and matches the design 1-pager. The
   tech-debt issue for a separate dev-agent identity remains open.

## What stays from the 1-pager

The design contract in [headless-dispatch.md](headless-dispatch.md) holds
unchanged in all respects except this one substitution:

- **Bridge** is Candidate B (subprocess), not Candidate A.
- All other sections (approval gating, review checklist, escalation,
  health probe, failure-mode rollback) are unchanged.

The 1-pager's `dispatch.health` verb maps cleanly to a tiny
`claude -p "echo: hello" --model sonnet --output-format json` probe
with a 30s timeout.

## Files in this spike

- This report: `docs/design/headless-dispatch-spike.md`
- Spike repo: `bramveen1/ai-dev-team-spike` (keep for reference / re-runs)
- Reference PR: `bramveen1/ai-dev-team-spike#1`

## Next

#D (the `dispatch` pack) is unblocked. It should answer the six open
questions above before code lands, but the bridge contract is now
known. Lisa can schedule.
