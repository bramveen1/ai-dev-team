"""Runtime settings — one registry, one hot-reloadable store (#576).

Replaces the ".env → docker-compose environment → container recreate" loop
for every value that does not genuinely need to be baked in at boot.

Three tiers, by lifecycle:

* ``var``    — plain configuration (channels, toggles, limits). Stored in
  ``config/runtime.json`` (bind-mounted into the router container), re-read
  on a short TTL so edits apply **without a container restart** for settings
  whose call sites read per-use (``reload="hot"``). Settings only consumed
  during startup are marked ``reload="restart"`` — a plain ``docker restart
  router`` (no recreate, no rebuild) picks up the new value because the file
  is re-read at boot.
* ``secret`` — tokens. Stored in ``data/secrets.json`` via
  :class:`router.packs.secret_store.SecretStore` (atomic writes, re-read on
  every access — already hot). Never stored in ``config/`` because that
  directory is mounted into agent containers.
* ``boot``   — values that exist only as process environment (Slack app
  credentials, auth mode for agent containers). Read-only in the config UI;
  changing them still requires editing ``.env`` and recreating containers.

Precedence (decided with the operator, 2026-07): **store wins over env**.
A key present in ``runtime.json`` (or the secret store) overrides the
environment; the env var remains a fallback for unset keys so existing
deployments keep working unchanged. Empty env values (compose renders
``VAR=${VAR:-}`` which yields ``""``) are treated as unset.

Failure modes (from #576):

* Malformed / mid-edit ``runtime.json`` → keep the last-good parse, log
  loudly, never crash a tick.
* A value in the file that fails type coercion → warn and fall back to
  env / default for that key only.
* Writes are atomic (tmp + rename), matching :class:`SecretStore`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from router.atomic_io import atomic_write_json
from router.packs.secret_store import SecretStore

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Container mount (compose maps ./config → /config); repo fallback for
# CI / local dev, mirroring router.config.DEFAULT_AGENTS_DIR's pattern.
MOUNTED_CONFIG_DIR = Path("/config")

DEFAULT_TTL_SECONDS = 15.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})

# Slack conversation IDs: C… channels, D… DMs, G… groups.
_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{5,}$")

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def default_runtime_path() -> Path:
    """Return the runtime.json path: the /config mount when present, else the repo copy."""
    if MOUNTED_CONFIG_DIR.is_dir():
        return MOUNTED_CONFIG_DIR / "runtime.json"
    return REPO_ROOT / "config" / "runtime.json"


@dataclass(frozen=True, kw_only=True)
class Setting:
    """One entry in the registry — the single place a setting is defined."""

    key: str
    kind: str  # "var" | "secret" | "boot"
    type: str  # "str" | "int" | "bool" | "channel"
    default: Any
    description: str
    reload: str  # "hot" | "restart"
    category: str
    choices: tuple[str, ...] = ()
    sensitive: bool = False  # masked in the API/UI (secrets are always sensitive)
    secret_key: str = ""  # SecretStore top-level key (kind == "secret" only)
    min_value: int | None = None  # ints: reject values below this on set()
    max_value: int | None = None  # ints: reject values above this on set()
    # Legacy key names still honoured on read (file and env layers; canonical
    # name wins within a layer). set() writes only the canonical key and drops
    # alias keys from runtime.json, so stores self-migrate on first save.
    aliases: tuple[str, ...] = ()

    @property
    def is_sensitive(self) -> bool:
        return self.sensitive or self.kind == "secret"


_REGISTRY_ENTRIES: tuple[Setting, ...] = (
    # ── Dispatch & notifications ─────────────────────────────────────────
    Setting(
        key="AUTO_DISPATCH_CHANNEL",
        kind="var",
        type="channel",
        default="",
        description="Slack channel for autonomous bug-backlog dispatch status posts. Resolved every tick.",
        reload="hot",
        category="Dispatch",
    ),
    Setting(
        key="AUTO_DISPATCH_REPO",
        kind="var",
        type="str",
        default="",
        description="owner/repo for the autonomous dispatch loop. The loop is registered at boot, so "
        "changing this needs a router restart.",
        reload="restart",
        category="Dispatch",
    ),
    Setting(
        key="AUTO_DISPATCH_PAT_PATH",
        kind="var",
        type="str",
        default="",
        description="Path (inside the router container) to the GitHub PAT used by auto-dispatch. "
        "Empty → /config/secrets/gh-aidt-merge.token.",
        reload="restart",
        category="Dispatch",
    ),
    Setting(
        key="MERGE_QUEUE_CHANNEL",
        kind="var",
        type="channel",
        default="",
        description="Slack channel for idle auto-merge queue status posts. Resolved every tick.",
        reload="hot",
        category="Merge queue",
    ),
    Setting(
        key="MERGE_QUEUE_TRANSPORT",
        kind="var",
        type="str",
        default="",
        description="Non-Slack transport (currently only 'discord') for merge-queue status posts when "
        "MERGE_QUEUE_STATUS_VIA_CHAT_ADAPTER is on. Empty or 'slack' keeps the legacy Slack path.",
        reload="hot",
        category="Merge queue",
    ),
    Setting(
        key="MERGE_QUEUE_CONVERSATION_REF",
        kind="var",
        type="str",
        default="",
        description="Stored ChatAdapter conversation_ref merge-queue status posts are sent to when "
        "MERGE_QUEUE_STATUS_VIA_CHAT_ADAPTER is on and MERGE_QUEUE_TRANSPORT is a supported non-Slack "
        "transport. Empty → legacy Slack path (no adapter fallback).",
        reload="hot",
        category="Merge queue",
    ),
    Setting(
        key="MERGE_QUEUE_REPO",
        kind="var",
        type="str",
        default="",
        description="owner/repo for the idle auto-merge queue. Registered at boot → restart to change.",
        reload="restart",
        category="Merge queue",
    ),
    Setting(
        key="MERGE_QUEUE_PAT_PATH",
        kind="var",
        type="str",
        default="",
        description="Path to the GitHub PAT used by the merge queue. Empty → /config/secrets/gh-aidt-merge.token.",
        reload="restart",
        category="Merge queue",
    ),
    Setting(
        key="CONTINUOUS_MERGE",
        kind="var",
        type="bool",
        default=False,
        description="Master flag for the continuous merge daemon (#832). When on, each merge-queue tick "
        "partitions ALL open PRs independently into auto-merge / auto-rebase / digest-ping buckets instead "
        "of the legacy single-PR-per-tick idle auto-merge, so a blocked PR never stalls an eligible one. "
        "Off = unchanged legacy behavior.",
        reload="hot",
        category="Merge queue",
    ),
    Setting(
        key="CONTINUOUS_MERGE_DRY_RUN",
        kind="var",
        type="bool",
        default=True,
        description="Shadow/dry-run gate for CONTINUOUS_MERGE (#832), independent of legacy merge-queue "
        "behavior. Defaults to True so the *first* flip of CONTINUOUS_MERGE runs shadow-first: intended "
        "merge/rebase/digest actions are logged only, nothing is merged or posted to Slack. Flip to False "
        "after verifying a few ticks.",
        reload="hot",
        category="Merge queue",
    ),
    Setting(
        key="AUTO_DISPATCH_WORKER_AGENT",
        kind="var",
        type="str",
        default="",
        description="Agent that runs auto-dispatched work. Empty → the agent whose manifest declares "
        "dispatch_workspace: true.",
        reload="hot",
        category="Dispatch",
    ),
    Setting(
        key="AUTO_DISPATCH_APPROVERS",
        kind="var",
        type="str",
        default="",
        description="Comma-separated GitHub logins whose 'verdict: pass/fail' PR comments count. "
        "EMPTY = all verdicts ignored (fail-safe) — set this to enable the verdict gate.",
        reload="hot",
        category="Dispatch",
    ),
    Setting(
        key="DEFAULT_AGENT",
        kind="var",
        type="str",
        default="",
        description="Fallback agent for un-mentioned chat messages and the terminal console. "
        "Empty → first discovered agent (alphabetical).",
        reload="hot",
        category="Router",
    ),
    Setting(
        key="OPERATOR_DM_CHANNEL",
        kind="var",
        type="channel",
        default="",
        description="Fallback destination for scheduled-task and dispatch notifications when no dedicated "
        "channel is set. (Renamed from BRAM_DM_CHANNEL — the old key keeps working via alias.)",
        reload="hot",
        category="Dispatch",
        aliases=("BRAM_DM_CHANNEL",),
    ),
    # ── Feature toggles ──────────────────────────────────────────────────
    Setting(
        key="WORKER_MENTION_HANDOFF",
        kind="var",
        type="bool",
        default=False,
        description="Allow workers-bot @mentions through the bot-message guard (one wake per completion post).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="DISPATCH_MILESTONE_FEED",
        kind="var",
        type="bool",
        default=True,
        description="Post dispatch milestone updates into the originating thread.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="ATTACHMENTS_ENABLED",
        kind="var",
        type="bool",
        default=True,
        description="Ingest Slack file attachments into agent context.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="MEMORY_RETRIEVAL_ENABLED",
        kind="var",
        type="bool",
        default=False,
        description="Enable keyword memory retrieval when building agent context.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="AUDIO_INGEST_ENABLED",
        kind="var",
        type="bool",
        default=False,
        description="Transcribe audio attachments (voice notes) to a .txt sidecar via OpenAI Whisper "
        "before they reach an agent. Requires the openai_whisper_key secret. Default off (#804).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="DISCORD_ENABLED",
        kind="var",
        type="bool",
        default=False,
        description="Start the Discord gateway path. Evaluated at boot → restart to change.",
        reload="restart",
        category="Features",
    ),
    Setting(
        key="SLACK_VIA_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description=(
            "Route Slack events through the transport-neutral ChatAdapter pipeline "
            "(#553). Off = legacy dispatch path. Read per event — hot."
        ),
        reload="hot",
        category="Features",
    ),
    Setting(
        key="SLACK_OUTBOUND_VIA_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description=(
            "Route plain-text outbound/proactive Slack sends (scheduled tasks, approval "
            "notices, reminders) through ChatAdapter.send_message instead of calling "
            "chat_postMessage directly (#801, deferred outbound slice of #553). Off = "
            "legacy dispatch path. Read per send — hot. Rich sends (Block Kit, chat_update, "
            "metadata) are unaffected either way."
        ),
        reload="hot",
        category="Features",
    ),
    Setting(
        key="DISCORD_WORKER_STATUS_VIA_AGENT",
        kind="var",
        type="bool",
        default=False,
        description="Post Discord worker status through the dispatching agent's adapter identity "
        "(router callback) instead of a separate WORKERS_DISCORD_TOKEN bot (#707).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="DISPATCH_FEED_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description="Route milestone_feed and supervision posts through a ChatAdapter resolved "
        "from the dispatch's conversation_ref instead of calling slack_post directly. Mirrors "
        "DISCORD_WORKER_STATUS_VIA_AGENT (#707); Slack path is unaffected either way (#713).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="KILL_COMMAND_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description="Route the /kill command's threaded kill notice through a ChatAdapter "
        "resolved from the command's conversation_ref instead of calling slack_post directly. "
        "Mirrors DISPATCH_FEED_VIA_CHAT_ADAPTER (#713); Slack path is unaffected either way (#827).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="AUTO_DISPATCH_NOTIFY_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description="Route the auto-dispatch loop's status notices through a ChatAdapter resolved "
        "from the notice's agent/transport/conversation_id instead of calling slack_post directly. "
        "Mirrors DISPATCH_FEED_VIA_CHAT_ADAPTER (#713); Slack path is unaffected either way (#837).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="MERGE_QUEUE_STATUS_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description="Route the merge-queue daemon's status posts through a ChatAdapter resolved from "
        "the stored MERGE_QUEUE_TRANSPORT/MERGE_QUEUE_CONVERSATION_REF settings instead of calling "
        "slack_post directly. Mirrors DISPATCH_FEED_VIA_CHAT_ADAPTER (#713); Slack path is unaffected "
        "either way (#838).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="DISPATCHER_STATUS_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description="Route the dispatcher's stuck-guard notification through a ChatAdapter "
        "resolved from the dispatch's conversation_ref instead of calling slack_post directly. "
        "Mirrors DISPATCH_FEED_VIA_CHAT_ADAPTER (#713); Slack path is unaffected either way (#839).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="WORKERS_CLIENT_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=True,
        description="Historical rollout flag for routing runtime.workers_client() outbound resolution "
        "through a ChatAdapter when called with a non-Slack transport/conversation_ref instead of "
        "constructing a raw Slack AsyncWebClient. Default-on and unconditional since #862 — "
        "router/runtime.py no longer reads this key; a resolvable non-Slack transport always prefers "
        "the adapter, and Slack/no-argument call sites stay on the legacy AsyncWebClient construction "
        "permanently. Mirrors DISPATCH_FEED_VIA_CHAT_ADAPTER (#713).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="APP_LIFECYCLE_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description="Gate app.py's _resolve_workers_bot_user_id() auth.test lookup behind transport "
        "awareness: a non-Slack transport skips the raw Slack AsyncWebClient construction instead of "
        "attempting a Slack-only lookup with no ChatAdapter equivalent. Mirrors "
        "WORKERS_CLIENT_VIA_CHAT_ADAPTER (#841); Slack path and today's no-argument call site are "
        "unaffected either way (#842).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="CHAT_BACKENDS",
        kind="var",
        type="bool",
        default=False,
        description="Enable the multi-backend chat abstraction. Evaluated at import → restart to change.",
        reload="restart",
        category="Features",
    ),
    Setting(
        key="CONFIG_CONTAINER_RESTART_ENABLED",
        kind="var",
        type="bool",
        default=False,
        description="Allow the /config page's per-container Restart buttons (and router self-restart) "
        "to actually restart containers. Refused when unset/false (#710).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_ORCHESTRATOR",
        kind="var",
        type="bool",
        default=False,
        description="Master flag for the epic orchestrator loop (#751/#755). Stage 1: walk a configured "
        "epic's sub-issue DAG (config/epic.yaml) and post a per-issue approval card for each ready child — "
        "dispatch, merge, and deploy stay human. Off = the tick no-ops; zero change to the bug loop.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_AUTO_DISPATCH",
        kind="var",
        type="bool",
        default=False,
        description="Stage 2 (#756) of the epic orchestrator: requires EPIC_ORCHESTRATOR on. When on, "
        "each DAG-ready sub-issue is auto-dispatched straight to the worker (no per-dispatch approval "
        "card) instead of Stage 1's approval card, still subject to the bug loop's existing 12/day + "
        "1/hr dispatch caps (config/dispatch.yaml auto_dispatch:) and the $50/5h spend cap. Merge and "
        "deploy stay human. Off = Stage 1 behavior (approval card per dispatch).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_SHADOW_MODE",
        kind="var",
        type="bool",
        default=True,
        description="#773: shadow/dry-run gate for EPIC_AUTO_DISPATCH (Stage 2), independent of the bug "
        "loop's own config/dispatch.yaml auto_dispatch.shadow_mode (which may already be flipped live). "
        "Defaults to True so the *first* flip of EPIC_AUTO_DISPATCH runs shadow-first: a dispatch-eligible "
        "sub-issue is logged as 'would dispatch' and no worker is spawned, no counter incremented. Flip to "
        "False for real Stage 2 launches. No effect on merge/deploy, which stay human either way.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_AUTO_MERGE",
        kind="var",
        type="bool",
        default=False,
        description="Stage 3 (#757) of the epic orchestrator: requires EPIC_ORCHESTRATOR on. When on, "
        "a landed epic sub-issue PR that is reviewed (non-author approval), green "
        "(mergeable_state == 'clean'), and DAG-satisfied (every parent already merged) gets the "
        "epic-auto-merge label applied, which lifts #753's merge-gate exclusion so merge_queue "
        "merges it like any other approved PR. Still subject to EPIC_SHADOW_MODE: while that's on, "
        "an eligible PR is only logged as 'would apply epic-auto-merge', never labelled. Off = "
        "landed PRs keep the plain epic:<slug> label only and stay excluded from auto-merge. Deploy "
        "stays human (#758) either way.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_AUTO_DEPLOY",
        kind="var",
        type="bool",
        default=False,
        description="Stage 4 (#758) of the epic orchestrator: the last human checkpoint. Deploy is "
        "already fully automatic for every merge to main via the pull daemon (scripts/deploy-pull.sh: "
        "health-check + auto-revert), unchanged by this flag either way. Off (default) = when "
        "epic-auto-merge is applied to a landed epic PR, the notification asks Bram to watch/approve "
        "the resulting deploy. On = the notification says monitor-only — Bram is no longer expected to "
        "act. Purely a notification-posture toggle; reversible by one config line.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_STATUS_TRANSPORT",
        kind="var",
        type="str",
        default="",
        description="Non-Slack transport (currently only 'discord') for epic-orchestrator status posts "
        "when EPIC_STATUS_VIA_CHAT_ADAPTER is on. Empty or 'slack' keeps the legacy Slack path.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_STATUS_CONVERSATION_REF",
        kind="var",
        type="str",
        default="",
        description="Stored ChatAdapter conversation_ref epic-orchestrator status posts are sent to when "
        "EPIC_STATUS_VIA_CHAT_ADAPTER is on and EPIC_STATUS_TRANSPORT is a supported non-Slack transport. "
        "Empty → legacy Slack path (no adapter fallback).",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="EPIC_STATUS_VIA_CHAT_ADAPTER",
        kind="var",
        type="bool",
        default=False,
        description="Route the epic orchestrator's own status posts (DAG-cycle warning, kickoff card, "
        "deploy-posture notice) through a ChatAdapter resolved from the stored "
        "EPIC_STATUS_TRANSPORT/EPIC_STATUS_CONVERSATION_REF settings instead of calling slack_post "
        "directly. Mirrors DISPATCH_FEED_VIA_CHAT_ADAPTER (#713); Slack path is unaffected either way "
        "(#840). Orchestrator dispatch/DAG/merge-gate logic is untouched — notification routing only.",
        reload="hot",
        category="Features",
    ),
    Setting(
        key="SLASH_COMMAND_PREFIX",
        kind="var",
        type="str",
        default="",
        description="Prefix for registered Slack slash commands (e.g. 'dev-'). Handlers register at boot.",
        reload="restart",
        category="Features",
    ),
    Setting(
        key="DISPATCH_BOT_USER_IDS",
        kind="var",
        type="str",
        default="",
        description="Comma-separated Slack bot user IDs whose posts may trigger dispatch handoff.",
        reload="restart",
        category="Features",
    ),
    Setting(
        key="DISCORD_DISPATCH_BOT_IDS",
        kind="var",
        type="str",
        default="",
        description="Comma-separated Discord bot snowflakes allowed to trigger agent turns.",
        reload="hot",
        category="Features",
    ),
    # ── Router limits ────────────────────────────────────────────────────
    Setting(
        key="SESSION_TIMEOUT",
        kind="var",
        type="int",
        default=600,
        description="Idle session timeout in seconds (routing, cleanup, and idle detection share this).",
        reload="hot",
        category="Router",
        min_value=1,
    ),
    Setting(
        key="MAX_CONTEXT_TOKENS",
        kind="var",
        type="int",
        default=32000,
        description="Token budget for context assembly at dispatch time.",
        reload="hot",
        category="Router",
        min_value=1,
    ),
    Setting(
        key="LOG_LEVEL",
        kind="var",
        type="str",
        default="INFO",
        description="Router log level. Applied when logging is configured at boot.",
        reload="restart",
        category="Router",
        choices=VALID_LOG_LEVELS,
    ),
    # ── Stuck guard ──────────────────────────────────────────────────────
    Setting(
        key="STUCK_GUARD_MODE",
        kind="var",
        type="str",
        default="dry-run",
        description="dry-run: log and post trips only. enforce: halt the task on trip.",
        reload="restart",
        category="Stuck guard",
        choices=("dry-run", "enforce"),
    ),
    Setting(
        key="STUCK_GUARD_TURN_CAP",
        kind="var",
        type="int",
        default=50,
        description="Max agent turns per task before the guard trips.",
        reload="restart",
        category="Stuck guard",
        min_value=1,
    ),
    Setting(
        key="STUCK_GUARD_TURN_CAP_WINDOW",
        kind="var",
        type="int",
        default=3600,
        description="Rolling window (seconds) for turn-cap rate measurement. Only turns within this window "
        "count toward the cap.",
        reload="restart",
        category="Stuck guard",
        min_value=1,
    ),
    Setting(
        key="STUCK_GUARD_LOOP_WINDOW",
        kind="var",
        type="int",
        default=5,
        description="Sliding window (turns) for repeated-action loop detection.",
        reload="restart",
        category="Stuck guard",
        min_value=1,
    ),
    Setting(
        key="STUCK_GUARD_LOOP_THRESHOLD",
        kind="var",
        type="int",
        default=3,
        description="Identical actions within the window that count as a loop.",
        reload="restart",
        category="Stuck guard",
        min_value=1,
    ),
    Setting(
        key="STUCK_GUARD_ERROR_STREAK",
        kind="var",
        type="int",
        default=3,
        description="Consecutive CLI failures before the guard trips.",
        reload="restart",
        category="Stuck guard",
        min_value=1,
    ),
    Setting(
        key="STUCK_GUARD_POST_MORTEM_DIR",
        kind="var",
        type="str",
        default="/config/shared/stuck-tasks",
        description="Directory where stuck-task post-mortems are written.",
        reload="restart",
        category="Stuck guard",
    ),
    Setting(
        key="STUCK_GUARD_MAX_TURNS_STORED",
        kind="var",
        type="int",
        default=200,
        description="Max per-task turn records kept in memory.",
        reload="restart",
        category="Stuck guard",
        min_value=1,
    ),
    # ── Secrets (stored in data/secrets.json — router-only mount) ────────
    Setting(
        key="WORKERS_BOT_TOKEN",
        kind="secret",
        type="str",
        default="",
        description="Slack workers bot token (xoxb-…) — posts worker status back to Slack threads. Read per dispatch.",
        reload="hot",
        category="Secrets",
        secret_key="workers_bot_token",
    ),
    Setting(
        key="WORKERS_DISCORD_TOKEN",
        kind="secret",
        type="str",
        default="",
        description="Discord workers bot token — posts worker status to Discord threads. Read per dispatch.",
        reload="hot",
        category="Secrets",
        secret_key="workers_discord_token",
    ),
    Setting(
        key="OPENAI_WHISPER_KEY",
        kind="secret",
        type="str",
        default="",
        description="OpenAI API key used for Whisper audio transcription (#804). Read per attachment; "
        "falls back to the OPENAI_WHISPER_KEY env var when unset in the secret store.",
        reload="hot",
        category="Secrets",
        secret_key="openai_whisper_key",
    ),
    # ── Boot environment (read-only in the config UI) ────────────────────
    Setting(
        key="ROUTER_INTERNAL_TOKEN",
        kind="boot",
        type="str",
        default="",
        description="Shared bearer token for the internal dispatch API. Required at boot.",
        reload="restart",
        category="Boot environment",
        sensitive=True,
    ),
    Setting(
        key="CLAUDE_AUTH_MODE",
        kind="boot",
        type="str",
        default="credentials",
        description="Claude CLI auth mode for agent containers (credentials | oauth_token | api_key). Baked into "
        "agent container env at create.",
        reload="restart",
        category="Boot environment",
    ),
    Setting(
        key="ANTHROPIC_API_KEY",
        kind="boot",
        type="str",
        default="",
        description="Metered API key for agent containers (api_key mode only).",
        reload="restart",
        category="Boot environment",
        sensitive=True,
    ),
    Setting(
        key="CLAUDE_CODE_OAUTH_TOKEN",
        kind="boot",
        type="str",
        default="",
        description="Long-lived OAuth token for agent containers (oauth_token mode only).",
        reload="restart",
        category="Boot environment",
        sensitive=True,
    ),
    Setting(
        key="HEALTHZ_PORT",
        kind="boot",
        type="str",
        default="8080",
        description="Host port the health/config server is published on (127.0.0.1 only).",
        reload="restart",
        category="Boot environment",
    ),
    Setting(
        key="SCHEDULED_TASKS_DB",
        kind="boot",
        type="str",
        default="data/scheduled_tasks.db",
        description="SQLite path for the scheduled-tasks store. Opened at boot.",
        reload="restart",
        category="Boot environment",
    ),
)

REGISTRY: dict[str, Setting] = {s.key: s for s in _REGISTRY_ENTRIES}


def _coerce(entry: Setting, raw: Any, origin: str) -> Any:
    """Coerce ``raw`` (from file, env, or API) to the entry's declared type.

    Raises :class:`ValueError` with a caller-facing message on any mismatch —
    read paths catch it and fall back; the write path surfaces it to the UI.
    """
    if entry.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in _TRUTHY:
                return True
            if low in _FALSY:
                return False
        raise ValueError(f"{entry.key}: expected a boolean, got {raw!r} ({origin})")

    if entry.type == "int":
        if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
            raise ValueError(f"{entry.key}: expected an integer, got {raw!r} ({origin})")
        try:
            value = int(str(raw).strip())
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{entry.key}: expected an integer, got {raw!r} ({origin})") from exc
        if entry.min_value is not None and value < entry.min_value:
            raise ValueError(f"{entry.key}: must be >= {entry.min_value}, got {value} ({origin})")
        if entry.max_value is not None and value > entry.max_value:
            raise ValueError(f"{entry.key}: must be <= {entry.max_value}, got {value} ({origin})")
        return value

    # "str" and "channel" are both strings at rest.
    if not isinstance(raw, str):
        raise ValueError(f"{entry.key}: expected a string, got {type(raw).__name__} ({origin})")
    return raw


def validate_for_write(entry: Setting, raw: Any) -> Any:
    """Full validation used by the write path (API / :meth:`RuntimeSettings.set`).

    Applies type coercion plus the stricter checks we only want on writes:
    channel-ID format and ``choices`` membership. Empty strings are always
    accepted (they mean "explicitly cleared").
    """
    value = _coerce(entry, raw, "submitted value")
    if isinstance(value, str) and value == "":
        return value
    if entry.type == "channel" and not _CHANNEL_ID_RE.match(value):
        raise ValueError(
            f"{entry.key}: {value!r} does not look like a Slack conversation ID (C…/D…/G… + uppercase alphanumerics)"
        )
    if entry.choices and value not in entry.choices:
        raise ValueError(f"{entry.key}: must be one of {', '.join(entry.choices)} (got {value!r})")
    return value


class RuntimeSettings:
    """TTL-cached view over ``runtime.json`` + the secret store + the environment."""

    def __init__(
        self,
        path: Path | None = None,
        ttl: float = DEFAULT_TTL_SECONDS,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.path = path if path is not None else default_runtime_path()
        self.ttl = ttl
        self._secret_store = secret_store if secret_store is not None else SecretStore()
        self._cache: dict[str, Any] = {}
        self._cache_read_at: float | None = None
        self._warned_conflicts: set[str] = set()

    # ── file layer ────────────────────────────────────────────────────────

    def _read_file(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._cache_read_at is not None and (now - self._cache_read_at) < self.ttl:
            return self._cache

        try:
            with open(self.path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"{self.path} root must be a JSON object")
        except FileNotFoundError:
            data = {}
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            # #576 failure mode: malformed / mid-edit file → keep last-good.
            logger.error("runtime config %s unreadable (%s); keeping last-good values", self.path, exc)
            self._cache_read_at = now
            return self._cache

        self._cache = data
        self._cache_read_at = now
        return data

    def _write_file(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data, indent=2, sort_keys=True)
        self._cache = data
        self._cache_read_at = time.monotonic()

    # ── read path ─────────────────────────────────────────────────────────

    def _env_raw(self, key: str) -> str | None:
        """Env value, with '' (compose's ``${VAR:-}`` artifact) treated as unset."""
        raw = os.environ.get(key)
        return raw if raw not in (None, "") else None

    def _env_lookup(self, entry: Setting) -> str | None:
        """Env value for the canonical key, falling back to legacy aliases."""
        for name in (entry.key, *entry.aliases):
            raw = self._env_raw(name)
            if raw is not None:
                return raw
        return None

    @staticmethod
    def _file_key(entry: Setting, data: dict[str, Any]) -> str | None:
        """The key under which ``entry`` is stored in the file (canonical wins over aliases)."""
        for name in (entry.key, *entry.aliases):
            if name in data:
                return name
        return None

    def _warn_conflict_once(self, key: str, winner: str) -> None:
        if key not in self._warned_conflicts:
            self._warned_conflicts.add(key)
            logger.warning(
                "Setting %s is defined in both the %s and the environment; the %s value wins (store-over-env)",
                key,
                winner,
                winner,
            )

    def _resolve(self, entry: Setting) -> tuple[Any, str]:
        """Resolve ``entry`` to ``(value, source)``.

        Single implementation of the precedence rules shared by :meth:`get`
        and :meth:`source`; source is one of ``runtime`` | ``secret-store`` |
        ``env`` | ``default``.
        """
        if entry.kind == "secret":
            stored = self._secret_store.get_str(entry.secret_key)
            if stored:
                if self._env_lookup(entry) is not None:
                    self._warn_conflict_once(entry.key, "secret store")
                return stored, "secret-store"
            env_raw = self._env_lookup(entry)
            if env_raw is not None:
                return env_raw, "env"
            return entry.default, "default"

        if entry.kind == "boot":
            raw = os.environ.get(entry.key)
            if raw:
                return raw, "env"
            return entry.default, "default"

        data = self._read_file()
        file_key = self._file_key(entry, data)
        if file_key is not None:
            try:
                value = _coerce(entry, data[file_key], str(self.path))
            except ValueError as exc:
                logger.warning("Ignoring invalid runtime-config value: %s", exc)
            else:
                if self._env_lookup(entry) is not None:
                    self._warn_conflict_once(entry.key, "runtime config file")
                return value, "runtime"

        env_raw = self._env_lookup(entry)
        if env_raw is not None:
            try:
                return _coerce(entry, env_raw, "environment"), "env"
            except ValueError as exc:
                logger.warning("Ignoring invalid environment value: %s", exc)
        return entry.default, "default"

    def get(self, key: str) -> Any:
        """Resolve ``key``: store → env → registry default (typed)."""
        return self._resolve(REGISTRY[key])[0]

    def source(self, key: str) -> str:
        """Where :meth:`get` resolved ``key`` from: runtime | secret-store | env | default."""
        return self._resolve(REGISTRY[key])[1]

    # ── write path ────────────────────────────────────────────────────────

    def set(self, key: str, raw: Any) -> Any:
        """Validate and persist ``raw`` for ``key``. Returns the stored value.

        Raises :class:`KeyError` for unknown keys, :class:`ValueError` for
        boot-tier keys (not writable here) or validation failures.
        """
        entry = REGISTRY[key]
        if entry.kind == "boot":
            raise ValueError(f"{key} is boot environment — edit .env and recreate containers to change it")
        value = validate_for_write(entry, raw)
        if entry.kind == "secret":
            if not isinstance(value, str):
                raise ValueError(f"{key}: secrets must be strings")
            self._secret_store.set_str(entry.secret_key, value)
            return value
        data = dict(self._read_file(force=True))
        data[key] = value
        # Self-migration: a save under the canonical key retires any legacy
        # alias entries so the file converges on the new name.
        for alias in entry.aliases:
            data.pop(alias, None)
        self._write_file(data)
        return value

    def unset(self, key: str) -> bool:
        """Remove ``key`` from the store so env/default apply again. True if it was set."""
        entry = REGISTRY[key]
        if entry.kind == "boot":
            raise ValueError(f"{key} is boot environment — edit .env and recreate containers to change it")
        if entry.kind == "secret":
            return self._secret_store.delete(entry.secret_key)
        data = dict(self._read_file(force=True))
        removed = False
        for name in (key, *entry.aliases):
            if name in data:
                del data[name]
                removed = True
        if removed:
            self._write_file(data)
        return removed


# ── module-level singleton ────────────────────────────────────────────────

_instance: RuntimeSettings | None = None


def get_settings() -> RuntimeSettings:
    global _instance
    if _instance is None:
        _instance = RuntimeSettings()
    return _instance


def reset_settings_for_tests(instance: RuntimeSettings | None = None) -> None:
    """Swap (or clear) the singleton — tests point it at a tmp path with ttl=0."""
    global _instance
    _instance = instance


def get(key: str) -> Any:
    """Convenience accessor: ``settings.get("SESSION_TIMEOUT")``."""
    return get_settings().get(key)
