# Browser access (browser_use pack)

You can drive a real Chromium browser through the `browser_use` pack.
The browser runs in a sidecar container — you reach it by calling
`packs/browser_use/handler.py` from Bash. Per-identity profiles
(`linkedin-bram`, `indeed-bram`, …) persist between sessions, so you
will pick up logged-in cookies and form autofill across restarts.

## What you can do

Call the handler with an action and a JSON payload on stdin:

```bash
python /config/packs/browser_use/handler.py <action> \
  --profile <profile-name> < payload.json
```

Available actions:

- `navigate` — open a URL and wait for it to settle. Returns the final
  URL, title, and visible text.
- `extract` — pull structured fields from the current page using a
  CSS-selector map.
- `screenshot` — capture a PNG of the viewport or full page.
- `health` — verify the sidecar is up. Use this first if you get a
  connection error.
- `login` — authenticate the profile against a username/password form.
  Credentials live encrypted inside the sidecar; you only pass the
  `credential_key` (the lookup name). Returns
  `{"session_persisted": true}` on success.
- `session_status` — cheap probe: returns `{"status": "authed" |
  "unauthed" | "unknown"}` against a configured probe URL. Use this
  before reaching for `login` so you don't re-authenticate when the
  session is already good.

Reads (`navigate`, `extract`, `screenshot`, `health`, `login`,
`session_status`) do not require approval. Write actions (`submit`,
`post`, `apply`, `purchase`) do — emit a `draft-approval` block
instead of calling the handler.

## Approval-gated actions

Anything that modifies state on a third-party site — submitting a
form, applying to a job, posting content, making a purchase — is
approval-gated. End your reply with a fenced code block whose info
string is literally `draft-approval` and whose body is one JSON
object. The router parses that block, strips it from the visible
message, and posts an approval card.

Example for "apply to job listing 12345 on indeed":

````
```draft-approval
{"draft_id": "apply-indeed-12345", "pack": "browser_use", "action_verb": "apply", "payload": {"profile": "indeed-bram", "url": "https://www.indeed.com/viewjob?jk=12345", "resume": "Bram Veenhof", "summary": "Apply to senior backend role at Acme Corp."}}
```
````

Notes on the shape:
- The fence info is `draft-approval` — not `json`. Other fences won't
  be parsed and the approval card won't render.
- `draft_id` is required. Use a stable string the operator can
  recognise (`apply-indeed-12345`, not a UUID).
- `pack` is `"browser_use"`. `action_verb` is one of
  `submit | post | apply | purchase`.
- Everything else (profile, url, form fields, …) nests inside
  `payload` so the approval card can preview it.

## Profiles

Profiles live at `/config/browser_profiles/<profile-name>/` inside
the sidecar. One dir per logical identity. The handler will create
the dir on first use; subsequent calls reuse the same cookies and
local storage.

Profile names are operator-controlled, not agent-controlled. If you
need a new profile (e.g. `linkedin-sam`) ask the operator to bootstrap
it before you reach for it. Don't invent profile names — `unknown-1`
won't exist on disk.

## Secrets

Site passwords and tokens live encrypted under
`/config/secrets/browser/*.age` and are decrypted at session start
inside the sidecar — never written to disk in plaintext and never
visible to you in the response. If a login flow needs a fresh
credential the operator will encrypt it for you; ask in Slack
("I need a credential for `linkedin-bram` to handle 2FA"), don't try
to harvest it yourself.

## Logging in (per-profile credentials)

Login passwords for sites like pathtohired.com live encrypted at
`/config/browser_profiles/<profile>/credentials.age`, separate from
the env-style secrets above. The operator stores them once via Slack
DM (`grant <profile> credentials <credential_key>`) and you reference
them from the `login` payload by `credential_key` only — you never
see the username or password.

Example `login` payload (POST to handler.py via stdin):

```json
{
  "profile": "pathtohired-bram",
  "credential_key": "pathtohired",
  "url": "https://pathtohired.com/login",
  "selectors": {
    "username": "input[name=email]",
    "password": "input[name=password]",
    "submit":   "button[type=submit]",
    "success":  "[data-testid=user-menu]"
  }
}
```

Behaviour you can rely on:

- **Session sticks across runs.** A successful `login` writes the
  full storage state (cookies + localStorage) back to the profile
  dir; subsequent `navigate` calls reuse it.
- **Auto-retry on logged-out signal.** If a `navigate` / `extract` /
  `screenshot` hits a 401/403 or redirects to the login URL, the
  sidecar silently re-runs `login` once with the cached credentials
  and retries. You don't need to call `session_status` first unless
  you want to short-circuit the round-trip.
- **MFA hard-fails.** A response with `error_type: mfa_required`
  means the site demanded TOTP/WebAuthn — v1 doesn't handle either.
  Don't retry; ask the operator to disable MFA for this profile or
  switch to a session that's already MFA-cleared.
- **No screenshots on `login` failure.** A bad password produces a
  structured error envelope with `error_type` set, but no screenshot
  and no DOM body — by design (locked decision #5; redaction is too
  easy to get wrong with credentials in the page).

## When the sidecar is down

If `health` returns non-200 or the handler exits with
`sidecar unreachable`, tell the operator:

> The `browser-use` sidecar isn't running. Bring it up with
> `docker compose --profile browser up -d browser-use`, then I'll
> retry.

Don't fall back to raw `curl` against the target site — the sidecar
exists so logins and session state stay in one place.
