# Runbook: Add a New Provider

Walk-through for wiring up a new provider — an MCP server that implements a capability for some account.

Budget: **under one hour** for a connector-transport provider. **2–4 hours** for a command-transport provider that needs a new MCP server written from scratch (auth + API integration time dominates).

## First: pick a transport

| Transport | When to use | What you have to build |
|---|---|---|
| `connector` | The service has an official claude.ai connector (Gmail, M365, Google Calendar, Google Drive, Notion, Linear…) | Just a registry entry. No server, no tokens. |
| `command` | No connector exists, or you need API-level permission enforcement (scope-restricted tools) | An MCP server process + secrets wiring. |

See [docs/providers/claude-ai-connectors.md](providers/claude-ai-connectors.md) for the current connector list and the trade-off discussion.

## Checklist (both transports)

- [ ] 1. Pick a stable provider ID
- [ ] 2. Add an entry to `config/providers.yaml`
- [ ] 3. (command only) Write or vendor the MCP server under `mcps/<name>/`
- [ ] 4. (command only) Add secrets file + env var conventions
- [ ] 5. Add deep-link generator (if the provider has approval flows)
- [ ] 6. Create `docs/providers/<provider>.md`
- [ ] 7. Use it in an agent's `config/capabilities.yaml`
- [ ] 8. Integration test
- [ ] 9. Link the provider doc from [docs/agents.md](agents.md) for each agent using it

---

## 1. Pick a stable provider ID

Conventions:

- Lowercase, hyphenated.
- **Suffix indicates transport**: `-mcp` for command, `-connector` for connector.
- Product-name-first: `gmail-connector`, `hubspot-mcp`, `posthog-mcp`, `figma-mcp`.

This ID is stable forever — it's referenced from every agent's `capabilities.yaml`. Changing it later is a rename across multiple files. Pick carefully.

## 2. Add an entry to `config/providers.yaml`

### Connector transport

```yaml
providers:
  notion-connector:
    transport: connector
    capabilities: [docs]
    permission_scopes:
      docs:
        read: "notion.read"
        write: "notion.update"
        comment: "notion.comment"
```

Connector entries skip `command`, `args`, `env_template`, `secrets_map`, and `oauth`. `permission_scopes` is retained purely for documentation and future audit — the claude.ai platform does the actual scope negotiation.

### Command transport

```yaml
providers:
  hubspot-mcp:
    command: npx
    args: ["-y", "@hubspot/mcp"]
    capabilities: [crm]
    permission_scopes:
      crm:
        read: "crm.objects.contacts.read"
        create: "crm.objects.contacts.write"
        update: "crm.objects.contacts.write"
        delete: "crm.objects.contacts.write"
        export: "crm.export"
    env_template:
      HUBSPOT_TOKEN: "${HUBSPOT_TOKEN}"
      HUBSPOT_PORTAL: "{account}"
      HUBSPOT_SCOPES: "{computed_scopes}"
    secrets_map:
      HUBSPOT_TOKEN: "hubspot:access_token"
    oauth:
      authority: "https://app.hubspot.com"
      token_path: "/oauth/v1/token"
```

Fields:

- **`command` / `args`** — what to spawn. Use `npx -y @scope/pkg` for Node MCPs, `python -m mcps.<name>` for in-repo Python MCPs.
- **`capabilities`** — which capability types this provider can back. Multiple is fine (see `m365-mcp` backing both `email` and `calendar`).
- **`permission_scopes.<capability>.<permission>`** — maps each permission verb to the provider-specific scope string. The framework unions these for granted permissions and injects them as `{computed_scopes}` in `env_template`.
- **`env_template`** — env vars passed to the server process. Placeholders:
  - `{account}` — the instance's account field.
  - `{computed_scopes}` — union of scopes for granted permissions.
  - `${VAR}` — substituted from the process env (for simple secrets).
- **`secrets_map`** — env var → `provider:key` reference in the secrets store. Used for OAuth'd providers where the token is refreshed out-of-band.
- **`oauth`** — metadata for the automatic token refresher (`python -m capabilities refresh_tokens`). Omit for API-key-only providers.

### Env var naming convention

For a provider with ID `<product>-mcp` (or `-connector`), env var names follow `<PRODUCT>_<FIELD>`. Examples: `ZOHO_API_KEY`, `M365_ACCESS_TOKEN`, `GITHUB_TOKEN`, `HUBSPOT_TOKEN`. Keep it consistent so `.env` and docker-compose stay readable.

## 3. Write the MCP server (command transport only)

If the service publishes an official MCP package, just reference it in `args: ["-y", "@vendor/pkg"]` and skip to step 4.

If you need to write one, follow the pattern of `mcps/m365_mail/`:

```
mcps/<name>/
  __init__.py
  __main__.py           # entry point: python -m mcps.<name>
  server.py             # tool registration + dispatch
  <service>_client.py   # HTTP client wrapper (Graph, REST, GraphQL)
```

### Minimum tool surface

- A `list_*` tool — returns a paginated list of items.
- A `read_*` tool — fetches one item by ID.
- Mutating tools named after **permission verbs** of the capability: `create_draft`, `update_draft`, `delete_draft`, `send`, etc.
- A `get_*_url` tool if resources have deep links for human follow-up.

### Permission-gated registration

Only register a tool if its corresponding permission is in scope. Example (simplified):

```python
def build_tools(scopes: set[str]) -> list[Tool]:
    tools = [LIST_TOOL, READ_TOOL]
    if "Mail.ReadWrite.Shared" in scopes:
        tools += [CREATE_DRAFT, UPDATE_DRAFT, DELETE_DRAFT]
    if "Mail.Send" in scopes:
        tools += [SEND_TOOL]   # never registered for the delegate config
    return tools
```

This is the **Level-1 enforcement** referenced in `capability-framework.md`: the LLM literally has no send tool to call when the scope is excluded.

### Stdio vs. HTTP

Default to stdio MCP servers — that's what Claude Code expects. The `mcps/m365_mail/server.py` file shows the pattern (line-delimited JSON-RPC on stdin/stdout).

## 4. Secrets file and env var conventions (command transport only)

If the provider uses OAuth, store the token set in `config/secrets/<name>.json`:

```json
{
  "client_id": "...",
  "tenant_id": "...",
  "client_secret": "...",
  "scopes": "scope1 scope2 offline_access",
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1713200000
}
```

The `config/secrets/` directory is gitignored. Commit a `<name>.example.json` with placeholder values so contributors know the shape.

Add entries to `.env.example` for any simple API-key providers (those without an OAuth flow):

```bash
HUBSPOT_TOKEN=...
```

The secrets store (`capabilities/secrets.py`) reads `config/secrets/<provider>.json`; the OAuth refresher (`capabilities/oauth.py`) handles `access_token` rotation. You shouldn't need to write refresh logic yourself unless the provider's OAuth deviates from the standard RFC 8628 flow.

## 5. Deep-link generator

If the provider has approval-gated actions, add a row to `DEEP_LINK_GENERATORS` in `router/approvals/deep_links.py`:

```python
def hubspot_contact(contact_id: str) -> str:
    return f"https://app.hubspot.com/contacts/{quote(contact_id, safe='')}"


DEEP_LINK_GENERATORS: dict[tuple[str, str], callable] = {
    ("email", "m365-mcp"): outlook_draft,
    ("email", "zoho-mcp"): zoho_draft,
    ("crm", "hubspot-mcp"): hubspot_contact,   # new
}
```

The approval card uses this to render an "Open in <App>" button. If a `(capability, provider)` pair has no generator, the approval card falls back to showing the draft content inline and relying on the agent to describe next steps.

## 6. Create `docs/providers/<provider>.md`

Structure (match [docs/providers/m365.md](providers/m365.md)):

```markdown
# <Product> Provider — <Short summary>

## Overview
<1 paragraph. What this provider gives the agent. What it deliberately doesn't.>

## App Registration / OAuth Setup
<Numbered steps to create the app and collect client_id, tenant_id, secret.>

## Authentication
<Device code / OAuth flow, token storage path.>

## MCP Server
Location: `mcps/<name>/`

### Tools exposed
| Tool | Description |
|---|---|
| ... | ... |

### Environment variables
| Variable | Description |
|---|---|
| ... | ... |

## Approval Flow
<How drafts + approvals work for this provider.>

## Capability Config
```yaml
<agent_name>:
  <capability>:
    - instance: <owner_instance>
      provider: <provider-id>
      account: ...
      ownership: ...
      permissions: [...]
```

## Provider Config
```yaml
<provider-id>:
  command: ...
  args: [...]
  capabilities: [...]
  permission_scopes: {...}
  env_template: {...}
  secrets_map: {...}
  oauth: {...}
```
```

## 7. Use it in an agent's `capabilities.yaml`

Pick an agent, add a capability instance:

```yaml
agents:
  alex:
    agent: alex
    capabilities:
      crm:
        - instance: pathtohired
          provider: hubspot-mcp   # new provider
          account: pathtohired
          ownership: shared
          permissions: [read, create, update]
```

Verify:

```bash
python -m capabilities render alex
python -m capabilities mcp_config alex
```

The generated `.mcp.json` should include a `crm_pathtohired` entry (command transport) or omit it (connector transport — claude.ai auto-inherits).

## 8. Integration test

Every new provider gets at least one integration test.

### Command-transport providers

Follow the pattern of `tests/unit/mcps/test_server.py`. Use a mock HTTP client to stand in for the service's API.

Required cases:

- **Happy path** for each tool that corresponds to a permission.
- **Scope enforcement** — when a permission is missing, the tool is not registered.
- **Token refresh** — if OAuth is used, assert `refresh_tokens` picks up an expiring token.
- **Error propagation** — HTTP 4xx/5xx surface to the agent as structured MCP errors, not tracebacks.

```python
# tests/unit/mcps/test_hubspot_server.py
import pytest
pytestmark = pytest.mark.unit

hubspot = pytest.importorskip("mcps.hubspot")


def test_create_contact_requires_write_scope(mock_client):
    server = hubspot.build_server(scopes={"crm.objects.contacts.read"})
    assert "create_contact" not in {t.name for t in server.tools}
```

### Connector-transport providers

Only two things to test:

- **Registry load** — the provider ID is loadable from `config/providers.yaml` without raising.
- **Render** — it shows up in the capabilities summary when an agent uses it (covered by the agent-level test in `test_prompt_renderer.py`).

No MCP server, so no tool-level tests.

### End-to-end

Add an e2e test under `tests/integration/` that:

1. Loads a fixture `providers.yaml` + `capabilities.yaml` using the new provider.
2. Runs `generate_mcp_config(agent_name)`.
3. Asserts the `.mcp.json` shape.
4. Asserts `render_capability_summary(agent_name)` contains the new provider.

### Run before opening a PR

```bash
.venv/bin/pytest tests/unit/mcps tests/unit/capabilities tests/unit/approvals -v
.venv/bin/pytest tests/integration -m integration -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ConfigError: unknown provider '<name>'` | Typo in `capabilities.yaml` or missing entry in `providers.yaml` | Check both files for the exact ID |
| `.mcp.json` has empty `M365_SCOPES` | No granted permissions map to any scope | Verify `permission_scopes` covers every permission in the capability |
| Agent session starts but MCP server crashes | Env var missing | Check `env_template` — missing `${VAR}` resolves to empty |
| Token keeps expiring mid-session | `oauth` block missing or refresh buffer too small | Add/fix `oauth:` in provider config; check `REFRESH_BUFFER_SECONDS` |
| Agent says "I don't have send access" but tool exists | Connector transport — all tools are exposed; the agent is self-enforcing. This is expected. | Verify the capabilities summary in the system prompt lists only granted permissions |
