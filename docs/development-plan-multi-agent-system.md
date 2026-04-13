# Development Plan: Multi-Agent Slack System

**Project:** Replace OpenClaw with a self-hosted multi-agent system running on Claude Code CLI (Max subscription)
**Infrastructure:** Surface Pro 8GB RAM (Ubuntu), Docker, 6 Slack apps, Claude Code CLI
**Principle:** Get something working fast, iterate from there.

---

## Architecture Overview

```
Slack (6 apps with unique avatars/@commands)
        │
        ▼
┌──────────────────────────────┐
│   Router Service (container) │  ← Python/slack_bolt
│   • Receives all webhooks    │
│   • Identifies agent         │
│   • Manages sessions/timeout │
│   • Inter-agent routing      │
│   • Dispatches to agent      │
│     containers via Docker    │
└────────┬─────────────────────┘
         │ docker exec / internal API
         ▼
┌────────────────────────────────────────────────┐
│   Agent Containers (one per agent)             │
├────────┬────────┬───────┬──────┬───────┬───────┤
│ Lisa   │ Alex   │ Sam   │ Dave │ Maya  │ Lin   │
│        │        │       │      │       │       │
│ MCP:   │ MCP:   │ MCP:  │ MCP: │ MCP:  │ MCP:  │
│ Outlook│ Google │GitHub │Post  │Brevo  │Figma  │
│ Zoho   │ PostHog│Post   │Hog   │Notion │Post   │
│ Google │ Notion │Hog    │GitHub│Blue   │Hog    │
│        │ GitHub │Play   │Notion│sky    │Google │
│        │ PTH API│wright │PTH   │PTH API│PTH API│
│        │        │PTH API│API   │Eleven │       │
│        │        │       │Play  │Labs   │       │
│        │        │       │wright│Play   │       │
│        │        │       │      │wright │       │
│        │        │       │      │Desc.  │       │
└──┬─────┴──┬─────┴──┬────┴──┬──┴──┬────┴──┬────┘
   │        │        │       │     │       │
   └────────┴────────┴───┬───┴─────┴───────┘
                         │
              ┌──────────┴──────────┐
              │   Shared Volumes    │
              ├─────────────────────┤
              │ /memory (shared)    │  ← MEMORY.md, daily/, decisions/,
              │                     │    projects/, preferences/, people/,
              │                     │    lessons/
              │ /agents (per-agent) │  ← {name}/role.md, {name}/memory.md
              │ /systems (shared)   │  ← *.md tool documentation
              └─────────────────────┘
```

**Container strategy:** Lightweight containers, not permanently running Claude Code.
Each container has Claude Code CLI installed + its MCP server configs. The router
dispatches work to the right container, which spawns a CLI session, responds, and
goes idle. Memory limits per container prevent one agent from starving the others.
Target: ~512MB limit per agent container, ~1GB for agents with Playwright.

---

## Hardware: Surface Pro (8GB RAM) Budget

```
OS + system overhead:     ~1.5 GB
Router container:         ~256 MB
Agent containers (idle):  ~128 MB × 6 = 768 MB
Active CLI session:       ~256 MB (runs on Anthropic's servers, local is light)
Playwright (when active): ~512 MB (only Sam/Dave/Maya, not concurrent)
Headroom:                 ~4.7 GB available
```

Comfortable for typical usage (1-2 agents active at a time). Gets tight only if
3+ Playwright sessions run simultaneously, which shouldn't happen in normal use.
If it does, queue them.

---

## Legacy Architecture Note

The system replaces OpenClaw. Key change: agents run via Claude Code CLI (billed
against Max subscription) instead of API calls (billed per token). This eliminates
overage charges beyond the $100/month subscription.

---

## Memory System (unchanged)
│ MEMORY.md (2KB index)   │
│ memory/daily/           │
│ memory/projects/        │
│ memory/decisions/       │
│ memory/preferences/     │
│ memory/people/          │
│ memory/lessons/         │
│ agents/{name}/memory.md │
│ agents/{name}/role.md   │
│ systems/*.md            │
└─────────────────────────┘
```

---

## The Agents

### Lisa — Admin
**Persona:** Executive assistant. Manages calendar, email, and todo.
**Tools:**
- Outlook Calendar + Mail (Microsoft Graph via Azure app)
- Zoho Mail + Maton (lisa@pathtohired.com)
- Google (Drive, Docs, Sheets — scheduling-related)

**MCP servers:** Outlook MCP, Zoho MCP (custom), Google MCP
**Notes:** Gatekeeper of Bram's time. PathToHired browser access for admin tasks.

### Alex — Cofounder
**Persona:** Strategic thinking partner. Co-founder of Path to Hired, also explores new ventures.
**Tools:**
- PostHog (data-driven strategy)
- Google Docs/Sheets (strategy docs)
- Notion (content calendar context)
- GitHub (understand what's being built)
- PathToHired API

**MCP servers:** PostHog MCP, Google MCP, Notion MCP, GitHub MCP, PathToHired API (custom)
**Notes:** Mostly read access. Advisor role — reasons about data, doesn't operate tools heavily. Should be able to pull up metrics and reports to ground strategic conversations.

### Sam — Tech Lead
**Persona:** Senior architect. Security, performance, infrastructure, code quality. Does NOT write code — creates GitHub issues with specs that Claude Code picks up.
**Tools:**
- GitHub (read-write: issues, projects, PRs for review)
- PathToHired API
- PostHog (performance analytics)
- Playwright MCP (browser — for inspecting deployed app)

**MCP servers:** GitHub MCP, PathToHired API (custom), PostHog MCP, Playwright MCP
**Notes:** Sam's output is GitHub issues with clear acceptance criteria, architectural context, and technical guidance. Issue template matters — needs to be parseable by Claude Code.

### Dave — Product Manager
**Persona:** User research expert, prioritization guru, roadmap owner. The PM dream.
**Tools:**
- PostHog (user analytics, funnels, session recordings)
- GitHub Issues + Projects (roadmap, backlog, prioritization)
- Notion (specs, user research docs)
- PathToHired API (product understanding)
- Playwright MCP (browser — for user flow walkthroughs)

**MCP servers:** PostHog MCP, GitHub MCP, Notion MCP, PathToHired API (custom), Playwright MCP
**Notes:** Dave and Sam share GitHub — Dave owns prioritization, Sam owns technical specs. Healthy tension by design.

### Maya — Product Marketing Manager
**Persona:** Social media, content strategy, email campaigns, demand gen. Manages the Brevo system.
**Tools:**
- Notion (blog drafts, content calendar)
- Brevo (email marketing + transactional)
- Bluesky (@lisa@pathtohired.com — can post)
- LinkedIn (read-only browse/research — NO posting)
- PathToHired API (blog CMS)
- ElevenLabs (TTS for content)
- Descript (video editing)
- Playwright MCP (browser — for LinkedIn research, competitor analysis)

**MCP servers:** Notion MCP, Brevo MCP (custom), Bluesky MCP (custom), PathToHired API (custom), ElevenLabs MCP, Descript MCP (custom or browser), Playwright MCP
**Notes:** Most tool-heavy agent. LinkedIn hard rule: browse and research only, never post or interact.

### Lin — Design Lead
**Persona:** UX expert, design system guardian, user flow architect.
**Tools:**
- Figma (design files, components, design tokens)
- PostHog (user behavior data for UX decisions)
- PathToHired API (understand current UI)
- Google (design specs/docs)

**MCP servers:** Figma MCP (official), PostHog MCP, PathToHired API (custom), Google MCP
**Notes:** Uses Figma MCP (direct API, no browser needed). Advises on UX, reviews user flows, maintains design system documentation.

---

## Tool-to-MCP Mapping

| Tool | MCP Approach | Status |
|------|-------------|--------|
| Outlook Calendar + Mail | Existing MCP (Microsoft Graph) | Available — already connected in Cowork |
| Zoho Mail + Maton | Custom MCP wrapping Zoho API | Needs building |
| Notion | Community Notion MCP | Available |
| Google (Drive/Docs/Sheets) | Google MCP | Available |
| LinkedIn | Playwright MCP (browse only) | Available (with constraints) |
| ElevenLabs | Custom MCP or API calls via CLI | Needs building or research |
| Bluesky | Custom MCP wrapping AT Protocol | Needs building |
| Brevo | Custom MCP wrapping Brevo API | Needs building |
| GitHub | GitHub MCP (official) | Available |
| Descript | Playwright MCP or custom | Needs research |
| PostHog | Custom MCP or Playwright | Needs building or research |
| PathToHired API | Custom MCP | Needs building |
| Figma | Figma MCP (official) | Available |
| Playwright (browser) | Playwright MCP (Microsoft) | Available |

---

## Memory System

### Structure
```
memory/
├── MEMORY.md              ← 2KB curated index, read on every session start
├── daily/                 ← YYYY-MM-DD.md, 30-day retention then archive/distill
├── projects/              ← One file per project, updated on status changes
├── decisions/             ← Never deleted. Institutional memory.
├── preferences/           ← How Bram likes things done
├── people/                ← People context: who they are, relationship
├── lessons/               ← Mistakes and learnings
└── agents/
    ├── lisa/
    │   ├── role.md        ← System prompt + persona
    │   └── memory.md      ← Lisa's accumulated knowledge
    ├── alex/
    │   ├── role.md
    │   └── memory.md
    ├── sam/
    │   ├── role.md
    │   └── memory.md
    ├── dave/
    │   ├── role.md
    │   └── memory.md
    ├── maya/
    │   ├── role.md
    │   └── memory.md
    └── lin/
        ├── role.md
        └── memory.md
```

### Session Lifecycle

**Start:** Message arrives → load role.md + MEMORY.md + agent memory.md + systems/*.md + thread summary (if exists)

**During:** Auto-save decisions, preferences, people, project updates to shared memory. Agent accumulates session-specific notes.

**Clean exit ("Thanks"):**
1. Persist decisions/facts to shared memory (decisions/, projects/, etc.)
2. Update agent's own memory.md
3. Close session

**Timeout exit (no message for ~10 min):**
1. Dump compact session summary to Slack thread (visible message)
2. Persist any decisions/facts to shared memory
3. Update agent's own memory.md
4. Close session

**Resume:** New message in thread with prior summary → load summary as context → agent picks up where it left off.

### Memory Behaviors
- Every decision saved automatically (what, why, what was rejected)
- Proactive recall: "Last time we discussed this, you decided X because Y"
- Contradiction flagging: "This contradicts the decision from March 12th to..."
- Self-curation: daily logs pruned at 30 days, MEMORY.md pruned weekly

---

## Inter-Agent Collaboration

**Pattern:** Coordinator with visible handoff in Slack threads.

**How it works:**
1. You're talking to Sam about architecture in a thread
2. Sam responds: "I'd want Dave's input on whether users actually need this. @Dave, can you check the PostHog data on feature X usage?"
3. Router detects the @Dave mention in Sam's response
4. Router spawns Dave with: Sam's summary of the conversation + the specific question
5. Dave responds in the same thread, tagged as Dave
6. You see the full exchange and can steer

**Rules:**
- Agents can only tag one other agent per response (no cascading)
- You can always override or redirect
- The receiving agent gets a summary, not the full conversation (token efficiency)
- Handoff includes: who's asking, what they need, relevant context

---

## Scheduled Tasks (Cowork Integration)

Agents can define recurring tasks that execute via Cowork:
- LinkedIn outreach campaign (existing)
- Weekly content review
- Memory gardening (prune MEMORY.md, archive old daily logs)
- Weekly analytics digest (PostHog → summary)
- Blog publishing pipeline

**Mechanism:** Agent creates a task spec → stored as a scheduled task definition → Cowork executes on schedule → results posted back to Slack or stored in memory.

---

## Development Phases

### Phase 0: One Agent, End-to-End (target: 1-2 days)
**Goal:** Prove the architecture works. One agent responding in Slack via Claude Code CLI.

**Pick Lisa** — she's the simplest (calendar/mail, no complex tool chains).

- [ ] Set up the Slack app for Lisa (avatar, bot token, event subscriptions)
- [ ] Write the router service (Node.js or Python) — receives Slack events, responds
- [ ] Create Lisa's role.md (port from OpenClaw)
- [ ] Spawn Claude Code CLI with --system-prompt from role.md
- [ ] Pipe response back to Slack
- [ ] Verify: send a message to @Lisa in Slack, get a response

**No memory, no tools, no MCP servers yet.** Just: Slack → router → Claude Code CLI → Slack. Prove the loop works.

### Phase 1: Memory System (target: 1-2 days)
**Goal:** Lisa remembers things between sessions.

- [ ] Create the memory/ folder structure
- [ ] Create MEMORY.md and Lisa's agents/lisa/memory.md
- [ ] Add memory loading to session start (role.md + MEMORY.md + agent memory)
- [ ] Add memory persistence on clean exit ("Thanks")
- [ ] Add timeout detection + thread summary dump
- [ ] Add thread history loading for session resume
- [ ] Port systems/*.md files from OpenClaw
- [ ] Test: have a conversation, end it, start a new one — Lisa remembers

### Phase 2: Lisa's Tools (target: 2-3 days)
**Goal:** Lisa can actually do things — read email, check calendar, etc.

- [ ] Configure Outlook MCP (Microsoft Graph via Azure app — already have the app)
- [ ] Configure Zoho Mail MCP (may need custom MCP)
- [ ] Configure Google MCP
- [ ] Wire MCP servers into Claude Code CLI spawn for Lisa
- [ ] Test each tool: "Lisa, what's on my calendar today?" / "Lisa, check my inbox"
- [ ] Port Lisa's systems/*.md files and verify she reads them

### Phase 3: All Six Agents (target: 3-5 days)
**Goal:** All agents live in Slack with their personas.

- [ ] Create remaining 5 Slack apps (Alex, Sam, Dave, Maya, Lin) with avatars
- [ ] Port all role.md files from OpenClaw
- [ ] Update router to handle all 6 app webhooks
- [ ] Create agent-specific memory.md files
- [ ] Configure MCP servers per agent (start with what's available off-the-shelf):
  - GitHub MCP for Sam and Dave
  - Notion MCP for Dave and Maya
  - PostHog MCP for Sam, Dave, Alex, Lin (if available, else Phase 5)
  - Figma MCP for Lin
  - Playwright MCP for Sam, Dave, Maya
- [ ] Test each agent: verify persona, verify tool access, verify memory isolation

### Phase 4: Inter-Agent Collaboration (target: 2-3 days)
**Goal:** Agents can hand off to each other in threads.

- [ ] Add @mention detection in agent responses
- [ ] Build handoff logic: summarize context → spawn target agent → post in thread
- [ ] Add handoff rules (one agent at a time, no cascading)
- [ ] Test: talk to Sam, Sam tags Dave, Dave responds in thread
- [ ] Add override mechanism (you can redirect or cancel handoffs)

### Phase 5: Custom MCPs + Browser (target: 5-7 days)
**Goal:** Build the custom tool integrations that don't exist off-the-shelf.

- [ ] PathToHired API MCP (blog CMS + auth)
- [ ] Brevo MCP (email marketing + transactional)
- [ ] Bluesky MCP (AT Protocol, posting as @lisa@pathtohired.com)
- [ ] PostHog MCP (if no community option works)
- [ ] ElevenLabs MCP (TTS)
- [ ] Zoho Mail MCP (if not done in Phase 2)
- [ ] Descript integration (MCP or browser-based)
- [ ] Playwright MCP hardening: test LinkedIn browsing, PathToHired UI
- [ ] Browserbase Stagehand as fallback if Playwright hits anti-bot walls

### Phase 6: Scheduled Tasks + Cowork (target: 2-3 days)
**Goal:** Recurring tasks run automatically.

- [ ] Define task spec format (what, when, which agent context, output destination)
- [ ] Migrate LinkedIn outreach campaign to Cowork scheduled task
- [ ] Set up weekly memory gardening task
- [ ] Set up weekly analytics digest
- [ ] Test: scheduled task fires → executes → posts result to Slack / updates memory

### Phase 7: Hardening + Polish (ongoing)
**Goal:** Make it reliable and pleasant to use daily.

- [ ] Error handling: what happens when Claude Code CLI fails, MCP server is down, etc.
- [ ] Rate limit awareness: monitor Max subscription usage, add warnings
- [ ] Session management: clean up stale sessions, handle concurrent conversations
- [ ] Memory optimization: tune MEMORY.md pruning, test at scale over weeks
- [ ] GitHub issue template for Sam → Claude Code handoff pipeline
- [ ] Agent personality tuning based on real usage

---

## Open Decisions

1. **Router language:** Node.js or Python? Python has better Claude SDK support. Node.js has better Slack SDK support. Leaning Python with slack_bolt.
2. **Claude Code CLI invocation:** Need to research exact flags for --system-prompt, MCP server config per session, and output capture.
3. **GitHub issue template for Sam:** What format makes issues most parseable by Claude Code?
4. **Descript integration:** API, MCP, or browser? Needs research.
5. **PostHog:** Community MCP exists? Or build custom?
6. **Session storage:** Where does active session state live? In-memory on the router? Redis? File-based?

---

## Hardware Considerations (Surface Pro / Ubuntu)

- Claude Code CLI is lightweight — the heavy lifting happens on Anthropic's servers
- Playwright MCP runs a browser instance — monitor RAM usage
- One agent session at a time is fine; concurrent sessions may strain memory
- Consider: if multiple agents are in parallel conversations, how many CLI processes can the Surface Pro handle?
- Backup plan: move to a small VPS if the Surface Pro can't keep up

---

## File Inventory from OpenClaw (to port)

- [ ] All role.md files (6 agents)
- [ ] MEMORY.md
- [ ] All memory/ subfolders and contents
- [ ] All systems/*.md files (13 system docs)
- [ ] Any existing scheduled task definitions
- [ ] LinkedIn outreach campaign configuration
