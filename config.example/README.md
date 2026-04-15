# config/ — Portable Team Configuration

Everything that defines **who the agents are** and **what they remember** lives here. Moving the team to a new machine means copying this one folder.

## Setup

```bash
cp -r config.example config
# Then customise the files for your team
```

## Layout

```
config/
├── agent_tools.json       # Maps agent names to system doc filenames
├── shared/
│   ├── WORLDVIEW.md            # Universal behavior rules for all agents
│   └── MEMORY.md          # Curated org-wide context (max 2 KB)
└── agents/
    └── <name>/
        ├── role.md        # Job description and responsibilities
        ├── personality.md # Per-agent voice and tone
        └── memory/        # Per-agent runtime memory (auto-created)
```

## Why config/ is gitignored

Config files may contain private information (credentials in tool configs, personal context in memory, internal role descriptions). The `config.example/` directory provides templates. Copy it to `config/` and customise.
