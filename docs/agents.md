# Current Agent Roster

One section per agent. Update this file every time an agent is added, renamed, or has a capability / scheduled-task change. The source of truth for behaviour is each agent's `role.md` + `personality.md` + `capabilities.yaml`; this file is a discoverable directory of what exists.

## How to keep this doc current

- **Adding an agent?** Follow [add-a-new-agent.md](add-a-new-agent.md) and add a new section below.
- **New capability or instance?** Update the capabilities table for that agent.
- **Swapped a provider?** Change the provider column in the capabilities table.
- **New seed scheduled task?** Update the scheduled-tasks table.

If a row here doesn't match what's in `config/capabilities.yaml`, this file is wrong — fix it.

---

## Lisa — Project Manager & Executive Assistant

**Container:** `lisa`
**Slack handle:** `@Lisa`
**Role file:** [`config.example/agents/lisa/role.md`](../config.example/agents/lisa/role.md)
**Personality:** Warm, encouraging, action-oriented. Plain language. Short sentences.

### Responsibilities

- Manage Bram's inbox (delegate access): triage incoming mail, draft replies for approval.
- Manage her own inbox (lisa@pathtohired.com): send on behalf of the team for recruiting, scheduling, and vendor correspondence.
- Propose calendar events on Bram's calendar; booking requires approval.
- Break down incoming Slack requests into actionable work and coordinate with other agents as they come online.

### Capabilities

| Capability | Instance | Provider | Ownership | Permissions |
|---|---|---|---|---|
| email | mine | zoho-mcp | self | read, send, archive, draft-create, draft-update, draft-delete |
| email | bram | m365-connector | delegate | read, draft-create, draft-update, draft-delete |
| calendar | bram | m365-connector | delegate | read, propose |
| web | browser | playwright-mcp | shared | browse, screenshot, interact *(baseline)* |
| memory | agent | memory-mcp | self | read, write *(baseline)* |
| memory | shared | memory-mcp | shared | read *(baseline)* |
| slack_io | team | slack-mcp | shared | read, send, react *(baseline)* |
| scheduled_tasks | agent | scheduler-mcp | self | create, read, update, delete *(baseline)* |

Baseline rows come from [`config/baseline.yaml`](../config/baseline.yaml) and are shared across every agent. Agent-specific capabilities are the top rows.

### Seed scheduled tasks

| Name | Schedule (UTC) | Enabled by default | Prompt summary |
|---|---|---|---|
| Daily inbox review | `0 9 * * 1-5` (weekdays 09:00) | No | Summarises yesterday's inbox activity for Bram and DMs it. |

Seeds live in [`router/scheduled_tasks/seeds.py`](../router/scheduled_tasks/seeds.py). Tasks are inserted idempotently at router startup and must be enabled via `/tasks resume <id>`.

### Approval rules

- **`email_bram`** has no `send` — Lisa always creates a draft and posts an approval card. The card links to Outlook via the M365 connector so Bram can review and send manually.
- **`email_mine`** can send directly — no approval required for standard sends from Lisa's own account.
- **`calendar_bram`** can propose but not book — booking requires approval.

---

## (Placeholder) Alex — Growth / Analytics

*Not yet created. Below is the target shape — remove this placeholder when Alex ships.*

**Container:** `alex` (planned)
**Responsibilities:** Product analytics, growth experiments, funnel review, retention dashboards.

### Planned capabilities

| Capability | Instance | Provider | Ownership | Permissions |
|---|---|---|---|---|
| analytics | posthog | posthog-mcp | shared | read, annotate |
| docs | notion | notion-connector | shared | read, write, comment |

### Planned seed scheduled tasks

| Name | Schedule (UTC) | Prompt summary |
|---|---|---|
| Weekly growth digest | `0 14 * * 1` (Mon 14:00) | Summarise last week's signup funnel and post to #growth. |

---

## (Placeholder) Sam — Engineering

*Not yet created.*

**Container:** `sam` (planned)
**Responsibilities:** Code review, issue triage, PR feedback, dependency updates.

### Planned capabilities

| Capability | Instance | Provider | Ownership | Permissions |
|---|---|---|---|---|
| code-repo | pathtohired | github-mcp | shared | read, comment, issue-create, pr-create, branch-create |

### Planned seed scheduled tasks

| Name | Schedule (UTC) | Prompt summary |
|---|---|---|
| Dependency digest | `0 13 * * 2` (Tue 13:00) | List stale dependencies across repos with upgrade recommendations. |

---

## (Placeholder) Dave — Dev Lead

*Not yet created.* Container `dave` (planned). Responsibilities: architecture reviews, onboarding new agents into the team, sign-off on infrastructure changes. Capabilities TBD.

## (Placeholder) Maya — Design

*Not yet created.* Container `maya` (planned). Responsibilities: design reviews, asset export, Figma-to-code handoff. Planned capabilities: `design` via `figma-mcp`, `docs` via `notion-connector`.

## (Placeholder) Lin — Marketing

*Not yet created.* Container `lin` (planned). Responsibilities: campaign drafting, social posting, newsletter authorship. Planned capabilities: `marketing` via a marketing provider, `social` via platform-specific MCPs (draft-only by default).

---

## Related docs

- [Capability framework](capability-framework.md) — the 3-layer model this roster is built on.
- [Add a new agent](add-a-new-agent.md) — end-to-end runbook.
- [Add a new capability](add-a-new-capability.md) — when and how to introduce a new capability type.
- [Add a new provider](add-a-new-provider.md) — wire up an MCP server or connector.
- [Swap a provider](swap-a-provider.md) — change which provider backs an existing instance.
- [Scheduled tasks](scheduled-tasks.md) — how recurring task execution works.
