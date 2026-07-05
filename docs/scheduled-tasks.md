# Scheduled Tasks

Recurring agent invocations driven by cron schedules. An agent can have tasks that fire on a schedule, run a prompt through the dispatcher, and post results to Slack.

## Architecture

```
/tasks (Slack)          bootstrap.py
     │                      │
     ▼                      ▼
 handlers.py ◄──── setup_scheduled_tasks()
     │                      │
     ▼                      ▼
  store.py             scheduler.py
     │                      │
     ▼                      ▼
  SQLite              dispatcher.dispatch()
                            │
                            ▼
                      Agent container
                            │
                            ▼
                    Slack chat_postMessage
```

Six modules, each with a single responsibility:

| Module | Role |
|---|---|
| `store.py` | SQLite CRUD with agent-scoped ownership |
| `cron.py` | 5-field POSIX cron parser and next-run calculator |
| `scheduler.py` | Async polling daemon that fires due tasks |
| `handlers.py` | Slack `/tasks` slash command and modal handlers |
| `block_kit.py` | Block Kit UI builders for task list and create modal |
| `seeds.py` | Default task definitions, seeded idempotently at startup |
| `bootstrap.py` | Wires everything together and spawns the scheduler |

## Data Model

Each task is a row in `scheduled_tasks` (SQLite):

| Column | Type | Description |
|---|---|---|
| `task_id` | TEXT PK | UUID |
| `agent_name` | TEXT | Owning agent (e.g. "lisa") |
| `name` | TEXT | Human-readable label |
| `prompt` | TEXT | Message sent to the agent |
| `schedule_cron` | TEXT | 5-field cron expression (UTC) |
| `destination` | TEXT | Slack channel ID (nullable) |
| `enabled` | INTEGER | 1 = active, 0 = paused |
| `created_at` | TIMESTAMP | When the task was created |
| `last_run_at` | TIMESTAMP | Last execution time (nullable) |
| `next_run_at` | TIMESTAMP | Next scheduled execution |

**Agent scoping:** Every store method that accepts `agent_name` enforces ownership. An agent cannot read, modify, or delete another agent's tasks. Violations raise `ScopeError`.

## Cron

In-house parser covering standard 5-field POSIX syntax: `minute hour day-of-month month day-of-week`.

Supported field syntax: `*`, `N`, `N-M` (range), `A,B,C` (list), `*/N` (step), `A-B/N` (stepped range).

Day-of-month and day-of-week use POSIX OR semantics: when both are restricted, a match in either is sufficient.

`next_run_after(expression, after)` walks forward minute-by-minute from `after + 1 minute` until a match is found (capped at one year).

## Scheduler

An async background daemon spawned as an `asyncio.Task` at startup.

1. Wakes every 30 seconds (configurable via `poll_interval`).
2. Queries `store.list_due(now)` for enabled tasks whose `next_run_at <= now`.
3. For each due task, calls `run_task()`:
   - Resolves destination: task's `destination` field, or `OPERATOR_DM_CHANNEL` setting (legacy alias: `BRAM_DM_CHANNEL`).
   - Dispatches via `dispatch(agent_name, prompt, channel, ...)`.
   - Posts the agent's response to Slack.
   - Recomputes `next_run_at` via `cron.next_run_after()` and persists it.
4. Failed dispatches or posts still advance `next_run_at` to prevent busy-loops.
5. Supports graceful shutdown via an `asyncio.Event`.

## Slack App Setup

The `/tasks` slash command must be registered in your Slack app configuration before Slack will route it to the Bolt app. Without this, Slack responds with "/tasks is not a valid command."

1. Go to your app at **api.slack.com/apps**.
2. Navigate to **Slash Commands** in the sidebar.
3. Click **Create New Command**.
4. Fill in:
   - **Command:** `/tasks`
   - **Short Description:** Manage scheduled agent tasks
   - **Usage Hint:** `[list | create | pause <id> | resume <id> | delete <id>]`
   - Request URL is not needed for Socket Mode apps.
5. Save and **reinstall the app** to the workspace when prompted.

## Slack Integration

### `/tasks` slash command

| Subcommand | Action |
|---|---|
| `list` (default) | Show the calling agent's tasks |
| `create` | Open a modal to define a new task |
| `pause <task_id>` | Disable a task |
| `resume <task_id>` | Re-enable a task |
| `delete <task_id>` | Remove a task |

### Create modal

Opened by `/tasks create`. Fields: task name, prompt (multiline), cron schedule, optional destination channel. Validates the cron expression before accepting. On success, inserts the task and posts a confirmation to the user's DM.

## Startup Flow

Called from `router/app.py` inside `main()`:

```python
setup_scheduled_tasks(
    bolt_app=app,
    slack_client=app.client,
    dispatch_fn=dispatch,
    agent_resolver=_resolve_agent_for_command,
)
```

`setup_scheduled_tasks()` does four things in order:

1. Opens (or creates) the SQLite database at `$SCHEDULED_TASKS_DB` or `scheduled_tasks.db`.
2. Seeds default tasks idempotently (skips if `(agent_name, name)` already exists).
3. Registers `/tasks` command and modal submission handlers with the Bolt app.
4. Spawns `run_forever()` as a background asyncio task.

Returns `(store, scheduler_task)` so the caller can shut them down cleanly.

## Configuration

| Setting | Source | Default |
|---|---|---|
| Database path | `$SCHEDULED_TASKS_DB` | `scheduled_tasks.db` |
| Fallback destination | `$OPERATOR_DM_CHANNEL` | none |
| Poll interval | `scheduler.py` constant | 30 seconds |
| Task dispatch timeout | `scheduler.py` constant | 300 seconds |

## Default Seed Tasks

| Agent | Name | Schedule | Enabled |
|---|---|---|---|
| lisa | Daily inbox review | `0 9 * * 1-5` (9 AM UTC, weekdays) | No (must be enabled via `/tasks resume`) |

## System Tasks

Beyond agent prompts on a cron schedule, the store and scheduler support a second flavor of task — **system tasks** — that invoke a Python callable directly instead of going through `dispatch_fn`. Introduced in #163 to power the dispatch supervision loop (`router.dispatch.supervision`) without burning a Claude session per poll.

System tasks add three columns to `scheduled_tasks`:

| Column | Type | Description |
|---|---|---|
| `callable_ref` | TEXT | Dotted path of the form `pkg.module:attr` to import |
| `payload` | TEXT | JSON dict passed to the callable as `payload=` |
| `period_seconds` | INTEGER | Polling cadence in lieu of a cron expression |

Create a system task via:

```python
store.create_system_task(
    agent_name="sam",
    name=f"supervise {dispatch_id}",
    callable_ref="router.dispatch.supervision:check_dispatch",
    payload={"dispatch_id": dispatch_id, "channel": channel, "thread_ts": thread_ts, "agent": "sam"},
    period_seconds=120,
)
```

On each tick the scheduler imports `callable_ref` and awaits `fn(payload=..., slack_client=..., now=...)`. Return `{"status": "done"}` from the callable to deregister the task; any other return value (or none) keeps it scheduled at `now + period_seconds`. Exceptions are logged and treated as "keep polling" — supervision correctness must not depend on the callable being crash-free.

The migration is idempotent: existing deployments without the new columns get them via `ALTER TABLE ADD COLUMN` on the next `ScheduledTaskStore` open. Pre-#163 cron tasks are untouched and the new columns default to `NULL` for them.
