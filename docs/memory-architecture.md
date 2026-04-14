# Memory Architecture: SOUL / Personality / Role

## The 3-Layer Model

Agent behavior is composed from three layers, each in a separate file. This avoids duplicating universal rules across agents while still allowing each agent to have a distinct voice.

| Layer | File | Scope | Changes affect |
|---|---|---|---|
| **SOUL** | `config/shared/SOUL.md` | All agents | Every agent simultaneously |
| **Personality** | `config/agents/{agent}/personality.md` | One agent | Only that agent |
| **Role** | `config/agents/{agent}/role.md` | One agent | Only that agent |

### Layer 1: SOUL (universal behavior)

Shared rules that every agent follows regardless of their specialization:

- Core truths (be helpful, have opinions, be resourceful)
- Boundaries (privacy, external action caution)
- Progress reporting style
- Anti-AI-slop rules (banned words, banned patterns)
- Continuity (how to use memory files)

**Location:** `config/shared/SOUL.md`

**Rule:** Never put agent-specific content here. If a rule only applies to one agent, it belongs in their role or personality file.

### Layer 2: Personality (agent voice)

How the agent sounds. Tone, communication style, quirks. This is the shortest file — typically under 200 words.

**Location:** `config/agents/{agent}/personality.md`

**Examples:**
- Lisa: warm, encouraging, action-oriented, plain language
- Sam: opinionated about architecture, pushes back on bad ideas, values simplicity
- Maya: thinks in narratives and positioning, allergic to corporate voice

**Rule:** Do not repeat anything from SOUL.md here. Personality files only contain what makes this agent *different* from the baseline.

### Layer 3: Role (job description)

What the agent does. Responsibilities, domain knowledge, constraints specific to their work.

**Location:** `config/agents/{agent}/role.md`

**Examples:**
- Lisa: project management, task breakdown, progress tracking
- Sam: architecture decisions, code review, technical specifications
- Maya: marketing copy, positioning, brand voice

**Rule:** This is a job description, not a personality profile. "Review PRs with constructive feedback" is a role item. "Be warm and encouraging" is a personality item.

## Per-Agent Memory

Each agent maintains its own memory, separate from other agents. This means Lisa's knowledge of people and projects is independent from what Sam or Dave learn.

**Location:** `config/agents/{agent}/memory/`

Memory categories:
- `memory.md` — agent's accumulated knowledge
- `daily/YYYY-MM-DD.md` — daily activity logs
- `decisions/YYYY-MM-DD.md` — decisions made in conversations
- `people/{name}.md` — contact and relationship context
- `projects/{name}.md` — per-project status and notes
- `preferences/preferences.md` — working style preferences

All memory files are runtime-generated and gitignored.

## Loading Order

When an agent starts a session, context files are loaded in this order. Each layer extends the previous:

```
1. config/shared/SOUL.md                        — universal behavior rules
2. config/agents/{agent}/role.md                — what this agent does
3. config/agents/{agent}/personality.md         — how this agent sounds
4. config/agents/{agent}/memory/memory.md       — what this agent remembers
5. config/shared/MEMORY.md                      — org-wide context index
```

The dispatcher passes these as system prompt files to Claude Code CLI via `--append-system-prompt-file`, preserving this order.

## Directory Structure

```
config/
├── agent_tools.json             # agent-to-tool mapping
├── shared/
│   ├── SOUL.md                  # universal behavior rules (all agents)
│   └── MEMORY.md                # curated org-wide context (max 2 KB)
└── agents/
    ├── lisa/
    │   ├── role.md              # job description
    │   ├── personality.md       # Lisa-specific voice
    │   └── memory/              # runtime memory (gitignored)
    │       ├── memory.md
    │       ├── daily/
    │       ├── decisions/
    │       ├── people/
    │       ├── projects/
    │       └── preferences/
    ├── alex/
    │   ├── personality.md       # stub (pending)
    │   └── memory/
    ├── sam/
    │   ├── personality.md
    │   └── memory/
    └── ...

agents/
├── lisa/
│   └── Dockerfile
└── ...
```

## When to Put What Where

| I want to... | Put it in... |
|---|---|
| Add a rule all agents must follow | `config/shared/SOUL.md` |
| Change how Lisa talks | `config/agents/lisa/personality.md` |
| Add a new responsibility for Lisa | `config/agents/lisa/role.md` |
| Record something Lisa learned | `config/agents/lisa/memory/memory.md` |
| Add org-wide context (projects, people) | `config/shared/MEMORY.md` |
| Add a new agent | Create role.md, personality.md under `config/agents/{name}/` |

## Adding a New Agent

1. Create `config/agents/{name}/role.md` with job description and responsibilities
2. Create `config/agents/{name}/personality.md` with tone and voice (under 200 words)
3. Add the agent to `router/config.py` AGENT_MAP
4. Create `agents/{name}/Dockerfile`
5. Add the service to `docker-compose.yml`

The new agent automatically inherits all SOUL rules. Memory directories are created automatically at runtime. No duplication needed.
