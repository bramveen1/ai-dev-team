# browser_use sidecar entrypoint — design notes

Companion to issue #143. Captures the contract added by `docker/entrypoint-browser.sh`
so we don't re-learn the "host bind-mount masks image ownership" lesson a third
time.

## Problem

The sidecar runs as uid 1000 (`sidecar`). It owns three on-disk directories:

- `/config/browser_profiles/<name>/` — Chromium user-data, mode 0700.
- `/config/secrets/browser/` — age-encrypted secret blobs, mode 0700.
- `/run/secrets/age.key` — host keyfile, managed by Docker.

`docker/Dockerfile.browser` does `chown -R sidecar:sidecar /config` at build
time, but those directories are then bind-mounted from the host at runtime.
The bind-mount uses the **host** ownership, which on a fresh checkout is
whatever uid created the dirs (`root` or the operator's account) — never uid
1000. The image-time chown is irrelevant.

Symptom: a `PermissionError` raised from `pathlib.Path.exists()` inside the
sidecar because uid 1000 can't traverse a dir owned by another uid. We've now
hit this twice — once on `browser_profiles/`, once on `secrets/browser/`
(#143 root cause).

## Contract

Before uvicorn starts, an entrypoint script — running as root inside the
container — ensures that:

1. `/config/browser_profiles` and `/config/secrets/browser` exist, are owned
   `1000:1000`, and are mode `0700`.
2. Every `*.age` blob directly under `/config/secrets/browser` is owned
   `1000:1000` and mode `0600`.
3. After (1)–(2), the script `exec gosu sidecar "$@"` and the CMD (uvicorn)
   runs as uid 1000.
4. `/run/secrets/age.key` is **never** touched. Docker manages that mount;
   `assert_keyfile_safe()` enforces the mode check on the sidecar side.

## Why root in the entrypoint

The bind-mount inodes are the host's. Only root inside the container can
`chown` them. uid 1000 cannot. We need exactly enough privilege to fix the
ownership and then drop it — same pattern as the agent container's
`docker/entrypoint.sh` (which chowns `/home/claude/.claude` and then
`exec gosu claude`).

`gosu` is already installed in `Dockerfile.browser` (was added for #141's
work). `tini` stays as PID 1 (`ENTRYPOINT ["tini", "--", ...]`); the script
runs as PID 2 and execs uvicorn in place, so signal handling is preserved.

## Why not just `:ro` on the secrets mount

Until #143, `./config/secrets/browser` was bind-mounted `:ro`. That blocked
`chown` from inside the container (read-only mount semantics), which is the
exact thing the entrypoint needs to do. Dropping `:ro` is a small defense-
in-depth loss: a compromised sidecar could now corrupt the encrypted bundles.
But:

- The sidecar already has full *read* access to the blobs; a compromise can
  exfiltrate plaintext after decrypt, with or without write access.
- Corruption is a data-integrity concern, not a confidentiality one.
- The alternative (chown only on first start via a host-side script) recreates
  the exact "operator has to remember" footgun that #143 says we want to
  eliminate.

Net: drop `:ro`, fix at runtime, document the tradeoff here.

## Failure modes

- **Chown fails because the path is a `:ro` mount** — log a warning, continue.
  The runtime check (`assert_keyfile_safe` for the keyfile, the verb-level
  perm check for the profile dir) will surface the real problem with a
  scoped error message. Crashing the sidecar at entrypoint time would
  obscure the actual cause.
- **The mount path doesn't exist** — `mkdir -p` creates it. This is the
  common "fresh checkout" case.
- **Some blob has unexpected mode/owner** — chown + chmod fix it. We don't
  validate the previous state; the entrypoint is idempotent.
- **Operator deliberately set different ownership** — we override. This is
  a design choice: the sidecar is the canonical owner of these dirs, and
  the entrypoint enforces that contract. If the operator needs a different
  uid, they override `SIDECAR_UID` / `SIDECAR_GID` env vars.

## Test coverage

- Unit: `tests/unit/packs/test_pack_browser_use.py` asserts the Dockerfile
  references the entrypoint script, the script exists and is executable, the
  script execs `gosu sidecar`, and the compose mount for `secrets/browser` is
  no longer `:ro`.
- Manual: the steps in the issue's acceptance — `sudo chown -R root:root
  config/browser_profiles config/secrets/browser`, then `docker compose
  --profile browser up -d --force-recreate browser-use && curl /health` → 200.
