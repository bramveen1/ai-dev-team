"""Router app — composition root for the multi-agent slack_bolt application.

Loads config, constructs one ``AsyncApp`` + ``AsyncSocketModeHandler`` per
agent that has configured Slack credentials, wires the extracted subsystems
together, and runs startup (``main``):

* event pipeline        — :mod:`router.slack_events`
* approved-draft exec   — :mod:`router.approvals.execute`
* session lifecycle     — :mod:`router.session_lifecycle`
* shared registries     — :mod:`router.runtime`

Module-level aliases to the moved names are kept so existing importers keep
working after the split.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from router import background, config_page, runtime, session_lifecycle, settings, slack_events
from router import log_buffer as _log_buffer
from router.approvals import execute as _approvals_execute
from router.approvals.execute import _execute_approved_draft
from router.approvals.handlers import register_handlers as register_approval_handlers
from router.approvals.store import DraftStore
from router.attachments import attachments_enabled, log_channel_membership_warnings
from router.chat.adapters.slack_client import SlackApiError, build_web_client
from router.config import get_agent_map, load_config, load_discord_credentials
from router.dispatch.attachments_sweep import register_attachments_sweep
from router.dispatch.discovery import start_discovery_loop
from router.dispatcher import dispatch
from router.healthz import mark_ready, set_wakeup_store, workspace_volume_writable
from router.healthz import start_server as start_healthz_server
from router.internal_api import (
    check_token_configured,
    start_internal_server,
)
from router.internal_api import (
    configure as configure_internal_api,
)
from router.kill_command import register_kill_handler
from router.merge_queue import register_merge_queue
from router.scheduled_tasks.bootstrap import (
    open_store,
    setup_scheduled_tasks_handlers,
    start_scheduled_tasks_scheduler,
)
from router.session_lifecycle import (
    _expiration_worker_loop,
    _session_cleanup_loop,
    _session_end_client,
)
from router.slack_events import (
    DEFAULT_THINKING_STATUS,
    _agent_owns_dispatch_thread,
    _handle_event,
    _is_dispatch_bot_sender,
    _make_event_handlers,
    _seen_events,
    handle_app_mention,
    handle_message,
    set_assistant_status,
)
from router.threads.state import get_default_store

load_dotenv()

config = load_config()

# Share the loaded config with the extracted subsystems. Re-runs on module
# reload (tests) so all of them observe the freshly loaded config.
slack_events.configure(config)
_approvals_execute.configure(config)
session_lifecycle.configure(config)

logging.basicConfig(
    level=getattr(logging, config["log_level"], logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Install in-memory ring buffer so /logs endpoint can serve recent lines.
_log_buffer.install()
logger = logging.getLogger(__name__)

_bolt_logger = logging.getLogger("slack_bolt")
_bolt_logger.setLevel(logging.INFO)

for _discord_logger_name in ("discord", "discord.gateway", "discord.client", "discord.http"):
    logging.getLogger(_discord_logger_name).setLevel(logging.WARNING)

# --- Approval flow setup ---
_draft_store = DraftStore()

# Back-compat aliases into the shared runtime registries (all mutated in
# place, never rebound — see router/runtime.py). Socket Mode handlers are
# constructed lazily in main() because their aiohttp client session needs
# a running event loop.
_apps_by_agent: dict[str, AsyncApp] = runtime.apps_by_agent
_app_tokens_by_agent: dict[str, str] = runtime.app_tokens_by_agent
_bot_user_id_by_agent: dict[str, str] = runtime.bot_user_id_by_agent
_bot_user_map: dict[str, str] = runtime.bot_user_map
_dispatch_bot_user_ids: set[str] = runtime.dispatch_bot_user_ids
_discord_adapters: list = runtime.discord_adapters
_workers_client = runtime.workers_client
_client_for_agent = runtime.client_for_agent

# Strong references to long-lived background tasks — shared with
# router.dispatcher via router.background; see that module's docstring.
# Aliased under the old private names for call sites and tests.
_background_tasks = background.background_tasks
_spawn_background_task = background.spawn_background_task

# Module-level handles for the HTTP servers. Kept alive for the lifetime
# of the process so the aiohttp ``AppRunner`` objects aren't GC'd while
# their sockets are still listening.
_healthz_runner: Any | None = None
_internal_runner: Any | None = None


def _resolve_active_agent_for_kill(channel: str, thread_ts: str) -> str | None:
    """Look up the thread's most recently active agent for ``/kill`` fallback.

    When an operator types ``/kill`` with no agent name we kill whoever
    is currently working in this thread. Reads from the thread state
    store the same way the message router does.
    """
    try:
        return get_default_store().get_active_agent(channel, thread_ts)
    except Exception:
        logger.exception("Failed to resolve active agent for /kill fallback")
        return None


def _build_apps() -> None:
    """Construct one ``AsyncApp`` per agent with configured Slack credentials.

    Each app is registered with the same approval/event/scheduled-task handlers,
    bound to the agent name via closure so the receiving agent is known.
    """
    _apps_by_agent.clear()
    _app_tokens_by_agent.clear()

    slack_credentials = config.get("slack_credentials", {})
    if not slack_credentials:
        logger.warning("No Slack credentials configured for any agent; router will not connect to Slack")
        return

    for agent_name, creds in slack_credentials.items():
        bolt_app = AsyncApp(
            token=creds["bot_token"],
            signing_secret=creds["signing_secret"],
            logger=_bolt_logger,
        )
        register_approval_handlers(bolt_app, _draft_store, execute_callback=_execute_approved_draft)

        # Kill switch — registered on every per-agent app. The handler
        # parses the agent name from the command body, so any agent's
        # bolt_app can carry the ``/kill`` for any other agent (which is
        # what the spec asks for: "/kill sam" must work even when sent
        # to Lisa's app).
        slash_prefix = settings.get("SLASH_COMMAND_PREFIX")
        kill_command_names = [f"/{slash_prefix}kill"]
        register_kill_handler(
            bolt_app,
            command_name=kill_command_names,
            active_agent_resolver=_resolve_active_agent_for_kill,
        )

        on_app_mention, on_message = _make_event_handlers(agent_name)
        bolt_app.event("app_mention")(on_app_mention)
        bolt_app.event("message")(on_message)

        _apps_by_agent[agent_name] = bolt_app
        _app_tokens_by_agent[agent_name] = creds["app_token"]
        logger.info("Built Bolt app for agent=%s", agent_name)


_build_apps()


async def _execute_approved_discord_draft(draft: Any, interaction: Any) -> None:
    """Bridge a Discord approval-button click into the transport-agnostic executor.

    Wraps the originating DiscordAdapter in the ``chat_postMessage``-shaped
    ``_DiscordSessionClient`` facade so ``_execute_approved_draft``'s
    lifecycle posts land back in the originating Discord conversation
    without needing a Discord-specific execution path (#682).
    """
    from router.session_lifecycle import _DiscordSessionClient

    conversation_ref = (draft.payload or {}).get("conversation_id") or ""
    adapter = next((a for a in runtime.discord_adapters if a.agent_name == draft.agent_name), None)
    if adapter is None or not conversation_ref:
        logger.warning(
            "No Discord adapter/conversation_ref for approved draft %s; cannot post lifecycle updates",
            draft.draft_id,
        )
        return

    client = _DiscordSessionClient(adapter, conversation_ref)
    await _execute_approved_draft(draft, "", "", client)


def _build_discord_adapters(tasks_store: Any = None) -> list:
    """Build one DiscordAdapter per agent with Discord credentials, gated on DISCORD_ENABLED.

    Returns an empty list when DISCORD_ENABLED is unset/false, leaving the Slack
    path byte-for-byte unchanged.  When enabled, one DiscordAdapter is constructed
    per agent that has Discord credentials; the DiscordApprovalAdapter is also
    instantiated for card rendering. Each adapter also gets the approval
    interaction handler registered (#682) so an Approve/Deny button click
    round-trips through ``on_approval()`` the same way the Slack action
    handler does.

    ``tasks_store`` is the shared ScheduledTaskStore so ``aidt tasks list``
    works over Discord (parity with the Slack ``/<agent>-tasks`` command).
    """
    if not settings.get("DISCORD_ENABLED"):
        logger.info("DISCORD_ENABLED not set; Discord path skipped")
        return []

    from router.approvals.adapters.discord import DiscordApprovalAdapter
    from router.approvals.discord_handlers import register_handlers as register_discord_approval_handlers
    from router.chat.adapters.discord import DiscordAdapter

    agent_map = config.get("agent_map", {})
    discord_creds = load_discord_credentials(agent_map)
    if not discord_creds:
        logger.warning("DISCORD_ENABLED=true but no agents have Discord credentials; Discord inactive")
        return []

    # Instantiate the approval adapter so it is ready for card rendering on the
    # Discord path (parked in a local so the reference survives module reload in tests).
    _discord_approval_adapter = DiscordApprovalAdapter()  # noqa: F841

    adapters = []
    for agent_name, creds in discord_creds.items():
        adapter = DiscordAdapter(
            bot_token=creds["bot_token"],
            agent_name=agent_name,
            default_channel_id=creds.get("default_channel_id", 0),
            tasks_store=tasks_store,
        )
        register_discord_approval_handlers(
            adapter,
            _draft_store,
            execute_callback=_execute_approved_discord_draft,
        )
        adapters.append(adapter)
        logger.info("Built Discord adapter for agent=%s", agent_name)

    return adapters


def _system_task_client(agent_name: str) -> Any | None:
    """Resolve the Slack client for a system (dispatch-supervision) task (#270).

    Dispatch-supervision posts report *on a dispatch*, so they speak as the
    workers bot. Prefer the workers client; fall back to the task owner's agent
    client when no ``WORKERS_BOT_TOKEN`` is configured (today's behaviour). The
    scheduler routes only system tasks through this resolver — agent (cron)
    tasks keep posting as their own agent.
    """
    return _workers_client() or _client_for_agent(agent_name)


async def _validate_channel_for_config(channel_id: str) -> str | None:
    """Resolve a channel ID against Slack for the config page (#576).

    Returns an error string when Slack rejects the ID (typo'd channel caught
    at save time instead of failing every tick), or None when the ID resolves
    — or when no Slack client is available yet, so config edits are never
    blocked by a Slack outage.
    """
    for agent_name in _apps_by_agent:
        client = _client_for_agent(agent_name)
        if client is None:
            continue
        try:
            resp = await client.conversations_info(channel=channel_id)
        except Exception as exc:
            error = getattr(getattr(exc, "response", None), "data", None)
            if isinstance(error, dict) and error.get("error"):
                return f"Slack: {error['error']}"
            logger.warning("config channel validation errored for %s: %s", channel_id, exc)
            return None
        if resp.get("ok"):
            return None
        return f"Slack: {resp.get('error', 'unknown_error')}"
    return None


async def _resolve_workers_bot_user_id() -> str | None:
    """Resolve the workers Slack app's bot user ID via ``auth.test`` (issue #252).

    The workers app (authenticated by ``WORKERS_BOT_TOKEN``) is post-only by
    design — the router never builds a Bolt app for it, it only needs the bot
    user ID so worker posts arriving on each agent's inbound Events stream pass
    the bot-message guard in ``_handle_event`` instead of being silently
    dropped. Whitelisting is the same trusted-bot exception path PR #234
    created for the agent bots seeing each other.

    Returns the ``U…`` user ID on success, or ``None`` for both safe-degradation
    paths — token unset, or ``auth.test`` failing (invalid token / network).
    Neither is a crash: without the seed, worker posts are dropped by the
    agent-side guard, which is exactly today's behaviour.
    """
    workers_token = settings.get("WORKERS_BOT_TOKEN")
    if not workers_token:
        logger.info("workers_bot_token absent from env and secrets.json — skipping worker bot auto-seed")
        return None
    try:
        client = build_web_client(workers_token)
        auth_resp = await client.auth_test()
        return auth_resp["user_id"]
    except SlackApiError as exc:
        error_code = exc.response.get("error", "unknown") if getattr(exc, "response", None) else "unknown"
        logger.warning("Could not resolve workers bot user ID via auth.test: %s", error_code)
        return None
    except Exception as exc:
        logger.warning("Could not resolve workers bot user ID via auth.test: %s", exc)
        return None


async def main():
    """Start the router: run one Socket Mode handler per configured agent."""
    # Fail-fast if the shared internal API token is not set.
    check_token_configured()

    logger.info("Starting router service for %d agent(s)...", len(_apps_by_agent))

    # Start the /healthz HTTP server early so the pull-based deploy
    # daemon can probe us during the initial settle window. The endpoint
    # only flips to 200 once readiness is marked further down — see
    # router/healthz.py for the readiness contract.
    #
    # Port is hardcoded to 8080 inside the container. Compose handles
    # host-side port selection via the HEALTHZ_PORT env var on the host
    # — if the router rebinds to the override, the host→container
    # forward breaks when the two diverge.
    global _healthz_runner
    _healthz_runner = await start_healthz_server(port=8080)

    # Save-time Slack channel resolution for the /config page (#576) — a
    # typo'd channel ID is rejected in the UI instead of erroring per tick.
    config_page.set_channel_validator(_validate_channel_for_config)

    # Wire up the internal API (port 8090, compose-network-only).
    # configure() must be called before start_internal_server() so that
    # request handlers can reach the draft store and per-agent Slack clients.
    # Discord token resolver: maps agent_name → per-agent Discord bot token so
    # Discord-origin approval cards are posted via the already-joined adapter
    # bot (which never 403s), not a separate WORKERS_DISCORD_TOKEN bot (#680).
    _discord_creds_for_api: dict[str, dict] = {}
    try:
        _discord_creds_for_api = load_discord_credentials(config.get("agent_map", {}))
    except ValueError as _exc:
        logger.warning("internal_api: Discord credentials unavailable: %s", _exc)
    configure_internal_api(
        store=_draft_store,
        client_resolver=_client_for_agent,
        discord_token_resolver=lambda _agent: (_discord_creds_for_api.get(_agent) or {}).get("bot_token"),
        discord_adapter_resolver=runtime.discord_adapter_for_agent,
    )
    global _internal_runner
    _internal_runner = await start_internal_server()

    # Load dispatch-bot user ID allowlist from environment. Mutated in place
    # so every alias of the runtime registry observes the update.
    raw_ids = settings.get("DISPATCH_BOT_USER_IDS")
    _dispatch_bot_user_ids.clear()
    _dispatch_bot_user_ids.update(uid.strip() for uid in raw_ids.split(",") if uid.strip())
    if _dispatch_bot_user_ids:
        logger.info("dispatch_bot_user_ids loaded: %s", _dispatch_bot_user_ids)
    else:
        logger.info("DISPATCH_BOT_USER_IDS not set; no bots whitelisted for auto-review")

    # Resolve each agent's bot user ID via auth.test, populate the reverse map.
    for agent_name, bolt_app in _apps_by_agent.items():
        try:
            auth_resp = await bolt_app.client.auth_test()
            bot_user_id = auth_resp["user_id"]
            _bot_user_id_by_agent[agent_name] = bot_user_id
            _bot_user_map[bot_user_id] = agent_name
            logger.info("Bot user ID for agent=%s: %s", agent_name, bot_user_id)
        except Exception:
            logger.warning("Could not resolve bot user ID for agent=%s via auth.test", agent_name)

    # Auto-seed the dispatch-bot allowlist with every resolved agent user ID.
    # The supervisor's auto-review handoff posts via the receiving agent's
    # own bolt client (see router.dispatch.supervision._maybe_fire_auto_review),
    # which means the resulting Slack event arrives with ``user`` = the agent's
    # own bot user ID. Without this seed the bot-message guard would silently
    # drop it. External bots (CI, future machine-user identities) are added via
    # DISPATCH_BOT_USER_IDS above; both sources are merged into the same set.
    _auto_seeded = set(_bot_user_id_by_agent.values())

    # Workers app auto-seed (issue #252). The workers app posts to channels all
    # agent apps are members of; those posts land on every agent's inbound
    # Events stream as bot-authored messages. Resolving its bot user ID and
    # merging it into the same global allowlist makes worker posts pass the
    # bot-message guard as bot-authored-but-trusted — same exception the agent
    # bots get above. Safe-degrades to a no-op when the token is unset or the
    # auth.test fails (see _resolve_workers_bot_user_id).
    workers_bot_id = await _resolve_workers_bot_user_id()
    if workers_bot_id:
        _auto_seeded.add(workers_bot_id)
        logger.info("Built worker bot user id auto-seed: workers_bot_id=%s", workers_bot_id)
    runtime.workers_bot_user_id = workers_bot_id

    _dispatch_bot_user_ids.update(_auto_seeded)
    logger.info(
        "dispatch_bot_user_ids final: %s (env=%d, auto-seeded=%d)",
        _dispatch_bot_user_ids,
        len(_dispatch_bot_user_ids - _auto_seeded),
        len(_auto_seeded),
    )

    # #328: Channel-membership precheck for file-attachment downloads.
    # When ATTACHMENTS_ENABLED, warn at startup for any agent that is not a
    # member of any channels (DMs are fine; url_private downloads require channel
    # membership). Safe-degrades: API errors are swallowed so startup never blocks.
    if attachments_enabled():
        logger.info("attachments: ATTACHMENTS_ENABLED=true — running channel membership precheck")
        for agent_name, bolt_app in _apps_by_agent.items():
            await log_channel_membership_warnings(bolt_app.client, agent_name)
    else:
        logger.info("attachments: ATTACHMENTS_ENABLED not set — file ingest disabled")

    _spawn_background_task(_session_cleanup_loop(), name="session-cleanup-loop")
    _spawn_background_task(_expiration_worker_loop(_draft_store), name="expiration-worker-loop")

    # Register scheduled-task handlers on each agent's Bolt app. Each agent
    # has a unique slash command (``/<prefix><name>-tasks``), but in a dev
    # deployment a single Slack App often hosts every agent's command, and
    # Socket Mode load-balances across whichever sockets that App has open
    # — so a command sent to "Lisa" can land on Sam's bolt_app and vice
    # versa. To stay correct under any routing, we register *every* agent's
    # command on *every* bolt_app, and resolve the target agent from the
    # command body itself. ``SLASH_COMMAND_PREFIX`` lets a dev deployment
    # (e.g. ``dev-`` prefix) coexist with prod in the same workspace.
    slash_prefix = settings.get("SLASH_COMMAND_PREFIX")
    all_agent_names = list(get_agent_map().keys())
    all_command_names = [f"/{slash_prefix}{name}-tasks" for name in all_agent_names]
    suffix = "-tasks"

    def _agent_from_command(body: dict) -> str | None:
        cmd = (body.get("command") or "").lstrip("/")
        if slash_prefix:
            cmd = cmd.removeprefix(slash_prefix)
        if not cmd.endswith(suffix):
            return None
        agent = cmd[: -len(suffix)]
        return agent or None

    # One shared store + scheduler for the whole process. Per-bolt_app
    # schedulers all see the same DB, so spinning up N of them caused every
    # due task to be posted under every bot at once.
    scheduled_tasks_store = open_store()
    set_wakeup_store(scheduled_tasks_store)

    # #327: Register the singleton attachments GC sweep as a router system
    # task. The sweep must run in the router process — the only container
    # with the attachments mount read-write — so it cannot live in the pack
    # handler (RO there). Idempotent across restarts. agent_name only picks
    # the scheduler's client; the sweep posts nothing.
    if all_agent_names:
        try:
            register_attachments_sweep(scheduled_tasks_store, agent_name=all_agent_names[0])
        except Exception:
            logger.exception("Failed to register attachments GC sweep system task")
    else:
        logger.warning("No agents configured; skipping attachments GC sweep registration")

    # #437: Register the singleton idle auto-merge queue task. Idempotent across
    # restarts. Requires MERGE_QUEUE_REPO to be set; silently skips if absent so
    # deployments that don't use auto-merge are unaffected.
    _merge_queue_repo = settings.get("MERGE_QUEUE_REPO")
    if _merge_queue_repo and all_agent_names:
        try:
            import router.merge_queue as _mq  # noqa: PLC0415

            register_merge_queue(
                scheduled_tasks_store,
                agent_name=all_agent_names[0],
                repo=_merge_queue_repo,
                pat_path=settings.get("MERGE_QUEUE_PAT_PATH") or _mq.MERGE_PAT_PATH,
                # Prefer the dedicated merge-queue channel; fall back to the
                # generic scheduled-task destination only if it is unset.
                destination=(settings.get("MERGE_QUEUE_CHANNEL") or settings.get("OPERATOR_DM_CHANNEL") or None),
            )
        except Exception:
            logger.exception("Failed to register idle auto-merge queue system task")
    elif not _merge_queue_repo:
        logger.info("MERGE_QUEUE_REPO not set; skipping idle auto-merge registration")

    # #535: Register the singleton autonomous bug-backlog dispatch loop. Gated
    # on AUTO_DISPATCH_REPO so it stays opt-in at the deploy layer; the runtime
    # ``auto_dispatch.enabled`` config flag (default OFF) + ``shadow_mode``
    # (default ON) are the real kill switches — a registered tick simply
    # no-ops (returns ``skipped: disabled``) until an operator flips the flag.
    # Idempotent across restarts (dedup by callable_ref).
    _auto_dispatch_repo = settings.get("AUTO_DISPATCH_REPO")
    if _auto_dispatch_repo and all_agent_names:
        try:
            import router.auto_dispatch as _ad  # noqa: PLC0415

            _ad.register_auto_dispatch(
                scheduled_tasks_store,
                agent_name=all_agent_names[0],
                repo=_auto_dispatch_repo,
                pat_path=settings.get("AUTO_DISPATCH_PAT_PATH") or _ad.MERGE_PAT_PATH,
                destination=(settings.get("AUTO_DISPATCH_CHANNEL") or settings.get("OPERATOR_DM_CHANNEL") or None),
            )
        except Exception:
            logger.exception("Failed to register autonomous bug-backlog dispatch system task")
    elif not _auto_dispatch_repo:
        logger.info("AUTO_DISPATCH_REPO not set; skipping autonomous bug-backlog dispatch registration")

    # #755: Register the epic orchestrator loop (Stage 1). Gated on
    # config/epic.yaml actually naming a repo + at least one tracked epic —
    # the runtime EPIC_ORCHESTRATOR flag (default OFF, hot-reload) is the
    # real kill switch a registered tick checks first and no-ops on.
    # Idempotent across restarts (dedup by callable_ref).
    try:
        import router.epic as _epic  # noqa: PLC0415

        _epic_cfg = _epic.load_epic_config()
        if _epic_cfg["repo"] and _epic_cfg["epics"] and all_agent_names:
            _epic.register_epic_orchestrator(
                scheduled_tasks_store,
                agent_name=all_agent_names[0],
                destination=(settings.get("OPERATOR_DM_CHANNEL") or None),
            )
        else:
            logger.info("config/epic.yaml has no repo/epics configured; skipping epic orchestrator registration")
    except Exception:
        logger.exception("Failed to register epic orchestrator system task")

    for agent_name, bolt_app in _apps_by_agent.items():
        setup_scheduled_tasks_handlers(
            bolt_app=bolt_app,
            store=scheduled_tasks_store,
            agent_resolver=_agent_from_command,
            command_name=all_command_names,
        )
        logger.info(
            "Registered scheduled-task commands %s on bolt_app for agent=%s",
            all_command_names,
            agent_name,
        )

    # Resolve a per-task Slack client off ``_apps_by_agent`` so each task
    # posts under its own bot, regardless of which agent's app the scheduler
    # was wired to.
    def _client_for_scheduled_task(agent_name: str) -> Any | None:
        app = _apps_by_agent.get(agent_name)
        return app.client if app is not None else None

    scheduler_task = start_scheduled_tasks_scheduler(
        store=scheduled_tasks_store,
        client_resolver=_client_for_scheduled_task,
        dispatch_fn=dispatch,
        # Issue #270: dispatch-supervision (system) tasks post as the workers
        # bot so runtime status lines share one identity; agent cron tasks keep
        # using _client_for_scheduled_task above.
        system_client_resolver=_system_task_client,
    )
    # Park the scheduler task so asyncio's weak-ref bookkeeping can't drop
    # it. Without this the loop can be GC'd mid-flight and scheduled tasks
    # silently stop firing.
    _background_tasks.add(scheduler_task)
    scheduler_task.add_done_callback(_background_tasks.discard)

    # Dispatch discovery — the router-side reconciler that turns a
    # launched-but-unsupervised dispatch dir (handler exited, babysit
    # is running) into a supervision system task. See
    # router/dispatch/discovery.py for why this lives separately from
    # the scheduler. Default-on; the actual supervision still respects
    # ``DISPATCH_SUPERVISION=inline`` from the pack handler side
    # (inline-mode handlers never write a dispatch dir, so discovery
    # finds nothing to register).
    discovery_task = start_discovery_loop(
        scheduled_tasks_store,
        agent_user_id_resolver=_bot_user_id_by_agent.get,
    )
    _background_tasks.add(discovery_task)
    discovery_task.add_done_callback(_background_tasks.discard)

    if not _app_tokens_by_agent:
        logger.error("No agents have Slack credentials; nothing to start")
        return

    # Build Socket Mode handlers now that the event loop is running, then
    # start them all concurrently.
    handlers = [
        AsyncSocketModeHandler(_apps_by_agent[agent_name], app_token)
        for agent_name, app_token in _app_tokens_by_agent.items()
    ]

    # Build Discord adapters behind DISCORD_ENABLED; the runtime registry
    # holds strong references so asyncio's weak-ref bookkeeping can't drop
    # them during the gather (updated in place — see router/runtime.py).
    _discord_adapters[:] = _build_discord_adapters(tasks_store=scheduled_tasks_store)

    # Startup probe: verify the dispatch workspace root is mounted and writable
    # before we declare ourselves ready.  A missing or root-owned mount causes
    # every dispatch execution to fail with PermissionError (see issue #691);
    # logging here means operators see it on boot rather than at first dispatch.
    if not workspace_volume_writable():
        from router.dispatch.state import dispatch_root

        logger.error(
            "workspace_volume_writable FAILED — dispatch workspace root %s is not writable "
            "(set via DISPATCH_WORKSPACE_ROOT, default /var/lib/dispatch). "
            "All dispatch executions will fail. See issue #691.",
            dispatch_root(),
        )

    # We're now fully wired: auth.test succeeded for each agent, the
    # session-cleanup and scheduled-task loops are running, and we're
    # about to hand off to Socket Mode. From the CD daemon's point of
    # view, the service has reached its "ready" state.
    mark_ready()

    await asyncio.gather(
        *(handler.start_async() for handler in handlers),
        *(adapter.start() for adapter in _discord_adapters),
    )


__all__ = [
    "DEFAULT_THINKING_STATUS",
    "_agent_owns_dispatch_thread",
    "_execute_approved_draft",
    "_handle_event",
    "_is_dispatch_bot_sender",
    "_make_event_handlers",
    "_seen_events",
    "_session_cleanup_loop",
    "_session_end_client",
    "handle_app_mention",
    "handle_message",
    "main",
    "set_assistant_status",
]


if __name__ == "__main__":
    asyncio.run(main())
