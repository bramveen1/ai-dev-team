# Memory Architecture: WORLDVIEW / Personality / Role

## The 3-Layer Model

Agent behavior is composed from three layers, each in a separate file. This avoids duplicating universal rules across agents while still allowing each agent to have a distinct voice.

| Layer | File | Scope | Changes affect |
|---|---|---|---|
| **WORLDVIEW** | `config/shared/WORLDVIEW.md` | All agents | Every agent simultaneously |
| **Personality** | `config/agents/{agent}/personality.md` | One agent | Only that agent |
| **Role** | `config/agents/{agent}/role.md` | One agent | Only that agent |

### Layer 1: WORLDVIEW (universal behavior)

Shared rules that every agent follows regardless of their specialization:

- Core truths (be helpful, have opinions, be resourceful)
- Boundaries (privacy, external action caution)
- Progress reporting style
- Anti-AI-slop rules (banned words, banned patterns)
- Continuity (how to use memory files)

**Location:** `config/shared/WORLDVIEW.md`

**Rule:** Never put agent-specific content here. If a rule only applies to one agent, it belongs in their role or personality file.

### Layer 2: Personality (agent voice)

How the agent sounds. Tone, communication style, quirks. This is the shortest file — typically under 200 words.

**Location:** `config/agents/{agent}/personality.md`

**Examples:**
- Lisa: warm, encouraging, action-oriented, plain language
- Sam: opinionated about architecture, pushes back on bad ideas, values simplicity
- Maya: thinks in narratives and positioning, allergic to corporate voice

**Rule:** Do not repeat anything from WORLDVIEW.md here. Personality files only contain what makes this agent *different* from the baseline.

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
- `memory.md` — the **working set** (how to do things + active projects), loaded every conversation, capped at 10 KB (`WORKING_MEMORY_MAX_BYTES`)
- `daily/YYYY-MM-DD.md` — daily activity logs
- `decisions/YYYY-MM-DD.md` — decisions made in conversations
- `people/{name}.md` — contact and relationship context
- `projects/{name}.md` — per-project status and notes
- `systems/{name}.md` — how recurring infrastructure/processes work
- `preferences/preferences.md` — working style preferences
- `manifest.json` / `INDEX.md` / `search.db` — generated index of the structured files (see below)

All memory files are runtime-generated and gitignored.

### Canonical identity (issue #640)

Person/project/system files are keyed on a **canonical slug**. The writer
resolves LLM-supplied name variants ("Bram Veenhof", "bramveen1",
"bramveenhof@gmail.com") to one canonical file via an explicit, reviewable
alias map at `config/shared/memory-aliases.json` (org-wide — identities are
org-wide; override the path with `MEMORY_ALIAS_MAP_PATH`). Unknown names keep
their own sanitized slug — we never merge on a guess. See
`config.example/shared/memory-aliases.json` for the format.

`router/memory_migrate.py` is the one-time migration that dedups pre-existing
slug-variant files into their canonical file and archives cruft. It is
reversible (move to `_archive/`, never delete) and dry-run by default. It
ships in the `router` image (no separate `scripts/` copy needed), so it runs
directly against the live `/config` volume:

```bash
docker compose exec router python -m router.memory_migrate --agent lisa
# add --apply once the dry-run output looks right
```

**Curation is receipted, not guessed (issue #716).** Every alias entry must
trace to a confirmed identity (a login/id/email seen in code, config, or
logs) — never an assumed variant. `sam`'s entry currently carries only
`aidt-tl-sam`, the GitHub login `pr_review`'s `gh api user` check verifies
against `config/dispatch.yaml` (see `packs/dispatch/pr_review.py`). The
worker/bot slugs `sam--bot`, `sam--sam-ai-bot`, `dev-sam`, and `aidt-sam` (the
PAT-file label, which does not match the verified `aidt-tl-sam` login) look
like Sam variants but are **not** aliased — they may belong to shared
worker/bot activity rather than the Sam persona specifically, so they're
flagged for Bram to confirm before merging. Bare generic slugs such as
`user-<id>` must never be aliased to a person — that would misroute anyone.

### Structured memory index & retrieval (issue #640)

`router/memory_index.py` generates `manifest.json` (machine view: one row per
structured file — canonical key, aliases, one-line summary, mtime, size),
`INDEX.md` (human view), and `search.db` — a SQLite FTS5 full-text database
over the complete file contents (porter stemming, entity names weighted 3×
over body text). SQLite ships in the Python stdlib, so this is an
off-the-shelf BM25 search engine with zero new dependencies or services. All
three are regenerated after every `persist_memory` and every nightly
curation, so staleness self-heals. No vector DB or embeddings in v1.

`router/memory_retriever.py` is the read path: on dispatch, the loader always
loads `memory.md`, then — when `MEMORY_RETRIEVAL_ENABLED=1` — BM25-ranks the
structured files against the new message via FTS5 and injects the top-K as a
`--- RELEVANT LONG-TERM MEMORY ---` context section. When `search.db` is
missing or unreadable it falls back to keyword scoring over the manifest;
when that is missing too, or on any error, it degrades to memory.md-only
(today's behaviour). The whole path is off unless the flag is set.

After curation a smoke probe asserts: (a) `memory.md` is within the cap,
(b) the manifest parses and references only existing files, (c) each known
entity resolves to exactly one canonical file. Problems are logged, never
fatal.

## Loading Order

When an agent starts a session, context files are loaded in this order. Each layer extends the previous:

```
1. config/shared/WORLDVIEW.md                        — universal behavior rules
2. config/agents/{agent}/role.md                — what this agent does
3. config/agents/{agent}/personality.md         — how this agent sounds
4. config/agents/{agent}/memory/memory.md       — what this agent remembers
5. config/shared/MEMORY.md                      — org-wide context index
```

The dispatcher passes these as system prompt files to Claude Code CLI via `--append-system-prompt-file`, preserving this order.

## Directory Structure

```
config/
├── shared/
│   ├── WORLDVIEW.md             # universal behavior rules (all agents)
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
| Add a rule all agents must follow | `config/shared/WORLDVIEW.md` |
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

The new agent automatically inherits all WORLDVIEW rules. Memory directories are created automatically at runtime. No duplication needed.
