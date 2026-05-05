# Capabilities → Packs simplification

This document is the audit and the target architecture for replacing the existing capability framework with a one-layer **pack** model. It is the deliverable of [issue #81](https://github.com/bramveen1/ai-dev-team/issues/81) and the entry point for the eight-PR migration tracked under that epic.

## Why this is happening

The repo ships a five-layer capability framework: `capability_type → instance → provider → permission → MCP server`. It lives across `capabilities/`, `config/providers.yaml`, `config/baseline.yaml`, and per-agent `agent.yaml` files, with 1,500+ lines of documentation.

The framework was meant to: generate a per-agent `.mcp.json`, render a "Your Capabilities" block into the system prompt, refresh OAuth tokens, and enforce permissions at the API scope level.

**None of that is wired into the dispatch path.** The router builds the `claude` invocation in [router/dispatcher.py](../router/dispatcher.py) with five `--append-system-prompt-file` flags (worldview, role, personality, agent memory, org memory) and **no `--mcp-config`, no rendered capabilities summary, no token injection**. The only callers of `capabilities/mcp_namespacer.py`, `prompt_renderer.py`, `oauth.py`, and `secrets.py` are:

1. The manual CLI: `python -m capabilities …`.
2. A validation step in [scripts/add_agent.py:443](../scripts/add_agent.py#L443) that imports `capabilities.loader` to confirm a freshly-written `agent.yaml` parses.
3. A second, parallel loader at [router/approvals/capabilities_loader.py](../router/approvals/capabilities_loader.py) (177 lines, dataclasses to avoid Pydantic in the router container) used to decide whether an approval card shows a "Send" button vs an "Open in Outlook" deep-link.

So the entire framework supports **one Block-Kit-button decision**.

The user-visible cost is that giving an agent a new capability — e.g. "let Sam use GitHub" — requires editing five unrelated systems (Dockerfile, docker-compose, .env, capabilities YAML, role.md) and even then nothing actually gets injected at runtime, because the namespacer never runs.

## Footprint to remove

| Area | Lines | Status |
|---|---|---|
| [capabilities/](../capabilities/) (`models`, `loader`, `mcp_namespacer`, `prompt_renderer`, `oauth`, `secrets`, `__main__`, `__init__`) | ~1,080 | Mostly dead at runtime |
| [router/approvals/capabilities_loader.py](../router/approvals/capabilities_loader.py) | 177 | Live (one button decision) |
| [config/providers.yaml](../config/providers.yaml) + [config/baseline.yaml](../config/baseline.yaml) | 224 | Inert config |
| [docs/capability-framework.md](capability-framework.md) + 3 runbooks | ~1,500 | Documents inert system |
| [tests/unit/capabilities/](../tests/unit/capabilities/) | ~1,660 | Tests for dead code |
| **Total** | **~4,700 lines** | |

There is also an unrelated third tool registry — [config/agent_tools.json](../config/agent_tools.json) — that maps each agent to a list of `/systems/*.md` filenames loaded into the prompt as "tool docs." Sam is listed there with `["github.md", "posthog.md", "pathtohired-api.md"]` — none of which actually exist in `systems/`. Three systems, no integration.

## Target architecture: connectors first, packs only when needed

Claude Code natively inherits **connectors** from the claude.ai account — Microsoft 365, Gmail, Google Calendar, Google Drive, Notion, Linear, and others. For any service with a connector, agents already have access. No token wiring, no MCP server, no pack.

**The pack system exists only for services without a Claude connector.** Examples: GitHub via the `gh` CLI, Zoho Mail, internal HTTP APIs, anything that requires a CLI in the container or a token managed outside claude.ai.

### When to add a pack vs. write a role.md note

| Has a Claude connector? | Mechanism |
|---|---|
| Yes (M365, Gmail, GCal, GDrive, Notion, Linear, …) | Describe usage and approval rules in `role.md`. Connector is auto-available. |
| No (`gh`, Zoho Mail, internal APIs, PostHog if needed, …) | Author a pack under `packs/<name>/`. |

This is the largest single simplification: Lisa's M365 access — the most complex case in the old design (two accounts, OAuth refresh, custom MCP server) — becomes a `role.md` paragraph. No pack, no `oauth.py`, no `mcps/m365_mail/`, no `config/secrets/m365.json`.

### Pack shape

```
packs/github/
  pack.yaml         # name, description, secrets needed, approval rules
  prompt.md         # what the agent reads to know it has this tool
  mcp.json          # optional: MCP server config to merge in
  authenticate.py   # optional: walks the user through getting the token
```

Example `pack.yaml`:

```yaml
name: github
description: GitHub access via the gh CLI
needs: [GITHUB_TOKEN]
cli: gh
approve: [merge]   # 'merge' requires human approval; reads/comments don't
```

That's the whole framework. No `capability_type / instance / provider / permission / ownership` abstractions. The pack *is* the capability; the agent either has it or doesn't.

`agent.yaml` shrinks to:

```yaml
name: Sam
container: sam
thinking_status: "is digging in…"
packs: [github, posthog]
```

### Granting from Slack

Non-tech users grant capabilities in Slack:

```
You (in #ops): @router grant sam github
Router (DM):   Sam doesn't have GitHub access yet. Click to authorize:
               https://github.com/login/device  Code: WDJB-MJHT
You: (clicks, approves)
Router: Done. Sam now has GitHub. Reads, comments, and creates issues OK; merges still ask first.
```

Behind the scenes the router runs the pack's `authenticate.py`, stores the secret in `data/secrets.json`, edits the target `agent.yaml` to append the pack to `packs:`, and (optionally) restarts the container. Companion commands: `revoke`, `list packs`, `who has <pack>`.

### CLIs come from a shared volume, not a Dockerfile rebuild

A shared Docker volume `agent-tools:/opt/tools` mounts into every agent container. When a pack with `cli: <name>` is granted, a one-shot installer drops the binary into the volume. Every container has `/opt/tools` on `PATH`. New CLIs become available without rebuilding any image.

### Approvals

Two clean cases, no lookup table:

- **Connector-backed services** (M365, Gmail, …): no pack. The button resolver maps the draft's `target` field to a deep-link URL via the existing `_APP_NAMES` dict. Whether to draft vs send is decided by the agent based on `role.md` rules.
- **Pack-backed services** (GitHub, Zoho, …): the pack's `pack.yaml` declares `approve: [...]`. The button resolver reads it. Approvals are pack-level, never per-agent — when Lisa needs different rules for the same service across two accounts, model it as two separate packs.

## File-by-file impact

### Delete (PR 7)

| Path | Reason |
|---|---|
| [capabilities/models.py](../capabilities/models.py) | Pydantic schema for the dead framework |
| [capabilities/loader.py](../capabilities/loader.py) | Loads/validates the dead framework |
| [capabilities/mcp_namespacer.py](../capabilities/mcp_namespacer.py) | Generates `.mcp.json` — never read at runtime |
| [capabilities/prompt_renderer.py](../capabilities/prompt_renderer.py) | Renders capabilities summary — never injected |
| [capabilities/oauth.py](../capabilities/oauth.py) | M365 OAuth — superseded by M365 connector. Generic device-code helper preserved separately in PR 2. |
| [capabilities/secrets.py](../capabilities/secrets.py) | Replaced by `data/secrets.json` |
| [capabilities/__main__.py](../capabilities/__main__.py) | CLI for the dead framework |
| [capabilities/__init__.py](../capabilities/__init__.py) | Package marker |
| [router/approvals/capabilities_loader.py](../router/approvals/capabilities_loader.py) | Replaced by pack-level `approve:` lookup |
| [config/providers.yaml](../config/providers.yaml) | Provider registry for the dead framework |
| [config/baseline.yaml](../config/baseline.yaml) | Auto-merged baseline capabilities — irrelevant in pack model |
| [config/secrets/](../config/secrets/) | Replaced by `data/secrets.json` |
| [config/agent_tools.json](../config/agent_tools.json) | Folded into agent.yaml `packs:` |
| [mcps/m365_mail/](../mcps/m365_mail/) | Custom M365 MCP — superseded by the M365 connector |
| [tests/unit/capabilities/](../tests/unit/capabilities/) | Tests for deleted code |
| [tests/unit/mcps/](../tests/unit/mcps/) | M365 tests for deleted code |
| [docs/capability-framework.md](capability-framework.md) | Documents the dead framework |
| [docs/add-a-new-capability.md](add-a-new-capability.md) | Runbook for the dead framework |
| [docs/add-a-new-provider.md](add-a-new-provider.md) | Runbook for the dead framework |
| [docs/swap-a-provider.md](swap-a-provider.md) | Runbook for the dead framework |

### Add (PR 2 / 3 / 4 / 5 / 8)

| Path | Purpose |
|---|---|
| `router/packs/__init__.py` | Pack runtime package |
| `router/packs/loader.py` | Discover and parse `packs/<name>/pack.yaml` |
| `router/packs/secret_store.py` | Read/write `data/secrets.json` |
| `router/packs/oauth_devicecode.py` | Generic OAuth2 device-code helper (extracted from `capabilities/oauth.py`) |
| `router/packs/grant.py` | Grant/revoke ops |
| `router/packs/slack_handlers.py` | `@router grant/revoke/list/who-has` handlers |
| `packs/_template/` | Reference pack scaffold |
| `packs/github/` | First real pack (PR 4) |
| `packs/zoho-mail/` | Lisa's second mailbox (PR 5) |
| `scripts/install_cli.sh` | Install a CLI into the shared `agent-tools` volume |
| `docs/authoring-a-pack.md` | For tech users (PR 8) |
| `docs/managing-agents-from-slack.md` | For everyone (PR 8) |

### Change

| Path | Change |
|---|---|
| [router/dispatcher.py](../router/dispatcher.py) | If `agent.yaml` declares `packs:`, append each pack's `prompt.md` to the system prompt and pass `--mcp-config` with merged `mcp.json`s. Backwards-compatible. |
| [scripts/render_compose.py](../scripts/render_compose.py) | Mount `agent-tools:/opt/tools` on every agent service. Prepend `/opt/tools` to PATH. |
| [router/approvals/button_resolver.py](../router/approvals/button_resolver.py) | Read `approve:` from the pack instead of looking up a `CapabilityInstance` |
| [router/approvals/interceptor.py](../router/approvals/interceptor.py) | Draft block schema gains a `pack:` field |
| [scripts/add_agent.py](../scripts/add_agent.py) | Drop capability prompts; multi-select packs |
| [config/agents/sam/agent.yaml](../config/agents/sam/agent.yaml) | `packs: [github]` |
| [config/agents/lisa/agent.yaml](../config/agents/lisa/agent.yaml) | `packs: [zoho-mail]`; remove `capabilities:` |
| [config/agents/lisa/role.md](../config/agents/lisa/role.md) | Add explicit M365 rules (always draft for Bram's inbox; send freely from her own) |
| Other agents under `config/agents/` | Migrate any `agent_tools.json` entries to `packs:` |

## Migration sequence

The eight PRs are tracked as GitHub issues #81 → #88. Each PR keeps the system running.

| # | PR | Summary |
|---|---|---|
| [#81](https://github.com/bramveen1/ai-dev-team/issues/81) | 1 | Audit + this doc (no code changes) |
| [#82](https://github.com/bramveen1/ai-dev-team/issues/82) | 2 | Pack runtime infra (additive, dormant) |
| [#83](https://github.com/bramveen1/ai-dev-team/issues/83) | 3 | Slack grant flow |
| [#84](https://github.com/bramveen1/ai-dev-team/issues/84) | 4 | First pack: `packs/github/` + migrate Sam |
| [#85](https://github.com/bramveen1/ai-dev-team/issues/85) | 5 | Migrate Lisa: Zoho pack + M365 connector |
| [#86](https://github.com/bramveen1/ai-dev-team/issues/86) | 6 | Pack-level approvals; drop `capabilities_loader` |
| [#87](https://github.com/bramveen1/ai-dev-team/issues/87) | 7 | Delete the old framework |
| [#88](https://github.com/bramveen1/ai-dev-team/issues/88) | 8 | Add-agent wizard rewrite + new docs |

## What is lost (and why it's OK)

- **API-level scope enforcement.** Already lost — the namespacer never ran. Trust the prompt + approval flow, which is what's actually been gating actions.
- **Pluggable providers swappable per instance.** One email user has two accounts. The M365 connector + a Zoho pack covers it. YAGNI for the rest.
- **The framework's audit story.** Replaced by: `agent.yaml` lists packs; `packs/<name>/pack.yaml` documents each. Both diff-able in PRs.
