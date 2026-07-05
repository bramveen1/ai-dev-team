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


@dataclass(frozen=True)
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
        "AUTO_DISPATCH_CHANNEL",
        "var",
        "channel",
        "",
        "Slack channel for autonomous bug-backlog dispatch status posts. Resolved every tick.",
        "hot",
        "Dispatch",
    ),
    Setting(
        "AUTO_DISPATCH_REPO",
        "var",
        "str",
        "",
        "owner/repo for the autonomous dispatch loop. The loop is registered at boot, so "
        "changing this needs a router restart.",
        "restart",
        "Dispatch",
    ),
    Setting(
        "AUTO_DISPATCH_PAT_PATH",
        "var",
        "str",
        "",
        "Path (inside the router container) to the GitHub PAT used by auto-dispatch. "
        "Empty → /config/secrets/gh-aidt-merge.token.",
        "restart",
        "Dispatch",
    ),
    Setting(
        "MERGE_QUEUE_CHANNEL",
        "var",
        "channel",
        "",
        "Slack channel for idle auto-merge queue status posts. Resolved every tick.",
        "hot",
        "Merge queue",
    ),
    Setting(
        "MERGE_QUEUE_REPO",
        "var",
        "str",
        "",
        "owner/repo for the idle auto-merge queue. Registered at boot → restart to change.",
        "restart",
        "Merge queue",
    ),
    Setting(
        "MERGE_QUEUE_PAT_PATH",
        "var",
        "str",
        "",
        "Path to the GitHub PAT used by the merge queue. Empty → /config/secrets/gh-aidt-merge.token.",
        "restart",
        "Merge queue",
    ),
    Setting(
        "AUTO_DISPATCH_WORKER_AGENT",
        "var",
        "str",
        "",
        "Agent that runs auto-dispatched work. Empty → the agent whose manifest declares dispatch_workspace: true.",
        "hot",
        "Dispatch",
    ),
    Setting(
        "AUTO_DISPATCH_APPROVERS",
        "var",
        "str",
        "",
        "Comma-separated GitHub logins whose 'verdict: pass/fail' PR comments count. "
        "EMPTY = all verdicts ignored (fail-safe) — set this to enable the verdict gate.",
        "hot",
        "Dispatch",
    ),
    Setting(
        "DEFAULT_AGENT",
        "var",
        "str",
        "",
        "Fallback agent for un-mentioned chat messages and the terminal console. "
        "Empty → first discovered agent (alphabetical).",
        "hot",
        "Router",
    ),
    Setting(
        "OPERATOR_DM_CHANNEL",
        "var",
        "channel",
        "",
        "Fallback destination for scheduled-task and dispatch notifications when no dedicated channel is set. "
        "(Renamed from BRAM_DM_CHANNEL — the old key keeps working via alias.)",
        "hot",
        "Dispatch",
        aliases=("BRAM_DM_CHANNEL",),
    ),
    # ── Feature toggles ──────────────────────────────────────────────────
    Setting(
        "WORKER_MENTION_HANDOFF",
        "var",
        "bool",
        False,
        "Allow workers-bot @mentions through the bot-message guard (one wake per completion post).",
        "hot",
        "Features",
    ),
    Setting(
        "DISPATCH_MILESTONE_FEED",
        "var",
        "bool",
        False,
        "Post dispatch milestone updates into the originating thread.",
        "hot",
        "Features",
    ),
    Setting(
        "ATTACHMENTS_ENABLED",
        "var",
        "bool",
        False,
        "Ingest Slack file attachments into agent context.",
        "hot",
        "Features",
    ),
    Setting(
        "MEMORY_RETRIEVAL_ENABLED",
        "var",
        "bool",
        False,
        "Enable keyword memory retrieval when building agent context.",
        "hot",
        "Features",
    ),
    Setting(
        "DISCORD_ENABLED",
        "var",
        "bool",
        False,
        "Start the Discord gateway path. Evaluated at boot → restart to change.",
        "restart",
        "Features",
    ),
    Setting(
        "CHAT_BACKENDS",
        "var",
        "bool",
        False,
        "Enable the multi-backend chat abstraction. Evaluated at import → restart to change.",
        "restart",
        "Features",
    ),
    Setting(
        "SLASH_COMMAND_PREFIX",
        "var",
        "str",
        "",
        "Prefix for registered Slack slash commands (e.g. 'dev-'). Handlers register at boot.",
        "restart",
        "Features",
    ),
    Setting(
        "DISPATCH_BOT_USER_IDS",
        "var",
        "str",
        "",
        "Comma-separated Slack bot user IDs whose posts may trigger dispatch handoff.",
        "restart",
        "Features",
    ),
    Setting(
        "DISCORD_DISPATCH_BOT_IDS",
        "var",
        "str",
        "",
        "Comma-separated Discord bot snowflakes allowed to trigger agent turns.",
        "hot",
        "Features",
    ),
    # ── Router limits ────────────────────────────────────────────────────
    Setting(
        "SESSION_TIMEOUT",
        "var",
        "int",
        1800,
        "Idle session timeout in seconds (routing, cleanup, and idle detection share this).",
        "hot",
        "Router",
        min_value=1,
    ),
    Setting(
        "MAX_CONTEXT_TOKENS",
        "var",
        "int",
        32000,
        "Token budget for context assembly at dispatch time.",
        "hot",
        "Router",
        min_value=1,
    ),
    Setting(
        "LOG_LEVEL",
        "var",
        "str",
        "INFO",
        "Router log level. Applied when logging is configured at boot.",
        "restart",
        "Router",
        choices=VALID_LOG_LEVELS,
    ),
    # ── Stuck guard ──────────────────────────────────────────────────────
    Setting(
        "STUCK_GUARD_MODE",
        "var",
        "str",
        "dry-run",
        "dry-run: log and post trips only. enforce: halt the task on trip.",
        "restart",
        "Stuck guard",
        choices=("dry-run", "enforce"),
    ),
    Setting(
        "STUCK_GUARD_TURN_CAP",
        "var",
        "int",
        50,
        "Max agent turns per task before the guard trips.",
        "restart",
        "Stuck guard",
        min_value=1,
    ),
    Setting(
        "STUCK_GUARD_TURN_CAP_WINDOW",
        "var",
        "int",
        3600,
        "Rolling window (seconds) for turn-cap rate measurement. Only turns within this window count toward the cap.",
        "restart",
        "Stuck guard",
        min_value=1,
    ),
    Setting(
        "STUCK_GUARD_LOOP_WINDOW",
        "var",
        "int",
        5,
        "Sliding window (turns) for repeated-action loop detection.",
        "restart",
        "Stuck guard",
        min_value=1,
    ),
    Setting(
        "STUCK_GUARD_LOOP_THRESHOLD",
        "var",
        "int",
        3,
        "Identical actions within the window that count as a loop.",
        "restart",
        "Stuck guard",
        min_value=1,
    ),
    Setting(
        "STUCK_GUARD_ERROR_STREAK",
        "var",
        "int",
        3,
        "Consecutive CLI failures before the guard trips.",
        "restart",
        "Stuck guard",
        min_value=1,
    ),
    Setting(
        "STUCK_GUARD_POST_MORTEM_DIR",
        "var",
        "str",
        "/config/shared/stuck-tasks",
        "Directory where stuck-task post-mortems are written.",
        "restart",
        "Stuck guard",
    ),
    Setting(
        "STUCK_GUARD_MAX_TURNS_STORED",
        "var",
        "int",
        200,
        "Max per-task turn records kept in memory.",
        "restart",
        "Stuck guard",
        min_value=1,
    ),
    # ── Secrets (stored in data/secrets.json — router-only mount) ────────
    Setting(
        "WORKERS_BOT_TOKEN",
        "secret",
        "str",
        "",
        "Slack workers bot token (xoxb-…) — posts worker status back to Slack threads. Read per dispatch.",
        "hot",
        "Secrets",
        secret_key="workers_bot_token",
    ),
    Setting(
        "WORKERS_DISCORD_TOKEN",
        "secret",
        "str",
        "",
        "Discord workers bot token — posts worker status to Discord threads. Read per dispatch.",
        "hot",
        "Secrets",
        secret_key="workers_discord_token",
    ),
    # ── Boot environment (read-only in the config UI) ────────────────────
    Setting(
        "ROUTER_INTERNAL_TOKEN",
        "boot",
        "str",
        "",
        "Shared bearer token for the internal dispatch API. Required at boot.",
        "restart",
        "Boot environment",
        sensitive=True,
    ),
    Setting(
        "CLAUDE_AUTH_MODE",
        "boot",
        "str",
        "credentials",
        "Claude CLI auth mode for agent containers (credentials | oauth_token | api_key). Baked into "
        "agent container env at create.",
        "restart",
        "Boot environment",
    ),
    Setting(
        "ANTHROPIC_API_KEY",
        "boot",
        "str",
        "",
        "Metered API key for agent containers (api_key mode only).",
        "restart",
        "Boot environment",
        sensitive=True,
    ),
    Setting(
        "CLAUDE_CODE_OAUTH_TOKEN",
        "boot",
        "str",
        "",
        "Long-lived OAuth token for agent containers (oauth_token mode only).",
        "restart",
        "Boot environment",
        sensitive=True,
    ),
    Setting(
        "HEALTHZ_PORT",
        "boot",
        "str",
        "8080",
        "Host port the health/config server is published on (127.0.0.1 only).",
        "restart",
        "Boot environment",
    ),
    Setting(
        "SCHEDULED_TASKS_DB",
        "boot",
        "str",
        "data/scheduled_tasks.db",
        "SQLite path for the scheduled-tasks store. Opened at boot.",
        "restart",
        "Boot environment",
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp_path.replace(self.path)
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

    def get(self, key: str) -> Any:
        """Resolve ``key``: store → env → registry default (typed)."""
        entry = REGISTRY[key]

        if entry.kind == "secret":
            stored = self._secret_store.get_str(entry.secret_key)
            if stored:
                if self._env_lookup(entry) is not None:
                    self._warn_conflict_once(key, "secret store")
                return stored
            return self._env_lookup(entry) or entry.default

        if entry.kind == "boot":
            return os.environ.get(key) or entry.default

        data = self._read_file()
        file_key = self._file_key(entry, data)
        if file_key is not None:
            try:
                value = _coerce(entry, data[file_key], str(self.path))
            except ValueError as exc:
                logger.warning("Ignoring invalid runtime-config value: %s", exc)
            else:
                if self._env_lookup(entry) is not None:
                    self._warn_conflict_once(key, "runtime config file")
                return value

        env_raw = self._env_lookup(entry)
        if env_raw is not None:
            try:
                return _coerce(entry, env_raw, "environment")
            except ValueError as exc:
                logger.warning("Ignoring invalid environment value: %s", exc)
        return entry.default

    def source(self, key: str) -> str:
        """Where :meth:`get` resolved ``key`` from: runtime | secret-store | env | default."""
        entry = REGISTRY[key]
        if entry.kind == "secret":
            if self._secret_store.get_str(entry.secret_key):
                return "secret-store"
            return "env" if self._env_lookup(entry) else "default"
        if entry.kind == "boot":
            return "env" if os.environ.get(key) else "default"
        data = self._read_file()
        file_key = self._file_key(entry, data)
        if file_key is not None:
            try:
                _coerce(entry, data[file_key], str(self.path))
            except ValueError:
                pass
            else:
                return "runtime"
        env_raw = self._env_lookup(entry)
        if env_raw is not None:
            try:
                _coerce(entry, env_raw, "environment")
            except ValueError:
                return "default"
            return "env"
        return "default"

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
