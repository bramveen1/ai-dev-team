# Memory Architecture: SOUL / Personality / Role

## The 3-Layer Model

Agent behavior is composed from three layers, each in a separate file. This avoids duplicating universal rules across agents while still allowing each agent to have a distinct voice.

| Layer | File | Scope | Changes affect |
|---|---|---|---|
| **SOUL** | `memory/shared/SOUL.md` | All agents | Every agent simultaneously |
| **Personality** | `memory/{agent}/personality.md` | One agent | Only that agent |
| **Role** | `agents/{agent}/role.md` | One agent | Only that agent |

### Layer 1: SOUL (universal behavior)

Shared rules that every agent follows regardless of their specialization:

- Core truths (be helpful, have opinions, be resourceful)
- Boundaries (privacy, external action caution)
- Progress reporting style
- Anti-AI-slop rules (banned words, banned patterns)
- Continuity (how to use memory files)

**Location:** `memory/shared/SOUL.md`

**Rule:** Never put agent-specific content here. If a rule only applies to one agent, it belongs in their role or personality file.

### Layer 2: Personality (agent voice)

How the agent sounds. Tone, communication style, quirks. This is the shortest file — typically under 200 words.

**Location:** `memory/{agent}/personality.md`

**Examples:**
- Lisa: warm, encouraging, action-oriented, plain language
- Sam: opinionated about architecture, pushes back on bad ideas, values simplicity
- Maya: thinks in narratives and positioning, allergic to corporate voice

**Rule:** Do not repeat anything from SOUL.md here. Personality files only contain what makes this agent *different* from the baseline.

### Layer 3: Role (job description)

What the agent does. Responsibilities, domain knowledge, constraints specific to their work.

**Location:** `agents/{agent}/role.md`

**Examples:**
- Lisa: project management, task breakdown, progress tracking
- Sam: architecture decisions, code review, technical specifications
- Maya: marketing copy, positioning, brand voice

**Rule:** This is a job description, not a personality profile. "Review PRs with constructive feedback" is a role item. "Be warm and encouraging" is a personality item.

## Loading Order

When an agent starts a session, context files are loaded in this order. Each layer extends the previous:

```
1. memory/shared/SOUL.md          — universal behavior rules
2. agents/{agent}/role.md          — what this agent does
3. memory/{agent}/personality.md   — how this agent sounds
4. agents/{agent}/memory.md        — what this agent remembers
5. memory/MEMORY.md                — org-wide context index
```

The dispatcher passes these as system prompt files to Claude Code CLI via `--append-system-prompt-file`, preserving this order.

## Directory Structure

```
memory/
├── MEMORY.md                    # org-wide context index
├── shared/
│   └── SOUL.md                  # universal behavior rules (all agents)
├── lisa/
│   └── personality.md           # Lisa-specific voice
├── alex/
│   └── personality.md           # stub (pending)
├── sam/
│   └── personality.md           # stub (pending)
├── dave/
│   └── personality.md           # stub (pending)
├── maya/
│   └── personality.md           # stub (pending)
├── lin/
│   └── personality.md           # stub (pending)
├── daily/                       # daily logs
├── decisions/                   # architectural decisions
├── lessons/                     # mistakes and learnings
├── people/                      # contact/relationship context
├── preferences/                 # working style preferences
└── projects/                    # per-project status

agents/
├── lisa/
│   ├── role.md                  # job description
│   ├── memory.md                # agent-specific memory
│   └── Dockerfile
└── ...
```

## When to Put What Where

| I want to... | Put it in... |
|---|---|
| Add a rule all agents must follow | `memory/shared/SOUL.md` |
| Change how Lisa talks | `memory/lisa/personality.md` |
| Add a new responsibility for Lisa | `agents/lisa/role.md` |
| Record something Lisa learned | `agents/lisa/memory.md` |
| Add org-wide context (projects, people) | `memory/MEMORY.md` |
| Add a new agent | Create `agents/{name}/role.md`, `memory/{name}/personality.md`, `agents/{name}/memory.md` |

## Adding a New Agent

1. Create `agents/{name}/role.md` with job description and responsibilities
2. Create `memory/{name}/personality.md` with tone and voice (under 200 words)
3. Create `agents/{name}/memory.md` (empty to start)
4. Add the agent to `router/config.py` AGENT_MAP
5. Create `agents/{name}/Dockerfile`
6. Add the service to `docker-compose.yml`

The new agent automatically inherits all SOUL rules. No duplication needed.
