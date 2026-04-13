# Epic: Lisa End-to-End — First Agent in Slack with Memory

## GitHub Issues for Phase 1

Copy each section below as a separate GitHub issue. The epic issue references all sub-issues.

---

## EPIC ISSUE

**Title:** `[Epic] Lisa End-to-End — First Agent in Slack with Memory`

**Labels:** `epic`, `phase-1`

**Body:**

```markdown
## Goal
Get Lisa (Admin agent) responding in Slack via Claude Code CLI running in a Docker container, with persistent memory between sessions. This proves the full architecture before scaling to the remaining five agents.

## Architecture
- **Router:** Python/slack_bolt container, receives all Slack webhooks, dispatches to agent containers
- **Agent container:** Lightweight Docker container with Claude Code CLI + MCP configs, spawns CLI sessions on demand
- **Memory:** Shared volume for org memory, per-agent volume for private memory
- **Session lifecycle:** Clean exit ("Thanks") + timeout exit (summary dump to thread + memory persist)

## Sub-issues (in dependency order)

### Foundation
- [ ] #__ Spike: Claude Code CLI invocation for agent use
- [ ] #__ Docker base image for agent containers
- [ ] #__ Set up Slack app for Lisa

### Core Loop
- [ ] #__ Scaffold the router service
- [ ] #__ Port Lisa's role.md and systems/*.md from OpenClaw
- [ ] #__ Lisa agent container + Claude Code CLI integration
- [ ] #__ Thread awareness (multi-turn conversations)

### Memory
- [ ] #__ Memory folder structure and MEMORY.md
- [ ] #__ Memory loading on session start
- [ ] #__ Memory persistence on session end (clean + timeout)
- [ ] #__ Session resume from thread summary

## Definition of Done
- [ ] Message @Lisa in Slack → get a response that sounds like Lisa
- [ ] Multi-turn conversation works in a thread
- [ ] End conversation with "Thanks" → Lisa persists memory
- [ ] Let conversation timeout → summary appears in thread + memory persisted
- [ ] Start new conversation → Lisa references past decisions/context
- [ ] Come back to timed-out thread → Lisa resumes from summary
```

---

## SUB-ISSUE 1

**Title:** `Spike: Claude Code CLI invocation for agent use`

**Labels:** `spike`, `phase-1`, `priority-critical`

**Body:**

```markdown
## Context
Everything depends on understanding how Claude Code CLI behaves when spawned programmatically. We need to answer several questions before building the router or containers.

## Research Questions
1. **System prompt:** How do we pass a custom system prompt? `--system-prompt` flag? File path? Stdin?
2. **MCP servers:** How do we configure MCP servers per invocation? Per-project config file? CLI flags?
3. **Output capture:** Can we get structured output (JSON mode)? Or do we parse stdout?
4. **Session management:** Does the CLI support continuing a session? Or is each invocation stateless?
5. **Authentication:** How does Claude Code CLI authenticate with the Max subscription? Token file? Login flow? Does it work in Docker?
6. **Concurrency:** Can we run multiple CLI instances simultaneously on one machine?
7. **Ubuntu/Docker:** Any known issues running Claude Code CLI in a Docker container on Linux?

## Deliverable
A spike doc (markdown) in the repo with findings, code snippets for each pattern, and any gotchas discovered. This doc becomes the reference for all subsequent implementation.

## Acceptance Criteria
- [ ] Can spawn Claude Code CLI from Python with a custom system prompt
- [ ] Can capture the response programmatically
- [ ] Understand MCP server configuration per-session
- [ ] Tested in Docker container on Ubuntu
- [ ] Spike doc committed to repo
```

---

## SUB-ISSUE 2

**Title:** `Docker base image for agent containers`

**Labels:** `infrastructure`, `phase-1`

**Depends on:** Spike (issue 1)

**Body:**

```markdown
## Context
Each agent runs in its own Docker container. We need a base image that all six agents will extend with their specific MCP configs.

## Requirements
- Claude Code CLI installed and authenticated
- Node.js runtime (for MCP servers that need it)
- Python runtime (for custom MCP servers)
- Volume mount points:
  - `/memory` — shared org memory (read-write)
  - `/agent` — agent-specific files: role.md, memory.md (read-write)
  - `/systems` — systems/*.md tool docs (read-only)
- Memory limit support (Docker --memory flag)
- Lightweight: target < 500MB image size

## Base image structure
```
FROM ubuntu:22.04
# Install Claude Code CLI
# Install Node.js, Python
# Set up volume mount points
# Set up entrypoint that accepts commands from router
```

## Variant: Playwright image
Extends base with Playwright + Chromium for agents that need browser access (Sam, Dave, Maya).

```
FROM agent-base
# Install Playwright + Chromium
# Configure Playwright MCP server
```

## Acceptance Criteria
- [ ] Base image builds successfully
- [ ] Claude Code CLI runs inside container and authenticates
- [ ] Can spawn a CLI session with a system prompt from a mounted volume
- [ ] Playwright variant can launch a browser session
- [ ] Image size < 500MB (base) / < 1GB (playwright variant)
- [ ] Memory limits enforced via Docker --memory
```

---

## SUB-ISSUE 3

**Title:** `Set up Slack app for Lisa`

**Labels:** `infrastructure`, `phase-1`

**Body:**

```markdown
## Context
Lisa needs her own Slack app with a distinct identity — avatar, display name, and bot token. This is one of six apps (one per agent), but we start with Lisa only.

## Tasks
- [ ] Create Slack app at api.slack.com
- [ ] Set display name: "Lisa"
- [ ] Upload avatar (admin/assistant themed)
- [ ] Configure bot token scopes: `chat:write`, `app_mentions:read`, `channels:history`, `groups:history`, `im:history`, `im:read`, `im:write`
- [ ] Enable Event Subscriptions for: `app_mention`, `message.im`
- [ ] Set up webhook URL pointing to Surface Pro (use Cloudflare Tunnel or ngrok for dev)
- [ ] Install app to workspace
- [ ] Store bot token and signing secret securely (env vars or secrets file)

## Acceptance Criteria
- [ ] @Lisa appears in Slack with correct avatar
- [ ] Sending a DM to Lisa generates a webhook event
- [ ] @Lisa mention in a channel generates a webhook event
- [ ] Bot token and signing secret stored securely, not in code
```

---

## SUB-ISSUE 4

**Title:** `Scaffold the router service`

**Labels:** `core`, `phase-1`

**Depends on:** Slack app (issue 3)

**Body:**

```markdown
## Context
The router is the central nervous system. It receives all Slack webhooks and dispatches to the right agent container. For Phase 1, it only handles Lisa. But the structure should support six agents from the start.

## Technical Decisions
- **Language:** Python
- **Framework:** slack_bolt
- **Runs in:** Its own Docker container (or directly on host during dev)
- **Agent dispatch:** docker exec into the target agent container (or internal HTTP API — decide during spike)

## Implementation
```python
# Pseudocode structure
app = SlackApp(token=LISA_TOKEN, signing_secret=LISA_SECRET)

# Map Slack app tokens to agent names
AGENT_MAP = {
    LISA_TOKEN: "lisa",
    # Future: ALEX_TOKEN: "alex", SAM_TOKEN: "sam", etc.
}

@app.event("app_mention")
@app.event("message")
def handle_message(event, say):
    agent = identify_agent(event)  # Which app received this?
    thread_ts = event.get("thread_ts", event["ts"])
    thread_history = load_thread_history(event)  # For context
    
    response = dispatch_to_agent(agent, event["text"], thread_history)
    say(text=response, thread_ts=thread_ts)
```

## Structure
```
router/
├── Dockerfile
├── requirements.txt
├── app.py              # Main slack_bolt app
├── config.py           # Agent map, tokens, settings
├── dispatcher.py       # Sends work to agent containers
└── session_manager.py  # Tracks active sessions, handles timeout
```

## Acceptance Criteria
- [ ] Router receives Slack events from Lisa app
- [ ] Correctly identifies the agent from the event
- [ ] Placeholder dispatch returns echo response to Slack thread
- [ ] Responds in-thread (not as a new message)
- [ ] Structure supports adding more agents without refactoring
- [ ] Runs in Docker container
```

---

## SUB-ISSUE 5

**Title:** `Port Lisa's role.md and systems/*.md from OpenClaw`

**Labels:** `content`, `phase-1`

**Body:**

```markdown
## Context
Lisa's persona and tool documentation exist in the OpenClaw setup. We need to port them to the new repo structure and adapt anything that references OpenClaw-specific behavior.

## Tasks
- [ ] Copy Lisa's role.md from OpenClaw
- [ ] Copy relevant systems/*.md files:
  - `systems/outlook.md`
  - `systems/zoho-mail.md`
  - `systems/google.md`
- [ ] Review role.md for OpenClaw-specific references and update:
  - Remove any OpenClaw API references
  - Update tool invocation patterns for Claude Code CLI / MCP
  - Ensure memory instructions reference the new memory/ structure
- [ ] Review systems/*.md for accuracy
- [ ] Place files in correct paths:
  - `agents/lisa/role.md`
  - `systems/outlook.md`
  - `systems/zoho-mail.md`
  - `systems/google.md`

## Acceptance Criteria
- [ ] Lisa's role.md is in `agents/lisa/role.md`, free of OpenClaw references
- [ ] All three systems/*.md files ported and reviewed
- [ ] No broken references to tools or APIs that don't exist in the new system
```

---

## SUB-ISSUE 6

**Title:** `Lisa agent container + Claude Code CLI integration`

**Labels:** `core`, `phase-1`, `priority-critical`

**Depends on:** Base image (issue 2), Router (issue 4), Role.md (issue 5)

**Body:**

```markdown
## Context
This is where it comes alive. The Lisa container receives a message from the router, spawns a Claude Code CLI session with her persona, and returns the response.

## Implementation

### Container setup
```dockerfile
FROM agent-base:latest

# Copy Lisa's MCP server configs
COPY mcp-configs/lisa.json /etc/claude/mcp-config.json

# Mount points (defined in docker-compose)
# /memory — shared org memory
# /agent — agents/lisa/ (role.md, memory.md)
# /systems — systems/*.md
```

### Dispatch flow
1. Router sends message + thread context to Lisa container
2. Container builds the full prompt:
   - System prompt from `/agent/role.md`
   - Context from thread history
3. Spawns Claude Code CLI with system prompt and MCP config
4. Captures response
5. Returns response to router → Slack

### Docker Compose (Phase 1)
```yaml
services:
  router:
    build: ./router
    ports:
      - "3000:3000"
    environment:
      - LISA_BOT_TOKEN=xoxb-...
      - LISA_SIGNING_SECRET=...
    
  lisa:
    build:
      context: .
      dockerfile: agents/lisa/Dockerfile
    volumes:
      - ./memory:/memory
      - ./agents/lisa:/agent
      - ./systems:/systems:ro
    mem_limit: 512m
```

## Acceptance Criteria
- [ ] Lisa container builds and starts
- [ ] Router dispatches message to Lisa container
- [ ] Claude Code CLI spawns with Lisa's system prompt
- [ ] Response returned to router and posted in Slack
- [ ] MCP config loaded (even if no MCP servers connected yet — no errors)
- [ ] Container respects memory limit
- [ ] End-to-end test: @Lisa in Slack → meaningful response in Lisa's voice
```

---

## SUB-ISSUE 7

**Title:** `Thread awareness — multi-turn conversations`

**Labels:** `core`, `phase-1`

**Depends on:** CLI integration (issue 6)

**Body:**

```markdown
## Context
When Bram sends multiple messages in a Slack thread, Lisa needs to understand the full conversation. Since Claude Code CLI sessions are ephemeral, we need to pass thread history as context.

## Implementation
1. When a message arrives in a thread, router calls Slack API to fetch thread history
2. Format thread messages as conversation context:
   ```
   [Bram]: Can you check my calendar for tomorrow?
   [Lisa]: You have 3 meetings tomorrow...
   [Bram]: Move the 2pm to Thursday
   ```
3. Pass this as part of the prompt to Claude Code CLI
4. Lisa's response is contextually aware of the full thread

## Edge Cases
- First message in a thread (no history) — handle cleanly
- Very long threads — truncate older messages, keep recent N messages
- Thread with timeout summary from previous session — handled in issue 11

## Acceptance Criteria
- [ ] Multi-message thread conversation flows naturally
- [ ] Lisa references earlier messages in the thread
- [ ] Long threads are truncated gracefully (no token overflow)
- [ ] First message in thread works without errors
```

---

## SUB-ISSUE 8

**Title:** `Memory folder structure and MEMORY.md`

**Labels:** `memory`, `phase-1`

**Body:**

```markdown
## Context
Set up the persistent memory system that all agents will share. This is the foundation for cross-session continuity.

## Folder Structure
```
memory/
├── MEMORY.md              # 2KB curated index
├── daily/                 # YYYY-MM-DD.md, 30-day retention
├── projects/              # One file per project
├── decisions/             # Never deleted — institutional memory
├── preferences/           # How Bram likes things done
├── people/                # People context
└── lessons/               # Mistakes and learnings

agents/
├── lisa/
│   ├── role.md            # System prompt + persona (from issue 5)
│   └── memory.md          # Lisa's accumulated knowledge
```

## Tasks
- [ ] Create folder structure in repo
- [ ] Seed MEMORY.md with initial org context from OpenClaw
- [ ] Create Lisa's agents/lisa/memory.md (empty or seeded from OpenClaw)
- [ ] Define format conventions:
  - Headers: `## [DATE] — [Topic]`
  - Entries: 2-3 sentences max
  - Include WHY, not just what
- [ ] Document the memory system rules (what to save, how to save, curation rules)
- [ ] Configure Docker volumes to mount these correctly

## Acceptance Criteria
- [ ] Folder structure exists and is mounted in Lisa container
- [ ] MEMORY.md has initial content
- [ ] Lisa's memory.md exists
- [ ] Format conventions documented in a CONTRIBUTING.md or similar
```

---

## SUB-ISSUE 9

**Title:** `Memory loading on session start`

**Labels:** `memory`, `phase-1`

**Depends on:** Memory structure (issue 8), CLI integration (issue 6)

**Body:**

```markdown
## Context
On every new session, Lisa needs to load her memory context before responding. This is what gives her continuity between conversations.

## Loading Order
1. `MEMORY.md` — org-wide index (always, ~2KB)
2. `agents/lisa/memory.md` — Lisa's personal memory (always)
3. `systems/outlook.md`, `systems/zoho-mail.md`, `systems/google.md` — tool docs (always for Lisa)
4. Thread summary (if resuming a timed-out thread — handled in issue 11)

## Implementation
- Build a context assembly function that reads these files and formats them as part of the system prompt or initial context
- Keep total injected context under a reasonable token budget (track sizes)
- Log which files were loaded for debugging

## Proactive Memory Behavior
Include instructions in the system prompt that tell Lisa to:
- Search memory before answering questions about past decisions, people, projects
- Surface relevant memories proactively
- Flag contradictions with previous decisions

## Acceptance Criteria
- [ ] Session starts with MEMORY.md + agent memory + systems docs loaded
- [ ] Lisa's responses reflect knowledge from memory files
- [ ] Total injected context stays under token budget
- [ ] Loading is logged for debugging
```

---

## SUB-ISSUE 10

**Title:** `Memory persistence on session end (clean + timeout)`

**Labels:** `memory`, `phase-1`, `priority-critical`

**Depends on:** Memory loading (issue 9)

**Body:**

```markdown
## Context
Two ways a session ends. Both must persist memory.

## Clean Exit ("Thanks")
1. Bram says "Thanks" (or similar — define trigger patterns)
2. Router detects the trigger
3. Before closing, router asks Claude Code CLI to generate a memory update:
   - Decisions made → `memory/decisions/`
   - People mentioned → `memory/people/`
   - Project updates → `memory/projects/`
   - Preferences expressed → `memory/preferences/`
   - Frustrations (signal real priorities) → note in relevant file
   - Update `agents/lisa/memory.md` with session learnings
4. Router writes the updates to the mounted volumes
5. Session closes

## Timeout Exit (no message for ~10 min)
1. Session manager detects inactivity (configurable timeout, default 10 min)
2. Router asks Claude Code CLI to generate:
   a. **Thread summary** — compact, posted as a visible Slack message in the thread
   b. **Memory updates** — same as clean exit
3. Router posts summary to Slack thread
4. Router writes memory updates to mounted volumes
5. Session closes

## Thread Summary Format
```
Session paused. Here's where we left off:
Topic: [what we were discussing]
Key points: [bullet points of decisions/discussion]
Open question: [what was unresolved]
Pending action: [what Lisa was waiting for]
```

## Implementation Details
- Timeout detection: heartbeat per active session in session_manager.py
- Trigger patterns for clean exit: "Thanks", "Thank you", "Cheers", etc.
- Memory write is atomic: write to temp file, then move (prevent corruption)
- If CLI fails to generate memory update, log error but don't lose the session

## Acceptance Criteria
- [ ] "Thanks" triggers clean exit with memory persistence
- [ ] 10-min inactivity triggers timeout with thread summary + memory persistence
- [ ] Memory files are updated correctly (check file contents after session)
- [ ] Thread summary appears as a visible message in Slack
- [ ] Atomic writes prevent corruption
- [ ] Timeout is configurable
- [ ] Graceful handling if memory generation fails
```

---

## SUB-ISSUE 11

**Title:** `Session resume from thread summary`

**Labels:** `memory`, `phase-1`

**Depends on:** Timeout persistence (issue 10), Thread awareness (issue 7)

**Body:**

```markdown
## Context
When Bram comes back to a thread that timed out, the session summary is right there in the thread. The new session should use it to pick up where things left off.

## Implementation
1. New message arrives in a thread
2. Router fetches thread history (already built in issue 7)
3. Router detects a session summary message (from Lisa's bot, matching the summary format)
4. Instead of loading full thread history, prioritize the summary as primary context
5. Lisa's new session starts with: role.md + memories + summary + new message
6. Lisa responds as if continuing the conversation

## Edge Cases
- Thread has multiple timeout summaries (conversation paused and resumed multiple times) — use the most recent
- Summary + thread history together exceed token budget — prefer summary over older messages
- Thread has no summary (first message or clean-exited thread) — fall back to normal thread history

## Acceptance Criteria
- [ ] Timeout a conversation → come back later → Lisa resumes coherently
- [ ] Lisa references points from the summary without re-asking
- [ ] Multiple resume cycles work (timeout → resume → timeout → resume)
- [ ] Token budget respected even with long threads + summaries
```
