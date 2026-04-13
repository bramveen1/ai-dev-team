# Memory System Conventions

## Folder Structure

```
memory/
├── MEMORY.md              # Curated index — read on every session start (max 2KB)
├── daily/                 # Daily logs: YYYY-MM-DD.md
├── projects/              # One file per project (e.g., path-to-hired.md)
├── decisions/             # Institutional memory — never deleted
├── preferences/           # How Bram likes things done
├── people/                # People context: who they are, relationship notes
└── lessons/               # Mistakes and learnings

agents/
└── lisa/
    ├── role.md            # System prompt + persona
    └── memory.md          # Lisa's accumulated knowledge
```

### What Goes Where

| Folder | Purpose | Example |
|---|---|---|
| `daily/` | Day-by-day log of what happened | `2026-04-13.md` — Shipped memory system, discussed agent architecture |
| `projects/` | Per-project context and status | `path-to-hired.md` — Tech stack, current sprint, blockers |
| `decisions/` | Architectural and process decisions | Why we chose Slack over Discord, why Docker over bare metal |
| `preferences/` | Bram's working style and preferences | Prefers short PRs, likes tests first, uses ruff not black |
| `people/` | Contact and relationship context | Key collaborators, their roles, communication preferences |
| `lessons/` | Mistakes made and learnings extracted | "Don't deploy on Fridays" — learned after incident X |

## Format Rules

### Entry Headers

Use this format for all entries:

```markdown
## [YYYY-MM-DD] — [Topic]
```

### Entry Length

- 2-3 sentences max per entry
- Include **why** it matters, not just what happened
- Link to related issues/PRs where relevant

### Example Entry

```markdown
## 2026-04-13 — Chose Slack over Discord for agent orchestration

Slack has better bot framework support (slack_bolt) and Bram's team already uses it daily.
Discord's bot API is more gaming-focused and would require more custom work.
```

## MEMORY.md — The Curated Index

- **Must stay under 2KB** — this is a hard limit
- Read by every agent at the start of every session
- Contains only the most important, current context
- Not a dump of everything — curated summaries only
- Sections: Active Projects, Key People, Current Priorities, Recent Decisions

## Retention Policy

| Folder | Retention | Action |
|---|---|---|
| `daily/` | 30 days | Archive or distill into project/decision files, then delete |
| `projects/` | While active | Archive when project completes |
| `decisions/` | **Forever** | Never delete — this is institutional memory |
| `preferences/` | Indefinite | Update in place as preferences change |
| `people/` | Indefinite | Update in place |
| `lessons/` | **Forever** | Never delete — learn from mistakes |

## What Agents Should Auto-Save

Agents should detect and persist the following from conversations:

1. **Decisions** — Any choice between alternatives, with reasoning
2. **Preferences** — How Bram wants things done (style, tools, process)
3. **People** — New contacts, roles, relationship notes
4. **Project updates** — Status changes, milestones, blockers
5. **Frustrations** — Things that annoyed Bram (so we can avoid/fix them)
6. **Lessons** — Mistakes, things that went wrong, post-mortems

## Memory Curation Schedule

### Weekly: Prune MEMORY.md

- Remove stale entries (completed tasks, resolved decisions)
- Ensure it stays under 2KB
- Move detailed context to the appropriate subfolder

### Monthly: Archive Daily Logs

- Review daily/ files older than 30 days
- Extract key decisions, lessons, and project updates into their permanent folders
- Delete the daily files after extraction

## Agent-Specific Memory

Each agent has a private `memory.md` in their `agents/<name>/` directory:

- **Not shared** with other agents (mounted per-container)
- Stores agent-specific context (e.g., Lisa's knowledge of Bram's calendar preferences)
- Same format rules apply (headers, short entries, include why)
- No size limit, but keep it practical — prune when it gets unwieldy
