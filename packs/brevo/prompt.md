# Brevo (via the Maton gateway)

You can send transactional and marketing email through Brevo, manage
contacts and lists, and trigger email campaigns. Calls go through the
Maton gateway, which proxies the Brevo REST API and handles its key
rotation on your behalf.

## Auth

- **Base URL:** `https://gateway.maton.ai/brevo`
- **Auth header:** `Authorization: Bearer $BREVO_API_KEY`

The token is in the `BREVO_API_KEY` env var. Never log it, paste it
into a Slack message, or include it in a draft you ask a human to
review.

## Common operations

```bash
# Account info — handy first call to confirm the key works
curl -s -H "Authorization: Bearer $BREVO_API_KEY" \
  "https://gateway.maton.ai/brevo/v3/account"

# Send a 1:1 transactional email (UNGATED — runs immediately)
curl -s -X POST -H "Authorization: Bearer $BREVO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sender": {"email": "team@example.com", "name": "Team"},
    "to":     [{"email": "alice@example.com", "name": "Alice"}],
    "subject": "Hello",
    "htmlContent": "<p>hi</p>"
  }' \
  "https://gateway.maton.ai/brevo/v3/smtp/email"

# List contacts
GET /v3/contacts?limit=50&offset=0

# Add or upsert a contact
POST /v3/contacts
  body: {"email": "alice@example.com", "attributes": {"FNAME": "Alice"}, "listIds": [3]}

# Update a contact
PUT /v3/contacts/{identifier}
  body: {"attributes": {"FNAME": "Alicia"}}

# Delete a single contact
DELETE /v3/contacts/{identifier}

# Lists & segments (read)
GET /v3/contacts/lists
GET /v3/contacts/segments

# Email campaigns (read)
GET /v3/emailCampaigns
GET /v3/emailCampaigns/{campaignId}
```

The Maton gateway forwards Brevo's response shape verbatim — see the
[Brevo API reference](https://developers.brevo.com/reference) for full
field lists.

## Approval rules

This pack declares `approve: [send-campaign, send-bulk]`. The actions
below need a human on the approval card before they run. Everything
else (reads, single-contact CRUD, 1:1 transactional sends) executes
immediately — don't draft for those.

**Gated** — must end your reply with a `draft-approval` block instead
of calling the API:

- `POST /v3/emailCampaigns` (create campaign) → `action_verb: "send-campaign"`
- `POST /v3/emailCampaigns/{id}/sendNow` (trigger campaign) → `action_verb: "send-campaign"`
- `POST /v3/smtp/email` **when any of:**
  - `to` resolves to more than one recipient, **or**
  - `messageVersions[]` is present, **or**
  - the payload references a contact list / segment (e.g. via `listIds`)
  → `action_verb: "send-bulk"`

**Ungated** — call the API directly:

- All `GET …` reads.
- `POST /v3/smtp/email` with a single `to` recipient and no
  `messageVersions[]` and no list/segment reference. This is the
  standard 1:1 transactional case.
- `POST/PUT /v3/contacts` and `DELETE /v3/contacts/{identifier}`
  against a single contact.

### Draft block shape

Concrete example for "send the May newsletter campaign (id 1422)":

````
```draft-approval
{"draft_id": "1422", "pack": "brevo", "action_verb": "send-campaign", "payload": {"campaign_id": 1422, "name": "May newsletter", "subject": "May at PathToHired", "list_ids": [12], "recipient_count": 4831}}
```
````

Concrete example for "send the same outreach to 87 contacts via /smtp/email":

````
```draft-approval
{"draft_id": "outreach-2026-05-07", "pack": "brevo", "action_verb": "send-bulk", "payload": {"endpoint": "/v3/smtp/email", "subject": "Quick hello", "sender": {"email": "team@example.com"}, "recipient_count": 87, "list_ids": [12]}}
```
````

Notes on the shape:
- The fence info is `draft-approval` — not `json`. If you write
  ` ```json `, the router won't see it as a draft block.
- `draft_id` is required. Use the campaign id (as a string) for
  campaigns, or a stable label (date + slug) for bulk sends.
- `pack` is `"brevo"`. `action_verb` is `"send-campaign"` or `"send-bulk"`.
- Put the full request body, plus `recipient_count` and any
  campaign id/name, inside `payload` so the approval card can
  preview blast size before a human clicks Send.
- One block per draft. Don't add extra prose after it.

## When you don't have it

If `$BREVO_API_KEY` is missing or the gateway returns 401, tell the
user to run `grant <agent> brevo` in Slack. Don't try to call the
Brevo API directly with a different key — the gateway is the only
supported path.
