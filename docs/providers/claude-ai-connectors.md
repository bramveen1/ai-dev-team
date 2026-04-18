# Claude.ai Connector-Based Providers

## Overview

Claude Code sessions running inside agent containers automatically inherit the connectors configured in the claude.ai account. No token management, no auth headers, no MCP server processes to spin up — the connectors are just available.

These providers use `transport: connector` in the provider registry, which tells the namespacer to skip `.mcp.json` generation for them.

## Available Connectors

| Provider ID | Connector | Capabilities | Notes |
|---|---|---|---|
| `gmail-connector` | Gmail | email | Google Workspace email |
| `m365-connector` | Microsoft 365 | email, calendar | Outlook email + calendar |
| `gcal-connector` | Google Calendar | calendar | Google Workspace calendar |
| `gdrive-connector` | Google Drive | docs | Google Drive documents |

## How It Works

1. **claude.ai account** — An admin configures connectors in the claude.ai organization settings.
2. **Auto-inheritance** — When a Claude Code session starts inside an agent container, it inherits all configured connectors from the account.
3. **No process needed** — Unlike command-based providers, connector providers don't spawn a local MCP server process. The tools are already available in the Claude Code session.
4. **Capability config still applies** — The agent's `capabilities.yaml` still defines instances, accounts, ownership, and permissions for connector-based providers. The capabilities summary is still rendered in the system prompt.

## Permission Enforcement

**This is the key difference from command-based providers.**

| Aspect | Command-based (`transport: command`) | Connector-based (`transport: connector`) |
|---|---|---|
| Tool availability | Only permitted tools are registered (API-level enforcement) | All connector tools are available |
| Permission enforcement | API-level — scopes restrict available tools | **Agent-level only** — WORLDVIEW rules + capabilities summary |
| `.mcp.json` entry | Yes — namespacer generates an entry | No — connector is auto-inherited |
| Token management | Managed via secrets store + OAuth refresh | Managed by claude.ai platform |

### What this means in practice

For a connector-based instance like `email_bram` with `permissions: [read, draft-create, draft-update, draft-delete]`:

- The agent **can see** send-related tools from the M365 connector (they are available in the session).
- The agent **must not use** send tools because `send` is not in its permission list.
- Enforcement relies on the WORLDVIEW rule: *"Respect your capability permissions. If a permission is not listed for an instance, do not attempt that action, even if the tool is technically available."*
- The capabilities summary in the system prompt explicitly lists only the granted permissions, reinforcing the boundary.

## Configuration Example

### Provider registry (`config/providers.yaml`)

```yaml
m365-connector:
  transport: connector
  capabilities: [email, calendar]
  permission_scopes:
    email:
      read: "Mail.Read"
      send: "Mail.Send"
      draft-create: "Mail.ReadWrite"
      draft-update: "Mail.ReadWrite"
      draft-delete: "Mail.ReadWrite"
    calendar:
      read: "Calendars.Read"
      propose: "Calendars.ReadWrite"
      book: "Calendars.ReadWrite"
```

Note: `command`, `args`, `env_template`, `secrets_map`, and `oauth` are all omitted — they are not needed for connector providers.

### Agent capabilities (`config/capabilities.yaml`)

```yaml
lisa:
  agent: lisa
  capabilities:
    email:
      - instance: bram
        provider: m365-connector    # connector — auto-inherited, no process spawned
        account: bram@pathtohired.com
        ownership: delegate
        permissions: [read, draft-create, draft-update, draft-delete]

    calendar:
      - instance: bram
        provider: m365-connector
        account: bram@pathtohired.com
        ownership: delegate
        permissions: [read, propose]
```

### Generated `.mcp.json`

Running `python -m capabilities mcp_config lisa` will **not** include `email_bram` or `calendar_bram` in the output — they are connector-based and auto-inherited.

### System prompt rendering

Running `python -m capabilities render lisa` will still include connector-based instances in the capabilities summary:

```
### email
- **email_bram** (delegate) — bram@pathtohired.com via m365-connector
  Permissions: read, draft-create, draft-update, draft-delete
  Note: delegate account — no send permission. Create drafts and notify bram for review.

### calendar
- **calendar_bram** (delegate) — bram@pathtohired.com via m365-connector
  Permissions: read, propose
  Note: delegate account — booking requires approval.
```

## Adding a New Connector Provider

1. Verify the connector is configured in the claude.ai organization settings.
2. Add a provider entry to `config/providers.yaml` with `transport: connector`.
3. Define `capabilities` and `permission_scopes` to map permission verbs to provider-specific scopes (for documentation and future audit purposes).
4. Reference the provider in agent capability configs as usual.
5. No MCP server implementation needed — the connector handles everything.
