# Zoho Mail (via the Maton gateway)

You can read, search, draft, and send mail from a Zoho Mail account.
Calls go through the Maton gateway, which proxies the Zoho Mail REST
API and handles its OAuth refresh on your behalf.

## Auth

- **Base URL:** `https://gateway.maton.ai/zoho-mail`
- **Auth header:** `Authorization: Bearer $ZOHO_API_KEY`

The token is in the `ZOHO_API_KEY` env var. Never log it, paste it
into a Slack message, or include it in a draft you ask a human to
review.

## Discovering the account on first use

The gateway proxies one or more Zoho accounts. List them once, then
keep the IDs in working memory for the rest of the session:

```bash
curl -s -H "Authorization: Bearer $ZOHO_API_KEY" \
  "https://gateway.maton.ai/zoho-mail/api/accounts"
```

The response includes `accountId` for each mailbox. Each account has
folders (Inbox, Sent, Drafts, Archive, …); fetch them with:

```bash
curl -s -H "Authorization: Bearer $ZOHO_API_KEY" \
  "https://gateway.maton.ai/zoho-mail/api/accounts/<accountId>/folders"
```

The Inbox folder has `folderType=Inbox`; cache its `folderId`.

## Common operations

```bash
# List recent inbox messages
GET /api/accounts/<accountId>/messages/view?folderId=<inboxFolderId>&limit=10

# Search across folders
GET /api/accounts/<accountId>/messages/search?searchKey=<urlencoded query>

# Read a message
GET /api/accounts/<accountId>/folders/<folderId>/messages/<messageId>/content

# Send a message
POST /api/accounts/<accountId>/messages
  body: {"fromAddress": "...", "toAddress": "...", "subject": "...", "content": "..."}

# Archive (move to Archive folder)
PUT /api/accounts/<accountId>/messages
  body: {"mode": "moveMessage", "folderId": "<archiveFolderId>", "messageIds": ["..."]}
```

The Maton gateway forwards Zoho's response shape verbatim — see
[Zoho Mail API docs](https://www.zoho.com/mail/help/api/) for fields.

## Approval rules

This pack has `approve: []`. The agent that holds it controls the
mailbox directly: send, archive, delete, and replies all execute
without an approval card. The grant itself was the approval.

If you find yourself touching *someone else's* inbox via the same
token, stop — that case is meant to flow through a different pack with
explicit approval rules.

## When you don't have it

If `$ZOHO_API_KEY` is missing or the gateway returns 401, tell the
user to run `grant <agent> zoho-mail` in Slack. Don't try to call the
Zoho API directly — the gateway is the only supported path.
