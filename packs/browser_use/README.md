# browser_use pack

Real-Chromium browser automation for agents who need to drive pages
the GitHub / Slack / email packs can't reach — job-board scraping,
web forms, OAuth flows that demand a human-shaped session.

Backed by [Browser Use](https://github.com/browser-use/browser-use) in
a dedicated sidecar container with persistent per-identity profiles.

## One-time operator setup

1. **Install `age` on the host** — Debian/Ubuntu: `sudo apt install age`.
   macOS: `brew install age`. Needed for both keyfile generation and
   the in-container decrypt at session start.

2. **Bootstrap the keyfile and the secrets/profile dirs**:

   ```bash
   scripts/bootstrap-browser-secrets.sh
   ```

   Defaults:
   - keyfile → `/etc/ai-dev-team/age.key` (mode 0400)
   - profiles → `./config/browser_profiles/` (mode 0700, gitignored)
   - secrets → `./config/secrets/browser/` (mode 0700, gitignored)

   Override the keyfile location with `AGE_KEYFILE=/srv/keys/age.key
   scripts/bootstrap-browser-secrets.sh`. **Set the same `AGE_KEYFILE`
   in your `.env`** — the compose file reads it to bind-mount the
   keyfile into the sidecar at `/run/secrets/age.key`.

   **macOS dev note:** Docker Desktop's file-sharing layer does not
   expose `/etc` by default, so the `/etc/ai-dev-team/age.key` default
   won't bind-mount into the container (the mount silently becomes an
   empty directory). Use a path under your home dir instead:

   ```bash
   AGE_KEYFILE=$HOME/.config/ai-dev-team/age.key \
     scripts/bootstrap-browser-secrets.sh
   echo "AGE_KEYFILE=$HOME/.config/ai-dev-team/age.key" >> .env
   ```

3. **Build the sidecar image** (one-off; rebuild when bumping
   Browser Use):

   ```bash
   docker compose --profile browser build browser-use
   ```

4. **Bring the sidecar up**:

   ```bash
   docker compose --profile browser up -d browser-use
   ```

   The `browser` profile is opt-in: plain `docker compose up` skips
   the sidecar entirely. Useful because the image is heavy (Chromium
   + deps ≈ 1 GB) and most agents don't need it.

5. **Grant the pack** to whichever agent should have it (Slack DM
   with the router):

   ```
   grant lisa browser_use
   ```

   No PAT / OAuth flow — the pack has `needs: []` because all
   real secrets are encrypted at rest under `config/secrets/browser/`.

## Adding a new profile

Profiles are operator-controlled, not agent-controlled. To add
`linkedin-bram`:

1. Pick a name. Lowercase alphanumerics, dashes, underscores. Length
   1–64.
2. Optionally encrypt env-style secrets the sidecar will inject when
   that profile is loaded:

   ```bash
   PUBKEY="$(grep '^# public key:' /etc/ai-dev-team/age.key | awk '{print $4}')"

   cat > linkedin-bram.env <<EOF
   LINKEDIN_2FA_BACKUP_CODE=abcd-efgh-ijkl-mnop
   EOF

   age -r "$PUBKEY" -o config/secrets/browser/linkedin-bram.env.age linkedin-bram.env
   shred -u linkedin-bram.env
   ```

3. First agent call with `profile: linkedin-bram` creates the dir on
   demand (mode 0700) under `config/browser_profiles/linkedin-bram/`.
4. The agent (or you, manually via the sidecar) logs in once.
   Chromium persists the cookies and IndexedDB. Subsequent agent
   calls reuse the session.

To remove a profile: stop the sidecar, `rm -rf
config/browser_profiles/<name>/`, restart. (The encrypted secret
blob is independent; delete `config/secrets/browser/<name>.env.age`
separately.)

## Encrypting a new secret

```bash
PUBKEY="$(grep '^# public key:' /etc/ai-dev-team/age.key | awk '{print $4}')"
age -r "$PUBKEY" -o config/secrets/browser/<profile>.env.age <profile>.env
```

The plaintext file must be a `KEY=value`-per-line `.env` file. Comments
(`# …`) are stripped at decrypt time. Lines without `=` are skipped
with a warning.

The bundle is decrypted in memory inside the sidecar and the values
are injected as env vars into the Browser Use session. They are never
written to disk in plaintext, never echoed in the sidecar's HTTP
response, and never appear in the agent's view (see "Log scrubbing"
below).

## Threat model

**Host compromise is the line we draw.** Anyone with read access to
`/etc/ai-dev-team/age.key` can decrypt every encrypted browser
secret on disk. We do not try to defend against that — the keyfile
is mode 0400 and owned by the docker-running user, but a root user
on the host (or any compromise of the docker-running account) ends
the security argument.

What we *do* defend against:

| Concern | Mitigation |
|---|---|
| Secrets leaking into git | Both dirs are gitignored; CI fails if either appears in a tracked diff. |
| Secrets leaking into agent context | Pack handler decrypts in memory and scrubs every decrypted value out of stdout / stderr / log records before they're visible to the agent. |
| Secrets leaking via container backups | The encrypted blobs are safe to back up (they're age-encrypted). The keyfile is **not** in `/config/` — it lives on the host filesystem so a `config/` backup never contains both halves. |
| Profile dirs getting world-readable | The handler refuses to use a profile dir whose mode drifted from 0700 and tells the operator to `chmod` it. |
| Sidecar being unreachable / not opted in | The handler exits with code 2 and a "start it with `docker compose --profile browser up`" hint. The dispatcher surfaces that exit code as an actionable error instead of "tool failed". Router-level enforcement of `requires_sidecar: true` (refusing the session before the handler runs) is a follow-up — see the [browser_use design notes](../../docs/packs/browser_use.md). |
| Keyfile exposed to the agent container | The age keyfile is mounted **only** into the sidecar (via `/run/secrets/age.key`). The agent container has no access. The handler decrypts nothing — it forwards the profile name to the sidecar, which owns all secret material. |

## Log scrubbing

`helpers/secrets.py` returns a `SecretBundle` whose `.scrub()` method
replaces every decrypted value of length ≥ 4 with `[REDACTED]` in any
string. The **sidecar** (which owns the keyfile and the plaintext)
runs every response body through `.scrub()` before sending it on the
wire — that's where the scrub is load-bearing.

The handler doesn't decrypt secrets, so it has nothing to scrub on
its end. It just forwards the sidecar's response verbatim to stdout.

### What scrub catches

- Verbatim plaintext values that appear inside response strings:
  `"got TOKEN=topsecret9999"` → `"got TOKEN=[REDACTED]"`.

### What scrub does NOT catch

- **Encoded values** — base64, URL-encoded, hex, JSON-stringified, or
  whitespace-mangled versions of the same secret. If the action
  handler emits `base64(secret)` the scrub passes it through unchanged.
- **Partial leaks** — first 8 chars of a token in a `"redirected to
  https://…?session=topse…"` URL.
- **Indirect leaks** — a screenshot PNG that visibly contains a
  password field, a downloaded HTML file that includes session
  cookies, an exception traceback from a deep dependency that
  formats env contents differently than `KEY=value`.
- **Values shorter than 4 characters** — those are skipped on purpose
  so the scrub doesn't mangle every occurrence of an unrelated
  letter or digit.

The action handlers (`_dispatch_action` in
`browser_use_sidecar/server.py`) are the right place to defend
against these — don't include raw env values in response objects,
don't log Browser Use's full session state, don't echo back arbitrary
DOM text from a page that may contain credentials. Treat scrub as
the second line of defence, not the first.

## Guard interaction

The pack does not special-case the generic stuck-detection guards
(see issue #112). Browser tasks share the global turn-cap, loop, and
error-streak thresholds. Per-pack overrides are a follow-up once #112
runs in enforce mode — until then, **expect a browser task to loop
the same way an LLM-only task would**, and prefer dry-run mode while
calibrating.

## Approval gates

`pack.yaml` declares `approve: [submit, post, apply, purchase]`. The
agent emits a `draft-approval` block for those verbs instead of
calling the handler directly — see `prompt.md` for the exact shape.
Reads (`navigate`, `extract`, `screenshot`, `health`) bypass approval.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `sidecar unreachable` | Sidecar not running or wrong compose profile | `docker compose --profile browser up -d browser-use` |
| `age keyfile not found at /etc/ai-dev-team/age.key` | Bootstrap not run on this host | `scripts/bootstrap-browser-secrets.sh` |
| `age keyfile … is not a regular file` | Bind-mount source path doesn't exist on the host (typical on macOS — Docker Desktop doesn't share `/etc` by default, so it auto-creates an empty dir at the target) | Set `AGE_KEYFILE=$HOME/.config/ai-dev-team/age.key` in `.env`, regenerate the keyfile there, then `docker compose --profile browser up -d --force-recreate browser-use` |
| `age keyfile … has unsafe permissions 0644` | Keyfile got chmod'd at some point | `chmod 0400 /etc/ai-dev-team/age.key` |
| `profile … has mode 0755; expected 0700` | A backup tool or another process touched the profile dir | `chmod 0700 config/browser_profiles/<name>` and audit who touched it |
| `age decrypt failed` for a blob | Blob was encrypted with a different key, or the keyfile rotated | Re-encrypt the blob with the current pubkey (see "Encrypting a new secret") |
