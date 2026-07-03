# Runtime configuration — settings, secrets, and the /config page

Issue #576 replaced the ".env → docker-compose environment → container
recreate" loop with a settings layer that can be edited **while the router
runs**. This doc is the contract for what lives where.

## The three tiers

| Tier | Storage | Examples | A change takes effect |
|---|---|---|---|
| **Runtime vars** | `config/runtime.json` (bind-mounted, re-read on a ~15 s TTL) | `AUTO_DISPATCH_CHANNEL`, `MERGE_QUEUE_CHANNEL`, `BRAM_DM_CHANNEL`, `SESSION_TIMEOUT`, feature toggles | *hot* keys: next read (≤ TTL); *restart* keys: next `docker restart router` |
| **Secrets** | `data/secrets.json` (router-only mount) and `config/secrets/*.token` files | `WORKERS_BOT_TOKEN`, `WORKERS_DISCORD_TOKEN`, GitHub PATs | next read — these are read per use, no restart |
| **Boot env** | `.env` → compose `environment:` | Slack app/bot credentials, `ROUTER_INTERNAL_TOKEN`, `CLAUDE_AUTH_MODE` + Claude keys for agent containers | edit `.env`, then `docker compose up -d` (recreate) |

Every runtime var and managed secret is declared exactly once, in the
registry at `router/settings.py` (`_REGISTRY_ENTRIES`): key, type, default,
description, category, and reload class (`hot` / `restart`). **Adding a new
var is one registry entry** — no `.env` edit, no compose renderer change, no
image rebuild. Code reads values with:

```python
from router import settings
channel = settings.get("AUTO_DISPATCH_CHANNEL")
```

## Precedence: store wins over env

Resolution order for `settings.get(key)`:

1. `config/runtime.json` (vars) / `data/secrets.json` (secrets) — **wins**
2. environment variable of the same name (empty string counts as unset —
   compose renders `${VAR:-}` as `""`)
3. registry default

The env fallback keeps existing deployments working unchanged; nothing needs
migrating on day one. When both a store value and an env var are set, the
store wins and a warning is logged once. Note this **flipped** the old
`WORKERS_BOT_TOKEN` precedence (env used to win) — decided with the operator
alongside #576 so the config page is authoritative.

`hot` vs `restart` is about *when the value is consumed*, not where it is
stored: `AUTO_DISPATCH_CHANNEL` is resolved on every tick (hot), while e.g.
`SLASH_COMMAND_PREFIX` is only read when handlers register at boot — the
saved value applies after a plain `docker restart router` (no recreate, no
rebuild; the file is re-read at boot).

## The /config page

The router serves a config UI on the health server (container port 8080,
published on `127.0.0.1` only). From your workstation:

```bash
ssh -L 8080:127.0.0.1:8080 <host>
# then open http://localhost:8080/config
```

SSH is the authentication layer — reaching the port at all requires SSH (or
shell) access to the host, which already implies the power to edit `.env`.
This matches the `/logs` and `/wakeup` trust model (compose network is
internal, host port is loopback-bound).

What the page does:

- **Runtime vars** — edit, save, reset (reset removes the key so env/default
  apply again). Badges show the winning source (`runtime`/`env`/`default`)
  and reload class. Saving a `restart` key tells you so.
- **Validation at save time** — types and ranges are checked, and
  channel-typed settings are resolved against Slack live: a typo'd channel ID
  is rejected in the UI instead of failing `channel_not_found` every tick 30
  minutes later (the #576 incident class). A Slack outage never blocks a
  save, and "save anyway" (force) is offered.
- **Secrets** — write-only: existing values render masked (`••••1234`) and
  are never returned raw by the API. Stored in `data/secrets.json`, which is
  mounted into the router only (never `config/`, which agent containers can
  read).
- **Token files** — add/replace/delete `config/secrets/*.token` (GitHub PATs
  for the merge queue and auto-dispatch). Written `0600`, atomically; read
  per tick, so a rotated PAT applies without any restart.
- **Boot environment** — shown read-only (set/unset, masked) so you can see
  at a glance what the containers were started with.

REST endpoints (same trust model, used by the page):
`GET /config/api/settings`, `PUT/DELETE /config/api/settings/{key}`,
`GET /config/api/tokenfiles`, `PUT/DELETE /config/api/tokenfiles/{name}`.

## Failure modes (by design, from #576)

- **Malformed / mid-edit `runtime.json`** — the router keeps the last-good
  parse, logs an error, and never crashes a tick.
- **A single invalid value in the file** — warned and skipped for that key
  only (falls back to env/default); other keys are unaffected.
- **Writes** are atomic (`.tmp` + rename), matching `SecretStore`.
- **Hand-editing** `config/runtime.json` (or copying it between machines)
  is supported — it's plain JSON and the router picks changes up within the
  TTL. The page is a convenience, not a requirement.

## How this stays fixed (governance ratchets)

`tests/unit/test_config_governance.py` runs in CI and only turns one way:

- **No new direct env reads in `router/`** — every `os.environ`/`os.getenv`
  occurrence is counted against a frozen per-file allowlist of boot-tier
  reads. A new read fails CI with instructions to add a registry entry
  instead. When a file drops a read, a companion test forces the allowlist
  to shrink, so headroom never accumulates.
- **The compose env block is frozen** — the router's `environment:` list in
  `docker-compose.yml` is legacy fallback plumbing as of #576. Any new var
  there fails CI (runtime vars need no compose line at all), and every
  static var it carries must exist in the registry.

Growing either frozen list is possible — but only by editing the governance
test itself, which makes "is this really boot-tier?" an explicit review
question instead of a silent regression. The agent-facing rules live in
`CLAUDE.md` ("Configuration & Secrets"), so the AI dev team is prompted with
the contract on every task.

## What deliberately did NOT move

Slack socket-mode credentials, `ROUTER_INTERNAL_TOKEN`, and the Claude auth
material injected into agent containers stay boot-env. Hot-reloading live
connection credentials needs its own design review (#576 called this out
explicitly); the page therefore shows them read-only.
