# Spike: Claude Code CLI Invocation for Agent Use

**Issue:** #1
**Date:** 2026-04-13
**Status:** Complete

---

## Table of Contents

1. [System Prompt](#1-system-prompt)
2. [MCP Server Configuration](#2-mcp-server-configuration)
3. [Output Capture](#3-output-capture)
4. [Session Management](#4-session-management)
5. [Authentication in Docker](#5-authentication-in-docker)
6. [Concurrency](#6-concurrency)
7. [Docker Compatibility on Ubuntu](#7-docker-compatibility-on-ubuntu)
8. [Python Integration Patterns](#8-python-integration-patterns)
9. [Gotchas and Recommendations](#9-gotchas-and-recommendations)

---

## 1. System Prompt

Claude Code provides four flags for system prompt customization:

| Flag | Behavior |
|------|----------|
| `--system-prompt <text>` | **Replaces** the entire default system prompt |
| `--system-prompt-file <path>` | **Replaces** the default prompt from a file |
| `--append-system-prompt <text>` | **Appends** to the default prompt |
| `--append-system-prompt-file <path>` | **Appends** file contents to the default prompt |

**Key constraints:**
- `--system-prompt` and `--system-prompt-file` are **mutually exclusive**
- Append flags can be combined with either replacement flag
- stdin piping is NOT supported for system prompts (only for query content)

### Recommended approach: append to keep built-in agent behavior

```bash
claude -p "Review this code for bugs" \
  --append-system-prompt "You are a security-focused code reviewer. Flag all OWASP top 10 issues."
```

### Full replacement (loses built-in agent capabilities)

```bash
claude -p "Analyze this file" \
  --system-prompt "You are a Python static analysis tool. Output only JSON."
```

### From file

```bash
claude -p "Run the analysis" \
  --append-system-prompt-file ./prompts/security-reviewer.txt
```

### Python subprocess call

```python
import subprocess, json

result = subprocess.run(
    [
        "claude", "-p", "Review auth.py for vulnerabilities",
        "--append-system-prompt", "You are a security engineer. Output findings as JSON.",
        "--output-format", "json",
    ],
    capture_output=True, text=True, timeout=300,
)
response = json.loads(result.stdout)
print(response["result"])
```

### CLAUDE.md files (context injection, not system prompt)

CLAUDE.md files are loaded as user context (not system prompt). Locations:
- `./CLAUDE.md` or `./.claude/CLAUDE.md` (project-level, checked into git)
- `./CLAUDE.local.md` (personal, add to .gitignore)
- `~/.claude/CLAUDE.md` (user-level, all projects)

**Important:** Use `--bare` mode to skip CLAUDE.md auto-loading for reproducible scripted runs.

---

## 2. MCP Server Configuration

### .mcp.json (project-level, team-shared)

Place at project root and check into version control:

```json
{
  "mcpServers": {
    "my-http-server": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${API_TOKEN}"
      }
    },
    "my-stdio-server": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@some/mcp-server"],
      "env": {
        "API_KEY": "${MY_API_KEY}"
      }
    }
  }
}
```

Environment variable expansion (`${VAR}` or `${VAR:-default}`) is supported in `.mcp.json`.

### CLI commands for MCP management

```bash
# Add servers via CLI
claude mcp add --transport http github --scope project https://mcp.github.com
claude mcp add --transport stdio myserver -- python server.py --port 8080
claude mcp add --transport stdio --env API_KEY=xxx airtable -- npx -y airtable-mcp

# Add from inline JSON
claude mcp add-json weather '{"type":"http","url":"https://api.weather.com/mcp"}'

# List, inspect, remove
claude mcp list
claude mcp get myserver
claude mcp remove myserver
```

### Three-scope hierarchy

| Scope | Storage | Shared | Flag |
|-------|---------|--------|------|
| Local | `~/.claude.json` | No | `--scope local` (default) |
| Project | `.mcp.json` | Yes | `--scope project` |
| User | `~/.claude.json` | No | `--scope user` |

### Per-invocation: no `--mcp-config` flag exists

There is **no `--mcp-config` CLI flag**. To vary MCP configuration per-invocation:
- Use environment variable expansion in `.mcp.json`
- Use `claude mcp add-json` before invocation
- Use `--bare` mode plus explicit config to isolate from local settings

### Bare mode isolation

```bash
# Skips all auto-discovery (MCP, hooks, CLAUDE.md, skills)
claude --bare -p "query" \
  --append-system-prompt-file ./rules.txt \
  --allowedTools "Read,Bash"
```

---

## 3. Output Capture

### Output format options

| Format | Flag | Use Case |
|--------|------|----------|
| Plain text | `--output-format text` (default) | Simple scripting |
| JSON | `--output-format json` | Structured parsing |
| Streaming JSON | `--output-format stream-json` | Real-time processing |

### JSON output (recommended for programmatic use)

```bash
claude -p "Summarize this project" --output-format json
```

Returns:
```json
{
  "result": "The project is a web application that...",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "total_cost_usd": 0.0123,
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 567,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0
  }
}
```

### Structured output with JSON schema validation

```bash
claude -p "Extract function names from auth.py" \
  --output-format json \
  --json-schema '{"type":"object","properties":{"functions":{"type":"array","items":{"type":"string"}}},"required":["functions"]}'
```

Returns:
```json
{
  "structured_output": {
    "functions": ["login", "logout", "verify_token"]
  },
  "session_id": "...",
  "total_cost_usd": 0.0123
}
```

### Streaming JSON (real-time token output)

```bash
claude -p "Write a poem" \
  --output-format stream-json \
  --verbose \
  --include-partial-messages
```

Each line is a newline-delimited JSON event. Filter with jq:

```bash
claude -p "Write a poem" \
  --output-format stream-json \
  --verbose \
  --include-partial-messages | \
  jq -rj 'select(.type == "stream_event" and .event.delta.type? == "text_delta") | .event.delta.text'
```

### Python: capturing JSON output

```python
import subprocess, json

result = subprocess.run(
    ["claude", "-p", "List all TODO items in this project",
     "--output-format", "json"],
    capture_output=True, text=True, timeout=300,
)

if result.returncode == 0:
    data = json.loads(result.stdout)
    print(data["result"])
    print(f"Cost: ${data['total_cost_usd']:.4f}")
    print(f"Session: {data['session_id']}")
else:
    print(f"Error: {result.stderr}")
```

---

## 4. Session Management

### Key flags

| Flag | Purpose |
|------|---------|
| `--session-id <uuid>` | Use/create a specific session |
| `--continue` / `-c` | Resume most recent session in current directory |
| `--resume <id-or-name>` / `-r` | Resume a specific session |
| `--fork-session` | Branch from an existing session (use with `--resume`) |
| `--name <name>` / `-n` | Set session display name |
| `--no-session-persistence` | Don't save session to disk (print mode only) |

### Multi-turn conversation pattern

```bash
# First invocation - capture session ID
SESSION_ID=$(claude -p "Analyze the auth module" \
  --output-format json | jq -r '.session_id')

# Continue the same conversation
claude -p "Now refactor the login function" \
  --resume "$SESSION_ID" \
  --output-format json

# Or simply continue the most recent session
claude -p "What about the logout function?" \
  --continue \
  --output-format json
```

### Named sessions

```bash
# Create a named session
claude -p "Start reviewing PR #42" --name "pr-42-review" --output-format json

# Resume by name later
claude -p "Continue the review" --resume "pr-42-review" --output-format json
```

### Session storage

Sessions are stored at: `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`

Where `<encoded-cwd>` is the working directory path with non-alphanumeric chars replaced by `-`.

### Stateless mode (no persistence)

```bash
claude -p "One-off query" --no-session-persistence --output-format json
```

### Python: multi-turn conversation

```python
import subprocess, json

def claude_query(prompt, session_id=None, system_prompt=None):
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if session_id:
        cmd.extend(["--resume", session_id])
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return json.loads(result.stdout)

# First turn
resp1 = claude_query(
    "Analyze the database schema",
    system_prompt="You are a database expert."
)
sid = resp1["session_id"]

# Second turn (continues conversation)
resp2 = claude_query("Suggest optimizations for the users table", session_id=sid)
print(resp2["result"])
```

---

## 5. Authentication in Docker

### Authentication methods (priority order)

1. Cloud provider env vars (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`)
2. `ANTHROPIC_AUTH_TOKEN` (custom Authorization header for proxies)
3. **`ANTHROPIC_API_KEY`** (Anthropic API key from Console)
4. `apiKeyHelper` script output (custom/rotating credentials)
5. `CLAUDE_CODE_OAUTH_TOKEN` (1-year OAuth token)
6. Subscription OAuth from `claude login` (default, requires browser)

### Method A: API key (simplest for Docker)

```bash
export ANTHROPIC_API_KEY=sk-ant-api03-...
claude -p "Hello" --output-format json
```

```dockerfile
FROM node:20-slim
RUN npm install -g @anthropic-ai/claude-code
WORKDIR /app
# API key passed at runtime
CMD ["claude", "-p", "--bare", "--output-format", "json", "Hello world"]
```

```bash
docker run -e ANTHROPIC_API_KEY=sk-ant-... my-claude-image
```

### Method B: Long-lived OAuth token (for Max/Pro subscriptions)

Generate on a machine with browser access:

```bash
claude setup-token
# Outputs: CLAUDE_CODE_OAUTH_TOKEN=ccdt_...
```

Use in Docker:

```bash
docker run -e CLAUDE_CODE_OAUTH_TOKEN=ccdt_... my-claude-image
```

Token is valid for 1 year. Scoped to inference only.

### Method C: apiKeyHelper (rotating credentials)

In `settings.json`:
```json
{
  "apiKeyHelper": "/app/scripts/get-token.sh"
}
```

The script is called every 5 minutes or on HTTP 401. Configure TTL:
```bash
export CLAUDE_CODE_API_KEY_HELPER_TTL_MS=300000
```

### Token storage locations

| OS | Location |
|----|----------|
| macOS | macOS Keychain (encrypted) |
| Linux | `~/.claude/.credentials.json` (mode 0600) |
| Custom | `$CLAUDE_CONFIG_DIR/.credentials.json` |

---

## 6. Concurrency

### Multiple CLI instances

Multiple `claude` processes **can run simultaneously**. Each session is isolated with its own context window.

### Considerations

- **File system contention:** If multiple instances edit the same files, conflicts will occur. Use different working directories or branches per instance.
- **Tool concurrency within a session:** Controlled by `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` (default: 10).
- **Rate limiting:** Multiple instances share the same API key quota. Monitor for 429 errors.

### Python: parallel invocations

```python
import subprocess, json
from concurrent.futures import ThreadPoolExecutor

def run_claude(prompt, workdir):
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--bare",
         "--no-session-persistence"],
        capture_output=True, text=True, timeout=600,
        cwd=workdir,
    )
    return json.loads(result.stdout)

prompts = [
    ("Review auth.py for security issues", "/app/repo1"),
    ("Review database.py for performance", "/app/repo2"),
    ("Review api.py for error handling", "/app/repo3"),
]

with ThreadPoolExecutor(max_workers=3) as pool:
    futures = [pool.submit(run_claude, p, w) for p, w in prompts]
    for f in futures:
        data = f.result()
        print(data["result"])
        print(f"Cost: ${data['total_cost_usd']:.4f}")
        print("---")
```

### Recommended isolation pattern

```
/workspaces/
  agent-1/   <-- clone or worktree for agent 1
  agent-2/   <-- clone or worktree for agent 2
  agent-3/   <-- clone or worktree for agent 3
```

```bash
# Create isolated worktrees
git worktree add ../agent-1 -b agent-1-branch
git worktree add ../agent-2 -b agent-2-branch
```

---

## 7. Docker Compatibility on Ubuntu

### Official reference Dockerfile

Anthropic provides a reference devcontainer at `github.com/anthropics/claude-code/.devcontainer/`.

### Minimal Dockerfile

```dockerfile
FROM node:20-slim

# Install system dependencies
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

# Install Claude Code globally
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user (recommended)
RUN useradd -m -s /bin/bash claude
USER claude
WORKDIR /home/claude/project

# Default command: print mode with bare flag
ENTRYPOINT ["claude"]
CMD ["-p", "--bare", "--output-format", "json", "echo hello"]
```

### Build and run

```bash
docker build -t claude-agent .

# Run with API key
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v $(pwd):/home/claude/project \
  claude-agent \
  -p --bare --output-format json "Analyze this project"
```

### Docker Compose for multi-agent setup

```yaml
version: "3.8"
services:
  claude-reviewer:
    build: .
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    volumes:
      - ./repo:/home/claude/project
    command: >
      -p --bare --output-format json
      --append-system-prompt "You are a code reviewer."
      "Review the latest changes"

  claude-tester:
    build: .
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    volumes:
      - ./repo:/home/claude/project
    command: >
      -p --bare --output-format json
      --append-system-prompt "You are a test engineer."
      "Write tests for the auth module"
```

### Known issues and mitigations

| Issue | Mitigation |
|-------|------------|
| No browser for `claude login` | Use `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` |
| Network restrictions in reference devcontainer | Custom firewall whitelists npm, GitHub, Claude API |
| Large image size with Node.js | Use `node:20-slim` as base |
| Permission issues on mounted volumes | Match UID/GID or use `--user` flag |

---

## 8. Python Integration Patterns

### Claude Agent SDK (recommended for production)

The Claude Agent SDK provides native Python/TypeScript bindings instead of subprocess calls.

**Python SDK:**

```python
from claude_code_sdk import query, ClaudeCodeOptions

async def run_agent(prompt: str, system_append: str = None):
    options = ClaudeCodeOptions()
    if system_append:
        options.system_prompt_append = system_append
    
    result = None
    async for message in query(prompt=prompt, options=options):
        if hasattr(message, 'result'):
            result = message.result
    return result
```

**Benefits over subprocess:**
- Proper streaming support
- Session management built in
- Type-safe responses
- No shell escaping issues
- File checkpointing support

### Subprocess wrapper (simpler, works everywhere)

```python
import subprocess, json, os
from dataclasses import dataclass

@dataclass
class ClaudeResponse:
    result: str
    session_id: str
    cost_usd: float
    input_tokens: int
    output_tokens: int

def claude_cli(
    prompt: str,
    system_prompt: str = None,
    session_id: str = None,
    workdir: str = None,
    timeout: int = 300,
    allowed_tools: list[str] = None,
    max_turns: int = None,
) -> ClaudeResponse:
    cmd = ["claude", "-p", prompt, "--bare", "--output-format", "json"]
    
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    if session_id:
        cmd.extend(["--resume", session_id])
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    if max_turns:
        cmd.extend(["--max-turns", str(max_turns)])
    
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout, cwd=workdir,
        env={**os.environ},
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI failed: {result.stderr}")
    
    data = json.loads(result.stdout)
    return ClaudeResponse(
        result=data["result"],
        session_id=data["session_id"],
        cost_usd=data.get("total_cost_usd", 0),
        input_tokens=data.get("usage", {}).get("input_tokens", 0),
        output_tokens=data.get("usage", {}).get("output_tokens", 0),
    )

# Usage
resp = claude_cli(
    prompt="Find bugs in auth.py",
    system_prompt="You are a security engineer.",
    allowed_tools=["Read", "Grep", "Glob"],
    max_turns=5,
)
print(resp.result)
print(f"Cost: ${resp.cost_usd:.4f}")
```

---

## 9. Gotchas and Recommendations

### Critical gotchas

1. **`--system-prompt` replaces everything.** You lose Claude Code's built-in agent behavior (tool use, file editing, etc.). Prefer `--append-system-prompt` unless you need full control.

2. **No `--mcp-config` flag.** MCP configuration must be set up via `.mcp.json`, `claude mcp add`, or scope-based config before invocation. You cannot pass MCP config inline per-invocation.

3. **`--bare` mode skips all auto-discovery.** CLAUDE.md, hooks, MCP servers, and skills are all skipped. You must pass everything explicitly. This is desirable for reproducible scripted/Docker use.

4. **Session persistence is on by default.** Use `--no-session-persistence` for one-off queries to avoid filling disk with session files.

5. **File paths in `--system-prompt-file` resolve from CWD**, not project root.

6. **Browser login doesn't work in Docker.** Must use `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.

7. **Multiple agents editing same files = conflicts.** Use separate worktrees or working directories.

8. **Rate limits are shared across instances** using the same API key.

### Recommended defaults for agent use

```bash
claude -p "<prompt>" \
  --bare \
  --output-format json \
  --append-system-prompt "<role instructions>" \
  --allowedTools "Read,Grep,Glob,Bash" \
  --max-turns 10 \
  --no-session-persistence
```

### Permission modes

| Mode | Flag | Behavior |
|------|------|----------|
| Default | (none) | Prompts for each tool approval |
| Allow specific | `--allowedTools "Read,Bash"` | Auto-approve listed tools |
| Don't ask | `--permission-mode dontAsk` | Auto-approve all tools |

For agent use, `--allowedTools` with an explicit list is the safest approach. `--permission-mode dontAsk` gives full autonomy but carries risk.

---

## Summary: Quick Reference

| Question | Answer |
|----------|--------|
| Custom system prompt? | `--append-system-prompt` or `--system-prompt-file` |
| MCP config per-invocation? | No CLI flag; use `.mcp.json` + env var expansion |
| Structured output? | `--output-format json` with `-p` flag |
| Continue a session? | `--resume <session-id>` or `--continue` |
| Auth in Docker? | `ANTHROPIC_API_KEY` env var (simplest) |
| Parallel agents? | Yes, use separate working directories |
| Docker compatible? | Yes, `node:20-slim` + `npm install -g @anthropic-ai/claude-code` |
