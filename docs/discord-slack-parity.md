# Discord ↔ Slack transport parity

> **Scope:** gap analysis of the Discord transport (`router/chat/adapters/discord.py`
> + the `run_agent_turn` core seam) against the live Slack path
> (`router/slack_events.py` `_handle_event` / `handle_message` + `router/dispatcher.py`
> `dispatch()`), and the fixes that closed them.
> **Related:** `docs/chat-backends-architecture.md` (epic #121), #126 (Discord MVP),
> #658 (aidt command surface), #663–#669 (TransportRef).

## Closed gaps

### Inbound pipeline (adapter)

| # | Gap | Slack behaviour | Discord fix |
|---|-----|-----------------|-------------|
| 1 | Bot-message guard | Bot-authored events are dropped unless allowlisted (`DISPATCH_BOT_USER_IDS` + auto-seeded agent IDs) so agents can never echo-loop each other | `message.author.bot` messages are dropped unless the snowflake is in `DISCORD_DISPATCH_BOT_IDS`; allowlisted bots dispatch with `human_initiated=False` |
| 2 | Summary guard (Guard 1, #547) | Allowlisted-bot messages containing `SUMMARY_MARKERS` are context-only, never a dispatchable turn | Same check before dispatch |
| 3 | Event dedup | `_seen_events` TTL cache keyed per agent+message identity | Per-adapter TTL cache keyed by gateway message ID (guards against redelivery after reconnect/resume) |
| 4 | DM support | `channel_type == "im"` messages always handled | `message.guild is None` messages always handled (no mention needed, no thread creation; guild encoded as `0` in the ref) |
| 5 | Active-agent thread routing | Un-mentioned thread replies route only to the thread's active agent (thread-state store) | Same store, keyed by `(channel_id, thread_id)`; falls back to thread membership when no active agent is recorded yet |
| 6 | Thread-state recording | `set_active_agent` on every handled event, before pack-command short-circuits | Same |
| 7 | Agent handoff | Response text mentioning another agent promotes them to thread-active | Same, via `mentions.last_mentioned` on plain `@name` mentions |
| 8 | Pack command surface | `grant` / `revoke` / `list packs` / `who has` + pending-reply resolution (`router.chat.pending_input.resolve_reply`) handled inline | Same functions wired in, keyed by Discord channel/thread IDs |
| 9 | Session management | Session per agent+thread; thread history recorded; activity updates | Same; Discord sessions are tagged `transport="discord"` + `conversation_ref` |
| 10 | Clean-exit trigger | "thanks"/"bye" → memory extraction + goodbye + session cleanup | Same |
| 11 | Timeout summaries | Session-cleanup loop posts a summary via the agent's Slack client | Cleanup loop resolves a transport-aware client; Discord sessions post through their adapter (`_DiscordSessionClient` facade) |
| 12 | Memory curation | First message of the day triggers background curation | Same |
| 13 | Attachments ingest (#328) | Files validated (count/size/mimetype/thread caps), downloaded to `/var/lib/attachments/<thread>/`, `[ATTACHMENTS]` block prepended; rejection/failure aborts dispatch | Same module reused; Discord attachments mapped to the Slack-shaped file dicts (CDN URLs need no auth token); GC mtime bump (#327) included |
| 14 | Error replies | Classified message with correlation ID (`error_classifier`) | Same (replaces the old "hit an error, check the logs") |
| 15 | Scheduled tasks store | `/<agent>-tasks` slash command backed by shared store | `aidt tasks …` now receives the shared store (`_build_discord_adapters(tasks_store=…)`) — previously it was never wired |

### Core seam (`run_agent_turn` vs `dispatch()`)

| # | Gap | Fix |
|---|-----|-----|
| 16 | Stuck guard (#422) | Turn accounting, halt pre-check (`TaskHaltedError`), human-initiated cap reset, trip post-mortems + in-conversation notification — task ID keyed by opaque `conversation_ref` |
| 17 | Pack CLI extras | `pack_cli_extras(conversation_ref=…)` injects prompt files, MCP config, and env (incl. `DISPATCH_TRANSPORT=discord` / `DISPATCH_CONVERSATION_ID`), so agent-initiated dispatches work from Discord conversations (TransportRef, #663/#665) |
| 18 | Session-summary resume | `split_messages_at_summary` with a Guard-2 provenance equivalent: adapters set `InboundMessage.is_summary` only for marker text **authored by their own bot** (Slack uses message metadata; Discord has none, so bot-authorship is the provenance signal) |
| 19 | Token budget | `MAX_CONTEXT_TOKENS` env respected (was fixed at the default) |
| 20 | History cap | Thread history capped at the dispatcher's 20-message limit |
| 21 | API-error classification | `API Error: NNN` in stderr raises `ApiError` so 429/529 produce the friendly "overloaded, retry" reply |
| 22 | Timeout notification | `:alarm_clock:` router-timeout note posted to the conversation |

## Known remaining gaps (tracked, out of scope here)

- **Approval cards are still Slack-only.** `router/internal_api.py` posts the
  Block Kit card via the agent's Slack client regardless of the draft's
  `transport`. A Discord-origin gated dispatch persists its draft (and the
  approve→execute path is transport-aware since #665), but the card itself is
  not rendered into Discord. `DiscordApprovalAdapter` (embed + button specs)
  exists and `prompt_for_choice` has a working button round-trip; the missing
  piece is internal_api → Discord adapter wiring plus a persistent-button
  handler backed by the DraftStore. Needs its own design pass (interaction
  identity, TTL, expiration worker parity).
- **Workers-bot identity (#270).** Dispatch lifecycle posts on Discord are sent
  by the agent's own bot; a `WORKERS_DISCORD_TOKEN` exists for the pack side
  but the router does not yet post supervision envelopes over Discord.
- **Scheduled-task delivery.** The scheduler resolves Slack clients for task
  destinations; cron tasks that should post into Discord conversations need a
  transport-aware destination (same shape as the session-cleanup fix).
- **`prompt_for_choice` timeout default.** On timeout/undeliverable card the
  method returns the *first* choice. Interactive callers must order choices so
  the safe option is first; the approval flow proper does not use this path.
- **Markdown fidelity.** Slack uses `md_to_slack`; Discord renders standard
  markdown natively, so no converter is needed (not a gap).
