# config/ — Portable Team Configuration

Everything that defines **who the agents are** and **what they remember** lives here. Moving the team to a new machine means copying this one folder.

## Layout

```
config/
├── README.md              # This file
├── agent_tools.json       # Maps agent names → system doc filenames
├── agents/
│   └── <name>/
│       └── role.md        # Job description and responsibilities
└── memory/
    ├── MEMORY.md          # Curated org-wide context (max 2 KB)
    ├── shared/
    │   └── SOUL.md        # Universal behavior rules for all agents
    └── <name>/
        └── personality.md # Per-agent voice and tone
```

### What lives outside `config/`

| Path | Why it's separate |
|---|---|
| `agents/<name>/Dockerfile` | Docker build artefact, not identity |
| `agents/<name>/memory.md` | Runtime data written by agents (gitignored) |
| `systems/` | System-specific API docs, not agent identity |

## Migrating to a new machine

1. Clone the repository
2. Copy `config/` from the old machine (or keep the repo's checked-in copy)
3. Copy `.env.example` to `.env` and fill in your credentials
4. `docker compose up --build`

That's it. Agent identities, roles, and organisational memory carry over automatically.
