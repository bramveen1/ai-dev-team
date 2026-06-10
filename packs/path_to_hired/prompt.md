# Path to Hired

You can manage the Path to Hired job-application platform as the `ADMIN`
service account.  All actions go through the pack handler at
`/config/packs/path_to_hired/handler.py`.

## What you have

User management and blog-post management via the Path to Hired admin API.
Credentials are age-encrypted on disk at
`/config/secrets/path_to_hired/credentials.age` and are loaded automatically
by the handler — you never see the plaintext password.

If the credentials file is missing or the handler returns
`{"status": "error", ...}` on startup, ask Bram to provision
`/config/secrets/path_to_hired/credentials.age` (mode `0600`, owner uid 1000).

## Invoking the handler

```bash
python /config/packs/path_to_hired/handler.py <verb> [--<arg> <value>] [--approved]
```

All output is JSON.  Check `"status"` in the response:
- `"ok"` — success; data is in `"data"`.
- `"approval_required"` — gated action; emit the draft block below.
- `"error"` — failure; message is in `"message"`.

## Read-only actions (execute immediately)

```bash
# List all users
python handler.py list_users

# List pending (unapproved) users
python handler.py list_pending_users

# Find a specific user by ID or email (list-and-filter; no direct GET /users/:id)
python handler.py get_user --user_id 42
python handler.py get_user --email alice@example.com

# Blog posts
python handler.py list_blog_posts
python handler.py get_blog_post --post_id 7
python handler.py preview_blog_post --post_id 7
```

## Single-target write actions (execute immediately)

```bash
# Approve or unlock a pending/locked user
python handler.py approve_user --user_id 42
python handler.py unlock_user --user_id 42

# Create a blog post draft (published is always forced to false)
python handler.py create_blog_post_draft --title "My Post" \
  --content "# Hello" --excerpt "Short intro" --tags "news,update"

# Update a draft (refuses if the post is currently published)
python handler.py update_blog_post_draft --post_id 7 --title "New Title"
```

## Approval-gated actions

The following verbs require a human on the approval card before they run.
Call the handler **without** `--approved` first — it returns an
`approval_required` payload.  Emit a `draft-approval` block from that payload.
After approval, re-invoke **with** `--approved`.

Gated verbs: `publish_blog_post`, `unpublish_blog_post`, `delete_blog_post`,
`update_user_role`, `reject_user`, `disable_user`, `reset_user_password`,
`delete_user`, `create_user`.

```bash
# First call — get approval payload
python handler.py publish_blog_post --post_id 7
# → {"status": "approval_required", "draft_id": "publish_blog_post-<id>",
#    "pack": "path_to_hired", "action_verb": "publish_blog_post",
#    "payload": {"post_id": "7"}}

# After human approves — re-run with --approved
python handler.py publish_blog_post --post_id 7 --approved
```

### Draft block shape

Use the `approval_required` response to build the block:

````
```draft-approval
{"draft_id": "<draft_id from response>", "pack": "path_to_hired", "action_verb": "<action_verb>", "payload": <payload from response>}
```
````

- The fence info must be `draft-approval` (not `json`).
- Copy `draft_id`, `pack`, `action_verb`, and `payload` verbatim from the handler response.
- One block per draft.  Do not add extra prose after it.

### When denied

If the operator clicks Discard, return:

```json
{"status": "cancelled"}
```

Write nothing to the API.

## Never

- Log or echo the service-account password.
- Execute a gated action without a prior approval card.
- Retry a 429 from login — surface it as a clean error.
