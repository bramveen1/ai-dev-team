# browser_use pack — operator guide

See [`packs/browser_use/README.md`](../../packs/browser_use/README.md)
for the full setup walkthrough. This page documents the design
decisions and what's intentionally out of scope, so future contributors
don't relitigate them.

## Why a sidecar, not the router

Browser Use pulls in Chromium and a ~1 GB worth of system deps. Adding
that to the router image would punish every operator who never uses
the browser pack with longer builds, slower starts, and more update
surface. The sidecar is gated by an opt-in compose profile
(`--profile browser`) so the default `docker compose up` path stays
cheap.

## Profile persistence

Profiles live at `./config/browser_profiles/<name>/` on the host and
are bind-mounted into the sidecar. They survive restarts and image
rebuilds. Treat them as secrets — cookies, IndexedDB, and local
storage are all in there. They're gitignored and the README warns
against backing them up unencrypted.

Mode is enforced at 0700; the handler refuses to use a profile dir
whose mode drifted.

## Secrets at rest (age)

Encrypted blobs live under `./config/secrets/browser/*.age`. The
master keyfile lives **on the host**, never in `/config/` (which is
the portability boundary — operators copy `config/` between machines
and we don't want a keyfile riding along inside it). Default
location: `/etc/ai-dev-team/age.key`, mode 0400.

The sidecar gets the keyfile via a tmpfs / docker-secret mount at
`/run/secrets/age.key`. It is not baked into the image and not visible
to other containers.

## Reachability check

`packs/browser_use/helpers/sidecar_client.py:SidecarClient.health` is
the dispatcher's hook for "is the sidecar up?" The handler calls this
before any action and exits with code 2 (and an actionable error
message) if it's down. Manifest declares `requires_sidecar: true` so
the router can also probe at dispatch time once that integration lands.

## Guard interaction

Browser tasks are the highest-loop-risk class of agent work we have:
"click submit, get rejected, try again" is a real pattern. The pack
registers with the generic guards (see issue #112) like any other
tool. Per-pack threshold overrides are explicitly out of scope here
— filed as a follow-up once #112 is in enforce mode. Until then,
browser tasks share the global thresholds and operators should keep
the guard in dry-run mode while calibrating.

## Out of scope (filed as follow-ups)

- **Per-pack guard threshold overrides** — wait for #112 in enforce.
- **Multi-host profile sync** — portability is single-directory copy
  plus the host keyfile. Anything fancier (rsync, syncthing, …) is
  the operator's problem.
- **Headed mode / VNC for debugging** — sidecar is headless. A
  debug-flag-gated VNC stream can be added later if the agent's
  screenshots aren't enough to debug a loop.

## Open questions

- The host keyfile location (`/etc/ai-dev-team/age.key`) is a
  default, not a hard requirement. Operators can override with
  `AGE_KEYFILE=...` when running the bootstrap script and
  `BROWSER_USE_AGE_KEYFILE=...` when starting the sidecar. The
  default was chosen because (a) it's outside `/config/` so a
  config backup doesn't include it, and (b) `/etc/` is the
  conventional spot for host-wide service config.
- A periodic "profile health check" (login-still-valid probe) is
  not in v1. The agent's own retry-on-401 logic handles the
  common case; revisit if we see profiles silently expiring in
  the wild.
