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
   scripts/bootstrap-browser-secrets.sh`.

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
| Sidecar being unreachable / not opted in | The handler exits with code 2 and a "start it with `docker compose --profile browser up`" hint. The dispatcher surfaces that exit code as an actionable error instead of "tool failed". |

## Log scrubbing

`helpers/secrets.py` returns a `SecretBundle` whose `.scrub()` method
replaces every decrypted value of length ≥ 4 with `[REDACTED]` in any
string. The handler:

- Wraps stdout (the JSON response) through `.scrub()` before printing.
- Installs a root-logger filter that runs `.scrub()` over every log
  record's formatted message.
- Routes the bad-response path's exception string through `.scrub()`
  before writing to stderr.

This is belt-and-braces — the sidecar shouldn't echo secrets back to
begin with, but if a future change accidentally puts a token in a
response body or a debug log, the scrub catches it.

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
| `age keyfile … has unsafe permissions 0644` | Keyfile got chmod'd at some point | `chmod 0400 /etc/ai-dev-team/age.key` |
| `profile … has mode 0755; expected 0700` | A backup tool or another process touched the profile dir | `chmod 0700 config/browser_profiles/<name>` and audit who touched it |
| `age decrypt failed` for a blob | Blob was encrypted with a different key, or the keyfile rotated | Re-encrypt the blob with the current pubkey (see "Encrypting a new secret") |
