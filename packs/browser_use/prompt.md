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

Reads (`navigate`, `extract`, `screenshot`, `health`) do not require
approval. Write actions (`submit`, `post`, `apply`, `purchase`) do —
emit a `draft-approval` block instead of calling the handler.

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

## When the sidecar is down

If `health` returns non-200 or the handler exits with
`sidecar unreachable`, tell the operator:

> The `browser-use` sidecar isn't running. Bring it up with
> `docker compose --profile browser up -d browser-use`, then I'll
> retry.

Don't fall back to raw `curl` against the target site — the sidecar
exists so logins and session state stay in one place.
