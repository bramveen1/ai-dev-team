# Runbook: Swap a Provider

Walk-through for changing which provider backs an existing capability instance. Example: moving Lisa's personal inbox from Zoho to Gmail, or moving Bram's delegate email from the M365 connector to the command-transport M365 MCP.

Budget: **under 30 minutes** if credentials are already in place.

## The one-line change

The swap itself is a single YAML field:

```yaml
# config/capabilities.yaml
agents:
  lisa:
    capabilities:
      email:
        - instance: mine
          provider: zoho-mcp     # <-- change this
          account: lisa@pathtohired.com
          ownership: self
          permissions: [read, send, archive, draft-create, draft-update, draft-delete]
```

becomes:

```yaml
          provider: gmail-connector
```

Everything else can stay — the instance name (`mine`), account, ownership, and permissions are portable across providers. The role.md references `email_mine` by namespace, not by provider, so it keeps working.

The rest of this runbook is the wrap-around: credentials, state migration, testing.

## Checklist

- [ ] 1. Confirm the new provider exists and supports the capability
- [ ] 2. Provision credentials for the new provider
- [ ] 3. Inventory open state tied to the old provider
- [ ] 4. Edit the `provider:` field
- [ ] 5. Restart the agent session
- [ ] 6. Smoke test
- [ ] 7. (If needed) migrate or discard old drafts
- [ ] 8. Remove old credentials
- [ ] 9. Update `docs/agents.md`
- [ ] 10. Tests

---

## 1. Confirm the new provider exists and supports the capability

Open `config/providers.yaml` and check:

- The new provider has an entry.
- Its `capabilities:` list includes the capability you're switching (e.g. `email`).
- Its `permission_scopes.<capability>` covers **every permission** your instance grants. If the old provider had `read, send, archive, draft-*` and the new provider's scope map only covers `read, send, draft-*`, you'll lose `archive` — either drop it from the instance permissions or add the scope mapping.

If the provider is missing any piece, first complete [add-a-new-provider.md](add-a-new-provider.md).

## 2. Provision credentials

| Transport | What you need |
|---|---|
| `connector` | Enable the connector in the claude.ai organization settings. No tokens to copy. |
| `command` + API key | Set the env var in `.env` (e.g. `GMAIL_OAUTH_TOKEN=...`). |
| `command` + OAuth | Run the provider's initial device-code flow, save the token set to `config/secrets/<provider>.json`. |

For OAuth-based command providers, check the secrets file exists and has fresh tokens before restarting the agent:

```bash
python -m capabilities refresh_tokens
```

This is a no-op if tokens aren't expired, and safe to run anytime.

## 3. Inventory open state tied to the old provider

Before you flip the config, identify anything that is provider-specific and might be stranded:

| State | Where it lives | What happens on swap |
|---|---|---|
| Pending approval drafts | `data/drafts.db` (SQLite, row per draft) | Draft rows still reference the old `provider` string; the "Open in <App>" deep link still points at the old provider's UI. Decide per-draft: approve-then-swap, or discard. |
| In-flight OAuth tokens | `config/secrets/<old-provider>.json` | Safe to leave. You can delete after a few sessions confirm the swap worked. |
| Drafts physically stored in the old provider | In the old service (e.g. actual Outlook drafts for M365) | The new provider can't see them. Either send/discard them in the old app, or accept that they'll linger. |
| Agent memory (`config/agents/<name>/memory/`) | Markdown files | References to "email_mine" still work — capability instance names are stable. No action needed. |
| Slack thread history | Slack | References agent by name, not provider. No action needed. |

### Decision tree for open drafts

```
Any pending approval drafts for the affected instance?
├── No  → proceed.
└── Yes → for each draft:
         ├── Still relevant? → user approves it now in old provider; router marks it sent; proceed.
         └── Not relevant?   → user discards it (Slack button); router deletes from old provider; proceed.
```

Query open drafts for an agent + capability:

```bash
sqlite3 data/drafts.db \
  "SELECT draft_id, capability_type, created_at FROM drafts
   WHERE agent='lisa' AND capability_type='email' AND status='pending';"
```

## 4. Edit the `provider:` field

Open `config/capabilities.yaml` and change only the `provider:` line for the affected instance.

If any permissions don't map in the new provider, either:
- Remove them from the instance (preferred — matches the new reality), or
- Map them at the provider level by adding a `permission_scopes` entry (if the API supports it).

Verify the config loads:

```bash
python -m capabilities render <agent>
python -m capabilities mcp_config <agent>
```

The rendered summary should show the new provider name next to the instance. The `.mcp.json` should have either an updated server entry (command) or omit it entirely (connector).

## 5. Restart the agent session

The agent's MCP config is generated at session start. Kick the container so it picks up the change:

```bash
docker compose restart <agent>
```

For connector swaps, the effect is immediate (no process spawn needed). For command-transport swaps, confirm the new MCP server launched:

```bash
docker compose logs <agent> | grep mcp
```

## 6. Smoke test

Run these in order. If any fails, roll back by reverting the `provider:` change and restarting.

- [ ] DM the agent: "list your capabilities." The summary should show the instance with the **new** provider name.
- [ ] For a `read` permission: "read the latest message in <instance>." Expect a real message.
- [ ] For a `draft-create` permission: "draft a reply to the latest message." Expect an approval card. Deep link opens in the new provider's UI.
- [ ] For a `send` permission (only on `self` instances): send a test email to yourself. Verify it arrives from the new provider's account.
- [ ] Open an approval card from the swap and confirm the "Open in <App>" button points at the new provider's UI.
- [ ] Trigger a scheduled task that uses the instance (e.g. daily inbox review) manually via `/tasks` and confirm output looks correct.

## 7. Migrate or discard old drafts

If you left drafts in "pending" state during step 3, clean them up now. Options:

- **Approve**: the user hits Approve on the old draft; the router will execute via the old provider's MCP. This only works if the old provider is still reachable — you may want to keep it wired for a short bake period, rather than pulling credentials immediately.
- **Discard**: user hits Discard on each draft. The router marks the draft discarded and tries to delete the underlying draft in the old service (best-effort; failures are logged).
- **Expire**: let the existing TTL expire the draft (see `approval_ttls` in `config/baseline.yaml`). Cleanest, slowest.

## 8. Remove old credentials

Once the swap is validated and no drafts reference the old provider:

- Delete `config/secrets/<old-provider>.json` (if OAuth).
- Remove env vars for the old provider from `.env` and `docker-compose.yml` (if API-key).
- If no other agent uses the old provider, you can also prune its entry from `config/providers.yaml` — but leave it if there's any chance of swapping back.

Do this as a **separate commit**, after a few days of successful runs. It's a trivial rollback window to have.

## 9. Update `docs/agents.md`

Change the provider column for the affected capability row. Keep the commit message in the form `docs(agents): lisa email/mine now on gmail-connector`.

## 10. Tests

A swap is mostly config; tests guard the edges.

### Required

- **`tests/unit/capabilities/test_loader.py`** — add a case that loads a fixture capabilities.yaml with the new provider and asserts no error. If the new provider doesn't map every granted permission, the loader should raise (a guard against silent permission loss).
- **`tests/unit/capabilities/test_prompt_renderer.py`** — assert the rendered summary includes the new provider name for the affected instance.

### Recommended

- **`tests/unit/capabilities/test_mcp_namespacer.py`** — if you swapped to a different transport, assert `.mcp.json` correctly includes/excludes the entry.
- **`tests/unit/approvals/test_deep_links.py`** — assert the new `(capability, provider)` pair has a deep-link generator registered (or document that deep links aren't available and the approval card uses the fallback).

### Run before opening a PR

```bash
.venv/bin/pytest tests/unit/capabilities tests/unit/approvals -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

---

## Rollback

Swaps are one-line; rollbacks are the same one line in reverse.

```bash
git checkout -- config/capabilities.yaml
docker compose restart <agent>
```

If you already removed old credentials (step 8), you'll need to restore them before the rollback restart succeeds. This is why step 8 is intentionally last and separated.

## FAQ

**Q: Do I need to update the agent's role.md?**
No. role.md references capabilities by namespace (`email_mine`), not by provider. That's the point.

**Q: Do I need to rewrite memory?**
No. Agent memory references capabilities by namespace too.

**Q: What if the new provider uses different scope names?**
That's the whole function of `permission_scopes` in the provider registry. Permission verbs (`send`, `read`) are capability-level; scope strings are provider-level. The framework bridges them.

**Q: Can I swap providers for only one instance while leaving others on the old provider?**
Yes. Each instance has its own `provider:` field. Lisa's `email_mine` can be Gmail while her `email_bram` stays on M365.

**Q: Can I A/B swap — route some calls to both for comparison?**
Not natively. Create a second instance (`email_mine_v2` pointing at the new provider) for a bake period, then remove the old one.
