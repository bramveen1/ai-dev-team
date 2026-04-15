# Capability Framework Architecture

## Why this exists

Phase 1 hardcodes tools per agent via `config/agent_tools.json` and static system docs in `/systems/*.md`. This works for one agent with a fixed toolset, but breaks down when:

- An agent needs **multiple accounts** for the same capability (Lisa's own Zoho inbox + delegated access to Bram's M365 inbox)
- Different accounts need **different permissions** (Lisa can send from her own inbox but can only draft from Bram's)
- We want to **swap providers** without rewriting role files (move Lisa from Zoho to Gmail)
- We need to **audit** what an agent can actually do at a glance

The capability framework replaces the flat tool list with a structured model: **capability type -> instance -> provider**.

---

## The 3-layer model

### Layer 1: Capability type

An abstract verb — what the agent can *do*, not how it does it.

| Capability | What it covers |
|---|---|
| `email` | Read, compose, send, manage messages |
| `calendar` | View, propose, book events |
| `chat` | Read/send messages in team channels |
| `web` | Browse, research, scrape |
| `analytics` | Query product/usage data |
| `design` | Access design files and assets |
| `code-repo` | Issues, PRs, code review |
| `social` | Post to social platforms |
| `docs` | Read/write documentation, wikis |
| `marketing` | Email campaigns, automation |

New capability types get added as the team grows. The list isn't closed.

**Why this layer exists:** It lets the system prompt give agents a human-readable summary of what they can do ("You have email access to two accounts and calendar access to one") without coupling to specific providers. It also enables permission reasoning at a semantic level — "can this agent send email?" is answerable without knowing whether the provider is Zoho, M365, or Gmail.

### Layer 2: Capability instance

A concrete, configured bundle: one account, one set of permissions, one ownership relationship.

```yaml
instance: bram              # unique name within this capability for this agent
provider: m365-mcp          # which MCP server implements this
account: bram@pathtohired.com
ownership: delegate          # self | delegate | shared
permissions:
  - read
  - draft-create
  - draft-update
  - draft-delete
```

**Why this layer exists:** The same capability type can appear multiple times per agent. Lisa has her own email *and* delegated access to Bram's. Each instance has its own permissions, its own account, its own provider. Without instances, you can't model "read from both inboxes, send from only one."

### Layer 3: Provider

The actual MCP server that implements the capability for a given instance. One provider can back multiple instances (m365-mcp serves both Bram's email and Bram's calendar). One capability can use different providers across instances (Lisa's own email is Zoho, Bram's is M365).

**Why this layer exists:** It decouples "what the agent can do" from "which service does it." Swapping Lisa's personal email from Zoho to Gmail changes the provider field on one instance — the capability, permissions, and system prompt rendering stay the same.

---

## Instance metadata schema

Each instance is defined with these fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `instance` | string | yes | Unique name within this agent+capability. Used in MCP namespacing. Examples: `mine`, `bram`, `team` |
| `provider` | string | yes | MCP server identifier. Must match an entry in the provider registry. Examples: `m365-mcp`, `zoho-mcp`, `github-mcp` |
| `account` | string | yes | The account/resource identifier. Email address, org name, project ID, etc. |
| `ownership` | enum | yes | Relationship to the account. One of: `self` (agent's own account), `delegate` (acting on behalf of someone), `shared` (team-wide resource) |
| `permissions` | list[string] | yes | Allowed actions from the permission vocabulary. Explicit allowlist — if it's not listed, the agent must not do it. |
| `config` | dict | no | Provider-specific configuration (API keys, endpoints, scopes). Injected as environment variables into the MCP server. |

### Ownership semantics

- **`self`** — The agent owns this account. Full autonomy within the granted permissions. No approval flow needed for permitted actions.
- **`delegate`** — The agent acts on behalf of a human. Higher-impact actions (sending, booking) should go through an approval flow even if technically permitted. The specific approval triggers are defined per capability type.
- **`shared`** — Team-wide resource (e.g., a shared GitHub org, a team Notion workspace). Multiple agents may have instances pointing to the same account with different permission sets.

---

## Permission vocabulary

Permissions are verbs scoped to a capability type. Each capability defines its own vocabulary.

### email

| Permission | Description |
|---|---|
| `read` | Read messages and threads |
| `send` | Send messages (new or reply) |
| `draft-create` | Create a new draft |
| `draft-update` | Modify an existing draft |
| `draft-delete` | Delete a draft |
| `archive` | Archive/move messages |
| `label` | Apply labels/categories |

### calendar

| Permission | Description |
|---|---|
| `read` | View events and availability |
| `propose` | Suggest a meeting time (creates a tentative/draft event, requires human confirmation) |
| `book` | Create confirmed events directly |
| `update` | Modify existing events |
| `cancel` | Cancel/decline events |

### code-repo

| Permission | Description |
|---|---|
| `read` | Browse code, issues, PRs |
| `comment` | Comment on issues and PRs |
| `issue-create` | Create new issues |
| `pr-create` | Open pull requests |
| `merge` | Merge pull requests |
| `branch-create` | Create branches |

### social

| Permission | Description |
|---|---|
| `read` | Browse feeds, profiles, mentions |
| `draft` | Create draft posts for review |
| `publish` | Post directly to the platform |
| `reply` | Reply to existing posts/comments |

### analytics

| Permission | Description |
|---|---|
| `read` | Query dashboards, funnels, metrics |
| `annotate` | Add annotations to events/data |
| `configure` | Create/modify dashboards and queries |

### docs

| Permission | Description |
|---|---|
| `read` | Read documents, pages, wikis |
| `write` | Create and edit documents |
| `comment` | Add comments/suggestions |
| `organize` | Move, archive, tag documents |

### marketing

| Permission | Description |
|---|---|
| `read` | View campaigns, templates, stats |
| `draft` | Create campaign drafts |
| `send` | Trigger campaign sends |
| `configure` | Modify lists, templates, automations |

### web

| Permission | Description |
|---|---|
| `browse` | Navigate web pages |
| `screenshot` | Capture page screenshots |
| `interact` | Fill forms, click elements (Playwright) |

### design

| Permission | Description |
|---|---|
| `read` | View designs, components, assets |
| `comment` | Add comments/annotations on designs |
| `export` | Export assets and images |

### chat

| Permission | Description |
|---|---|
| `read` | Read channel messages and threads |
| `send` | Post messages |
| `react` | Add reactions |

Permission lists are **allowlists**. If a permission isn't listed for an instance, the agent must not perform that action through that instance.

---

## MCP namespacing

Each capability instance becomes one entry in the agent's MCP configuration, named `{capability}_{instance}`.

### Convention

```
{capability_type}_{instance_name}
```

Examples:
- `email_mine` — Lisa's own Zoho inbox
- `email_bram` — Lisa's delegated access to Bram's M365 inbox
- `calendar_bram` — Lisa's access to Bram's M365 calendar
- `code-repo_pathtohired` — Sam's GitHub access
- `analytics_posthog` — Alex's PostHog access

### Generated `.mcp.json`

At session start, the framework generates an `.mcp.json` for the agent container. Each instance maps to one MCP server entry:

```json
{
  "mcpServers": {
    "email_mine": {
      "command": "npx",
      "args": ["-y", "@zoho/zoho-mcp"],
      "env": {
        "ZOHO_ACCOUNT": "lisa@pathtohired.com",
        "ZOHO_API_KEY": "${ZOHO_API_KEY}"
      }
    },
    "email_bram": {
      "command": "npx",
      "args": ["-y", "@microsoft/m365-mcp"],
      "env": {
        "M365_ACCOUNT": "bram@pathtohired.com",
        "M365_ACCESS_TOKEN": "${M365_ACCESS_TOKEN}",
        "M365_SCOPES": "Mail.Read,Mail.ReadWrite"
      }
    },
    "calendar_bram": {
      "command": "npx",
      "args": ["-y", "@microsoft/m365-mcp"],
      "env": {
        "M365_ACCOUNT": "bram@pathtohired.com",
        "M365_ACCESS_TOKEN": "${M365_ACCESS_TOKEN}",
        "M365_SCOPES": "Calendars.Read,Calendars.ReadWrite"
      }
    }
  }
}
```

Note: the same provider binary (m365-mcp) can appear multiple times with different configurations. Each entry is an independent MCP server process.

### Why one server per instance

An alternative is one MCP server per provider, exposing all accounts. We don't do that because:

1. **Permission isolation** — Each MCP server only exposes tools for its configured scopes. `email_bram` literally cannot expose a `send` tool if the M365 scopes don't include `Mail.Send`.
2. **Failure isolation** — If `email_mine` crashes, `email_bram` keeps working.
3. **Auditability** — The agent sees `email_bram.draft-create` in its tool list, not `m365.createDraft(account=bram)`. The namespace makes the permission boundary visible.

---

## Loading order at session start

The system prompt is assembled in this order:

```
1. WORLDVIEW.md             — Universal behavioral rules (all agents)
2. Capabilities summary — Auto-generated from the agent's capability config (NEW)
3. role.md             — Agent job description and responsibilities
4. personality.md      — Agent voice, tone, quirks
5. agent memory.md     — Agent's accumulated knowledge
6. org MEMORY.md       — Org-wide current context
```

The capabilities summary (step 2) is new. It's generated at session start from the agent's capability config and injected between WORLDVIEW and role. This means the role.md can reference capabilities by name ("use your email_bram access to check Bram's inbox") without defining them.

### Rendered capabilities summary

The framework generates a human-readable block from the config:

```markdown
## Your Capabilities

### email
- **email_mine** (self) — lisa@pathtohired.com via zoho-mcp
  Permissions: read, send, archive, draft-create, draft-update, draft-delete
- **email_bram** (delegate) — bram@pathtohired.com via m365-mcp
  Permissions: read, draft-create, draft-update, draft-delete
  Note: delegate account — send requires approval

### calendar
- **calendar_bram** (delegate) — bram@pathtohired.com via m365-mcp
  Permissions: read, propose
  Note: delegate account — booking requires approval
```

This summary:
- Groups instances by capability type
- Shows the namespace, ownership, account, and provider for each instance
- Lists explicit permissions
- Adds a note for delegate/shared instances about approval requirements
- Is deterministic — same config always produces the same summary

---

## Permission enforcement

Permissions are enforced at two levels, in order of preference.

### Level 1: API-level enforcement (preferred)

The MCP server for an instance is configured with only the scopes/credentials needed for the granted permissions. If Lisa's `email_bram` instance doesn't include `send`, the M365 MCP server is configured without `Mail.Send` scope. The tool literally doesn't exist — the agent can't call it.

This is the strongest enforcement. The agent cannot bypass it even if the WORLDVIEW/role instructions are ignored.

**How it works in practice:**
- The capability config's `permissions` list maps to provider-specific scopes/features
- The provider registry (see "Adding a new provider") defines this mapping
- At session start, the MCP server is launched with only the mapped scopes
- Tools that require excluded scopes are not registered by the MCP server

### Level 2: Agent-level enforcement (fallback)

When the provider's API granularity doesn't match the permission model (e.g., the API grants read+write together but we only want read), enforcement falls back to:

1. **WORLDVIEW rule** — The WORLDVIEW.md contains a universal rule: "Respect your capability permissions. If a permission is not listed for an instance, do not attempt that action, even if the tool is technically available."
2. **Capabilities summary** — The auto-generated summary in the system prompt explicitly lists what's allowed. The agent can see its own permission boundaries.
3. **Role.md rule** — The agent's role file can add capability-specific instructions ("Never send from Bram's inbox directly. Always create a draft and notify Bram.")

### What happens when an agent tries a disallowed action

| Scenario | Enforcement | Outcome |
|---|---|---|
| Agent calls `email_bram.send` but `send` not in permissions and API scope excludes it | API-level | Tool doesn't exist. MCP server returns "unknown tool" error. |
| Agent calls `email_bram.send` but `send` not in permissions and API scope includes it (granularity mismatch) | Agent-level | Agent should self-refuse based on WORLDVIEW rule + capabilities summary. If it doesn't, this is a framework bug — file an issue and tighten the provider. |
| Agent calls `email_bram.draft-create` with `draft-create` in permissions | Allowed | Action proceeds normally. |

The goal is to push as much enforcement as possible to Level 1 over time. Level 2 is a safety net, not a primary mechanism.

---

## Capability configuration format

Each agent's capabilities are defined in a YAML file at `agents/{agent}/capabilities.yaml`:

```yaml
# agents/lisa/capabilities.yaml

agent: lisa

capabilities:
  email:
    - instance: mine
      provider: zoho-mcp
      account: lisa@pathtohired.com
      ownership: self
      permissions:
        - read
        - send
        - archive
        - draft-create
        - draft-update
        - draft-delete

    - instance: bram
      provider: m365-mcp
      account: bram@pathtohired.com
      ownership: delegate
      permissions:
        - read
        - draft-create
        - draft-update
        - draft-delete

  calendar:
    - instance: bram
      provider: m365-mcp
      account: bram@pathtohired.com
      ownership: delegate
      permissions:
        - read
        - propose
```

### Provider registry

Providers are defined in `config/providers.yaml`:

```yaml
# config/providers.yaml

providers:
  zoho-mcp:
    command: npx
    args: ["-y", "@zoho/zoho-mcp"]
    capabilities: [email]
    permission_scopes:
      email:
        read: "Mail.Read"
        send: "Mail.Send"
        archive: "Mail.Archive"
        draft-create: "Mail.Draft"
        draft-update: "Mail.Draft"
        draft-delete: "Mail.Draft"
    env_template:
      ZOHO_ACCOUNT: "{account}"
      ZOHO_API_KEY: "${ZOHO_API_KEY}"

  m365-mcp:
    command: npx
    args: ["-y", "@microsoft/m365-mcp"]
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
    env_template:
      M365_ACCOUNT: "{account}"
      M365_ACCESS_TOKEN: "${M365_ACCESS_TOKEN}"
      M365_SCOPES: "{computed_scopes}"

  github-mcp:
    command: npx
    args: ["-y", "@github/github-mcp"]
    capabilities: [code-repo]
    permission_scopes:
      code-repo:
        read: "repo:read"
        comment: "repo:write"
        issue-create: "repo:write"
        pr-create: "repo:write"
        merge: "repo:write"
        branch-create: "repo:write"
    env_template:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"
      GITHUB_OWNER: "{account}"
```

The `{computed_scopes}` placeholder is resolved at startup by collecting the unique scopes for all granted permissions.

---

## Worked example: Lisa's email

End-to-end walkthrough of Lisa's two email inboxes.

### 1. Configuration

```yaml
# agents/lisa/capabilities.yaml
capabilities:
  email:
    - instance: mine
      provider: zoho-mcp
      account: lisa@pathtohired.com
      ownership: self
      permissions: [read, send, archive, draft-create, draft-update, draft-delete]

    - instance: bram
      provider: m365-mcp
      account: bram@pathtohired.com
      ownership: delegate
      permissions: [read, draft-create, draft-update, draft-delete]
```

Key decisions:
- Lisa's own inbox (`mine`): full permissions including `send`. She owns this account.
- Bram's inbox (`bram`): no `send`, no `archive`. She can read and draft, but cannot send on Bram's behalf or reorganize his inbox.

### 2. MCP namespace resolution

The framework reads the config and generates two MCP server entries:

```json
{
  "mcpServers": {
    "email_mine": {
      "command": "npx",
      "args": ["-y", "@zoho/zoho-mcp"],
      "env": {
        "ZOHO_ACCOUNT": "lisa@pathtohired.com",
        "ZOHO_API_KEY": "${ZOHO_API_KEY}"
      }
    },
    "email_bram": {
      "command": "npx",
      "args": ["-y", "@microsoft/m365-mcp"],
      "env": {
        "M365_ACCOUNT": "bram@pathtohired.com",
        "M365_ACCESS_TOKEN": "${M365_ACCESS_TOKEN}",
        "M365_SCOPES": "Mail.Read,Mail.ReadWrite"
      }
    }
  }
}
```

Note that `email_bram` gets `Mail.Read` and `Mail.ReadWrite` (for drafts) but not `Mail.Send`. The M365 MCP server won't register a `send` tool when `Mail.Send` isn't in the scopes.

### 3. System prompt rendering

The capabilities summary injected into Lisa's system prompt:

```markdown
## Your Capabilities

### email
- **email_mine** (self) — lisa@pathtohired.com via zoho-mcp
  Permissions: read, send, archive, draft-create, draft-update, draft-delete
- **email_bram** (delegate) — bram@pathtohired.com via m365-mcp
  Permissions: read, draft-create, draft-update, draft-delete
  Note: delegate account — no send permission. Create drafts and notify Bram for review.
```

### 4. Runtime behavior

**Scenario A: "Lisa, reply to that recruiter from my inbox"**

1. Lisa uses `email_bram.read` to find the recruiter's message
2. Lisa uses `email_bram.draft-create` to compose a reply draft
3. Lisa posts to Slack: "Done — drafted a reply in your inbox. Here's what I wrote: [summary]. Send it when it looks good, or tell me what to change."

Lisa doesn't try to send because:
- API-level: the `email_bram` MCP server has no `send` tool (Mail.Send scope excluded)
- Agent-level: the capabilities summary says no `send` permission, and her role.md says to create drafts and notify Bram

**Scenario B: "Lisa, send the weekly update from your account"**

1. Lisa uses `email_mine.draft-create` to compose the update
2. Lisa uses `email_mine.send` to send it directly
3. Lisa posts to Slack: "Sent the weekly update to [recipients]."

No approval needed — `mine` is a `self` account with `send` permission.

**Scenario C: "Lisa, send this from my account" (permission violation attempt)**

1. Lisa checks her capabilities summary — `email_bram` has no `send` permission
2. Lisa responds: "I can't send from your account directly — I only have draft access. Want me to draft it so you can hit send?"

If Lisa somehow ignores the WORLDVIEW rule and tries anyway, the MCP server returns an error because the `send` tool doesn't exist.

---

## How to add a new capability type

Example: adding `crm` as a capability.

### Step 1: Define the permission vocabulary

Decide what verbs make sense for CRM operations:

| Permission | Description |
|---|---|
| `read` | View contacts, deals, pipelines |
| `create` | Create new contacts or deals |
| `update` | Modify existing records |
| `delete` | Remove records |
| `export` | Export data |

### Step 2: Add it to the agent's capabilities config

```yaml
# agents/alex/capabilities.yaml
capabilities:
  crm:
    - instance: pathtohired
      provider: hubspot-mcp
      account: pathtohired
      ownership: shared
      permissions: [read, create, update]
```

### Step 3: Register the provider (if new)

If `hubspot-mcp` isn't in the provider registry yet, add it (see next section).

### Step 4: Update this document

Add the permission vocabulary table to the "Permission vocabulary" section above.

That's it. The framework handles MCP namespacing (`crm_pathtohired`), system prompt rendering, and `.mcp.json` generation automatically.

---

## How to add a new provider for an existing capability

Example: adding Gmail as an email provider.

### Step 1: Add to provider registry

```yaml
# config/providers.yaml
providers:
  gmail-mcp:
    command: npx
    args: ["-y", "@google/gmail-mcp"]
    capabilities: [email]
    permission_scopes:
      email:
        read: "gmail.readonly"
        send: "gmail.send"
        draft-create: "gmail.compose"
        draft-update: "gmail.compose"
        draft-delete: "gmail.compose"
        archive: "gmail.modify"
        label: "gmail.labels"
    env_template:
      GMAIL_ACCOUNT: "{account}"
      GMAIL_OAUTH_TOKEN: "${GMAIL_OAUTH_TOKEN}"
```

### Step 2: Use it in a capability instance

```yaml
# agents/lisa/capabilities.yaml
capabilities:
  email:
    - instance: mine
      provider: gmail-mcp          # swapped from zoho-mcp
      account: lisa@pathtohired.com
      ownership: self
      permissions: [read, send, archive, draft-create, draft-update, draft-delete]
```

The permission vocabulary stays the same — `read`, `send`, `draft-create`, etc. are capability-level verbs. The provider registry maps them to provider-specific scopes.

---

## How to swap providers for an agent

Example: moving Lisa's personal email from Zoho to Gmail.

### Step 1: Change the provider field

```yaml
# Before
- instance: mine
  provider: zoho-mcp
  account: lisa@pathtohired.com

# After
- instance: mine
  provider: gmail-mcp
  account: lisa@pathtohired.com
```

### Step 2: Set up credentials

Ensure the new provider's credentials are available as environment variables (e.g., `GMAIL_OAUTH_TOKEN`).

### Step 3: Restart the agent session

The next session start picks up the new config, generates a new `.mcp.json` with `gmail-mcp` backing `email_mine`, and renders the updated capabilities summary.

Nothing else changes. The instance name (`mine`), permissions, and ownership stay the same. The role.md references `email_mine` — it doesn't know or care which provider backs it. Slack history and memory remain valid because they reference capability/instance names, not providers.

---

## Design decisions and trade-offs

### Why YAML for config, not code

Capabilities are data, not behavior. YAML is readable by non-developers, diffable in PRs, and parseable without importing Python modules. The framework code reads the YAML and generates everything else.

### Why per-instance MCP servers instead of per-provider

See the "Why one server per instance" section above. The short version: permission isolation and auditability outweigh the cost of running a few extra processes. On our Surface Pro 8GB budget, each MCP server is a lightweight Node.js process — the overhead is minimal.

### Why not enforce all permissions at API level

Some providers bundle permissions coarsely (e.g., M365's `Mail.ReadWrite` grants both draft creation and message editing). We can't always split these at the API level. Agent-level enforcement via WORLDVIEW rules fills the gap. The goal is to push providers toward finer-grained scopes over time.

### Why ownership is a first-class field

Ownership drives approval flow behavior. A `self` account with `send` permission means the agent can send freely. A `delegate` account with `send` permission still means the agent should seek approval before sending — ownership changes the behavioral contract even when the technical permission is identical. This distinction matters for trust and safety.
