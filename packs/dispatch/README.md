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
writes on a 15-second cadence (`<workspace>/heartbeat`). The router treats a
heartbeat stale for more than 45 seconds as evidence the process is gone.
Cross-namespace `kill -0` is unreliable and was the root cause of false-orphan
detections before #172.

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

## Approval gating

Not wired in D-1. Lands in #D-7 along with the 5-dispatch flip flag.
For the scaffold, `approve: []` in `pack.yaml`.
