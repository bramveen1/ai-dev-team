"""Router configuration — agent discovery and environment variable loading.

Agents are discovered by scanning ``config/agents/*/agent.yaml``. Each
manifest is the single source of truth for one agent (identity, capabilities,
scheduled tasks). Adding or removing an agent is a directory operation, not
a code change.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_AGENTS_DIR = Path("/config/agents")

SHARED_WORLDVIEW_FILE = "config/shared/WORLDVIEW.md"
SHARED_MEMORY_FILE = "config/shared/MEMORY.md"

DEFAULTS = {
    # Mirrors the SESSION_TIMEOUT registry default (router/settings.py), which
    # adopted the 600s the compose env fallback had been injecting into every
    # deployment. Note #200 raised the pre-registry constant to 1800s for
    # --max-turns 50 wall clock, but that never took effect deployed — bump
    # SESSION_TIMEOUT via /config or .env if long dispatches need it.
    # Override per-agent via container_timeout in agent.yaml.
    "session_timeout": 600,
    "log_level": "INFO",
}

# Inline JSON schema version for the ``backends:`` block in agent.yaml.
# Bump this integer when the schema changes shape in a backwards-incompatible way.
_BACKENDS_SCHEMA_VERSION = 1

# Matches ``${SECRET:SOME_VAR_NAME}`` — the only secret-reference syntax we support.
_SECRET_REF_RE = re.compile(r"^\$\{SECRET:([A-Z0-9_]+)\}$")

_agent_map_cache: dict[str, dict] | None = None


def resolve_secret_ref(value: str, env: dict[str, str] | None = None) -> str:
    """Resolve a ``${SECRET:NAME}`` reference to its env-var value.

    Non-secret strings are returned unchanged.  Raises :class:`ValueError` when
    a secret reference cannot be resolved — callers must surface this as a
    startup error rather than swallowing it.
    """
    if env is None:
        env = dict(os.environ)
    m = _SECRET_REF_RE.match(value)
    if not m:
        return value
    name = m.group(1)
    resolved = env.get(name)
    if resolved is None:
        raise ValueError(f"Unresolved secret reference ${{SECRET:{name}}} — set the {name!r} environment variable")
    return resolved


def _validate_backends_block(backends: object, agent_id: str) -> None:
    """Validate the ``backends:`` block from an agent manifest.

    Schema version: _BACKENDS_SCHEMA_VERSION.  Each backend must be a
    ``{field: string_or_secret_ref}`` mapping.  Raises :class:`ValueError`
    with a descriptive message on any structural violation so the error
    surfaces at startup rather than being silently ignored.
    """
    if not isinstance(backends, dict):
        raise ValueError(f"Agent '{agent_id}': 'backends' must be a mapping, got {type(backends).__name__}")
    for backend_name, backend_cfg in backends.items():
        if not isinstance(backend_cfg, dict):
            raise ValueError(
                f"Agent '{agent_id}': backends.{backend_name} must be a mapping, got {type(backend_cfg).__name__}"
            )
        for key, val in backend_cfg.items():
            if not isinstance(val, str):
                raise ValueError(
                    f"Agent '{agent_id}': backends.{backend_name}.{key} must be a string, got {type(val).__name__}"
                )


def _agent_manifest_paths(agents_dir: Path) -> list[Path]:
    """Return ``agent.yaml`` paths for every agent dir under ``agents_dir``."""
    if not agents_dir.exists():
        return []
    return sorted(
        agent_dir / "agent.yaml"
        for agent_dir in agents_dir.iterdir()
        if agent_dir.is_dir() and (agent_dir / "agent.yaml").exists()
    )


def discover_agents(agents_dir: Path | None = None) -> dict[str, dict]:
    """Build the agent map by scanning ``config/agents/*/agent.yaml``.

    The dict is keyed by agent id (the directory name, lowercase). Each value
    has the same shape the legacy ``AGENT_MAP`` constant produced:
    ``{name, container, role_file, personality_file, thinking_status}``.

    When ``agents_dir`` is not given, prefer the live-mount location
    (``DEFAULT_AGENTS_DIR``); if that directory does not exist (CI runners,
    local dev outside the container), fall back to the in-repo
    ``config/agents`` so discovery still resolves to the agent dirs that
    ship with the checkout. The fall-through preserves the legacy
    ``REPO_ROOT/config/agents`` behaviour for every caller that does not
    pass ``agents_dir`` explicitly.
    """
    if agents_dir is not None:
        base = agents_dir
    elif DEFAULT_AGENTS_DIR.exists():
        base = DEFAULT_AGENTS_DIR
    else:
        base = CONFIG_DIR / "agents"

    discovered: dict[str, dict] = {}

    for manifest_path in _agent_manifest_paths(base):
        agent_id = manifest_path.parent.name
        try:
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Skipping agent '%s' — failed to parse %s: %s", agent_id, manifest_path, e)
            continue

        if not isinstance(manifest, dict):
            logger.warning("Skipping agent '%s' — %s is not a YAML mapping", agent_id, manifest_path)
            continue

        if manifest.get("disabled"):
            # Reversible soft-off (toggled from the /config page). The agents
            # admin API still lists disabled agents; discovery — and therefore
            # routing, /wakeup, drain, and the compose renderer — skips them.
            logger.info("Skipping agent '%s' — disabled: true in agent.yaml", agent_id)
            continue

        display_name = manifest.get("name") or agent_id.capitalize()
        container = manifest.get("container") or agent_id
        backends = manifest.get("backends") or {}

        if backends:
            _validate_backends_block(backends, agent_id)

        discovered[agent_id] = {
            "name": display_name,
            "container": container,
            "role_file": f"config/agents/{agent_id}/role.md",
            "personality_file": f"config/agents/{agent_id}/personality.md",
            "thinking_status": manifest.get("thinking_status", ""),
            "model": manifest.get("model") or None,
            # None → fall back to the global session_timeout / SESSION_TIMEOUT env var.
            "container_timeout": manifest.get("container_timeout"),
            # Optional per-backend identity block; secrets resolved at credential-load time.
            "backends": backends,
            # Capability flag: this agent owns the per-dispatch workspace bind
            # mount (./var/dispatch ↔ /var/lib/dispatch). Replaces the old
            # hardcoded 'sam' gates in scripts/render_compose.py.
            "dispatch_workspace": bool(manifest.get("dispatch_workspace", False)),
        }

    return discovered


def get_agent_map() -> dict:
    """Return the discovered agent map.

    Cached after first call. Use :func:`reset_agent_map_cache` in tests that
    add or remove agent directories at runtime.
    """
    global _agent_map_cache
    if _agent_map_cache is None:
        _agent_map_cache = discover_agents()
    return dict(_agent_map_cache)


def reset_agent_map_cache() -> None:
    """Clear the cached agent map (useful for tests)."""
    global _agent_map_cache
    _agent_map_cache = None


def known_agent_ids() -> frozenset[str]:
    """Discovered agent ids — the single replacement for every hand-maintained
    ``KNOWN_AGENTS`` frozenset. Derived from the agent map so a renamed or
    newly added agent is recognised without a code change."""
    return frozenset(get_agent_map())


def resolve_default_agent() -> str:
    """Fallback agent for un-mentioned messages (replaces the '"sam"' literals).

    Precedence: the ``DEFAULT_AGENT`` setting when it names a discovered agent
    (a stale setting is warned about, not honoured) → first discovered agent
    alphabetically. Raises :class:`ValueError` when no agents are discovered.
    """
    from router import settings  # noqa: PLC0415 — deferred to avoid import cycle

    agent_map = get_agent_map()
    if not agent_map:
        raise ValueError("No agents discovered (config/agents/*/agent.yaml) — cannot resolve a default agent")
    configured = (settings.get("DEFAULT_AGENT") or "").strip()
    if configured:
        if configured in agent_map:
            return configured
        logger.warning("DEFAULT_AGENT=%r is not a discovered agent; falling back to first discovered", configured)
    return sorted(agent_map)[0]


def resolve_worker_agent() -> str:
    """Agent that runs auto-dispatched work (replaces the ``"sam"`` default).

    Precedence: the ``AUTO_DISPATCH_WORKER_AGENT`` setting when it names a
    discovered agent → the *sole* agent whose manifest declares
    ``dispatch_workspace: true`` (the workspace mount is what makes dispatch
    work, so alphabetical-first would be wrong). Raises :class:`ValueError`
    when unset and zero or multiple agents carry the flag.
    """
    from router import settings  # noqa: PLC0415 — deferred to avoid import cycle

    agent_map = get_agent_map()
    configured = (settings.get("AUTO_DISPATCH_WORKER_AGENT") or "").strip()
    if configured:
        if configured in agent_map:
            return configured
        raise ValueError(f"AUTO_DISPATCH_WORKER_AGENT={configured!r} is not a discovered agent")
    flagged = sorted(aid for aid, cfg in agent_map.items() if cfg.get("dispatch_workspace"))
    if len(flagged) == 1:
        return flagged[0]
    raise ValueError(
        "Cannot resolve the auto-dispatch worker agent: "
        f"{len(flagged)} agents declare dispatch_workspace ({', '.join(flagged) or 'none'}). "
        "Set dispatch_workspace: true on exactly one agent.yaml or set AUTO_DISPATCH_WORKER_AGENT."
    )


# Top-level SecretStore key holding per-agent transport credentials written
# by the /config page: {"<agent_id>": {"slack": {...}, "discord": {...}}}.
# data/secrets.json is mounted into the router only — never into agent
# containers — which is why these do not live in config/.
AGENT_CREDENTIALS_KEY = "agent_credentials"

# Complete Slack credential set — a store block is used only when all three
# are present (partial blocks warn and fall through to manifest/env).
_SLACK_REQUIRED_FIELDS = ("bot_token", "app_token", "signing_secret")


def agent_store_credentials(agent_id: str, backend: str) -> dict:
    """Per-agent credential block for ``backend`` from the secret store.

    Returns ``{}`` when absent or unreadable — callers fall through to the
    manifest/env paths, so a corrupt secrets.json can never take an agent
    below its pre-store behaviour.
    """
    from router.packs.secret_store import SecretStore  # noqa: PLC0415 — deferred to avoid import cycle

    try:
        block = SecretStore().get(AGENT_CREDENTIALS_KEY)
    except ValueError as exc:
        logger.warning("secret store unreadable while loading agent credentials: %s", exc)
        return {}
    agent_block = block.get(agent_id)
    if not isinstance(agent_block, dict):
        return {}
    backend_block = agent_block.get(backend)
    return backend_block if isinstance(backend_block, dict) else {}


def load_slack_credentials(agent_map: dict[str, dict]) -> dict[str, dict[str, str]]:
    """Load per-agent Slack credentials from ``agent_map``.

    Three sources, per agent, store-over-manifest-over-env:

    * **secret store** — a complete ``agent_credentials.<id>.slack`` block in
      ``data/secrets.json`` (written via the /config page) wins outright.
      Incomplete blocks warn and fall through. Restart-class by design:
      socket-mode connections are built at boot (#576 deferred hot reload of
      live-connection credentials).

    * **backends.slack declared** — credentials come from the ``backends.slack``
      block in the agent manifest.  Each value may be a ``${SECRET:NAME}``
      reference, which is resolved against the current environment.  A missing
      secret raises :class:`ValueError` immediately (fail-loud) so misconfigured
      deployments are caught at startup rather than at first message.

    * **no backends.slack** — falls back to the legacy
      ``<AGENT>_BOT_TOKEN`` / ``<AGENT>_APP_TOKEN`` / ``<AGENT>_SIGNING_SECRET``
      env-var convention.  Agents missing any of the three are skipped with a
      warning (soft-skip), preserving the previous behaviour for agents that
      have not yet migrated.
    """
    credentials: dict[str, dict[str, str]] = {}
    for agent_id, agent_cfg in agent_map.items():
        stored = agent_store_credentials(agent_id, "slack")
        if stored:
            complete = all(isinstance(stored.get(f), str) and stored.get(f) for f in _SLACK_REQUIRED_FIELDS)
            if complete:
                credentials[agent_id] = {f: stored[f] for f in _SLACK_REQUIRED_FIELDS}
                if (agent_cfg.get("backends") or {}).get("slack") or os.environ.get(f"{agent_id.upper()}_BOT_TOKEN"):
                    logger.warning(
                        "Agent '%s': Slack credentials found in BOTH the secret store and manifest/env; "
                        "the secret store wins (store-over-env)",
                        agent_id,
                    )
                continue
            missing = [f for f in _SLACK_REQUIRED_FIELDS if not stored.get(f)]
            logger.warning(
                "Agent '%s': agent_credentials.slack in the secret store is incomplete (missing: %s); "
                "falling back to manifest/env",
                agent_id,
                ", ".join(missing),
            )

        backends = agent_cfg.get("backends") or {}
        slack_cfg = backends.get("slack")

        if slack_cfg is not None:
            # Explicit backends.slack block — resolve secrets, fail loud on missing.
            resolved: dict[str, str] = {}
            for key, val in slack_cfg.items():
                try:
                    resolved[key] = resolve_secret_ref(val)
                except ValueError as exc:
                    raise ValueError(f"Agent '{agent_id}': {exc}") from exc

            required = {"bot_token", "app_token", "signing_secret"}
            missing_fields = required - resolved.keys()
            if missing_fields:
                raise ValueError(
                    f"Agent '{agent_id}': backends.slack missing required fields: {', '.join(sorted(missing_fields))}"
                )

            credentials[agent_id] = resolved
        else:
            # No backends block — fall back to env-var convention (soft-skip).
            prefix = agent_id.upper()
            bot_token = os.environ.get(f"{prefix}_BOT_TOKEN", "")
            app_token = os.environ.get(f"{prefix}_APP_TOKEN", "")
            signing_secret = os.environ.get(f"{prefix}_SIGNING_SECRET", "")

            if not (bot_token and app_token and signing_secret):
                missing = [
                    name
                    for name, value in (
                        (f"{prefix}_BOT_TOKEN", bot_token),
                        (f"{prefix}_APP_TOKEN", app_token),
                        (f"{prefix}_SIGNING_SECRET", signing_secret),
                    )
                    if not value
                ]
                logger.warning("Skipping agent '%s' — missing Slack env vars: %s", agent_id, ", ".join(missing))
                continue

            credentials[agent_id] = {
                "bot_token": bot_token,
                "app_token": app_token,
                "signing_secret": signing_secret,
            }
    return credentials


def load_discord_credentials(agent_map: dict[str, dict]) -> dict[str, dict]:
    """Load per-agent Discord credentials from ``agent_map``.

    Three sources, per agent, store-over-manifest-over-env:

    * **secret store** — an ``agent_credentials.<id>.discord`` block with a
      non-empty ``bot_token`` (written via the /config page) wins outright;
      ``default_channel_id`` is optional (default ``0``). Restart-class.

    * **backends.discord declared** — credentials come from the ``backends.discord``
      block in the agent manifest.  ``bot_token`` is required and may be a
      ``${SECRET:NAME}`` reference resolved against the current environment.  A
      missing secret raises :class:`ValueError` immediately (fail-loud).
      ``default_channel_id`` is optional (default ``0``) and is cast to ``int``.

    * **no backends.discord** — falls back to the
      ``<AGENT>_DISCORD_BOT_TOKEN`` / ``<AGENT>_DISCORD_CHANNEL_ID``
      env-var convention.  Agents missing a token are skipped with a warning
      (soft-skip).
    """
    credentials: dict[str, dict] = {}
    for agent_id, agent_cfg in agent_map.items():
        stored = agent_store_credentials(agent_id, "discord")
        stored_token = stored.get("bot_token")
        if isinstance(stored_token, str) and stored_token:
            raw_channel = stored.get("default_channel_id") or "0"
            try:
                stored_channel = int(raw_channel)
            except (ValueError, TypeError):
                logger.warning(
                    "Agent '%s': agent_credentials.discord.default_channel_id=%r is not a valid int; using 0",
                    agent_id,
                    raw_channel,
                )
                stored_channel = 0
            credentials[agent_id] = {"bot_token": stored_token, "default_channel_id": stored_channel}
            continue

        backends = agent_cfg.get("backends") or {}
        discord_cfg = backends.get("discord")

        if discord_cfg is not None:
            # Explicit backends.discord block — resolve secrets, fail loud on missing.
            if "bot_token" not in discord_cfg:
                raise ValueError(f"Agent '{agent_id}': backends.discord missing required field: bot_token")

            try:
                bot_token = resolve_secret_ref(discord_cfg["bot_token"])
            except ValueError as exc:
                raise ValueError(f"Agent '{agent_id}': {exc}") from exc

            default_channel_id = 0
            if "default_channel_id" in discord_cfg:
                raw = discord_cfg["default_channel_id"]
                try:
                    default_channel_id = int(resolve_secret_ref(raw))
                except (ValueError, TypeError) as exc:
                    raise ValueError(
                        f"Agent '{agent_id}': backends.discord.default_channel_id must be castable to int: {exc}"
                    ) from exc

            credentials[agent_id] = {
                "bot_token": bot_token,
                "default_channel_id": default_channel_id,
            }
        else:
            # No backends.discord block — fall back to env-var convention (soft-skip).
            prefix = agent_id.upper()
            bot_token = os.environ.get(f"{prefix}_DISCORD_BOT_TOKEN", "")
            if not bot_token:
                logger.debug(
                    "Skipping agent '%s' — no Discord credentials "
                    "(no backends.discord block, no %s_DISCORD_BOT_TOKEN env var)",
                    agent_id,
                    prefix,
                )
                continue

            channel_id_str = os.environ.get(f"{prefix}_DISCORD_CHANNEL_ID", "0")
            try:
                default_channel_id = int(channel_id_str)
            except (ValueError, TypeError):
                logger.warning(
                    "Agent '%s': %s_DISCORD_CHANNEL_ID=%r is not a valid int, defaulting to 0",
                    agent_id,
                    prefix,
                    channel_id_str,
                )
                default_channel_id = 0

            credentials[agent_id] = {
                "bot_token": bot_token,
                "default_channel_id": default_channel_id,
            }

    return credentials


def resolve_session_timeout() -> int:
    """Return the configured idle session timeout in seconds.

    Single source of truth for the global session timeout: the
    ``SESSION_TIMEOUT`` setting (runtime config file, then env var — see
    :mod:`router.settings`), falling back to the registry default. Both
    :func:`load_config` and the merge-queue idle guard call this so routing,
    cleanup, and idle detection share one expiry boundary (issue #462).
    """
    from router import settings  # noqa: PLC0415 — deferred to avoid import cycle

    return int(settings.get("SESSION_TIMEOUT"))


def load_config() -> dict:
    """Load configuration from environment variables with sensible defaults.

    Returns a dict with:
        - slack_credentials: ``{agent_name: {bot_token, app_token, signing_secret}}``
        - session_timeout: Seconds before an idle session times out
        - log_level: Logging level string
        - agent_map: The agent configuration map

    Note: the context token budget is owned by ``router.dispatcher``. It reads
    the ``MAX_CONTEXT_TOKENS`` env var (with a sane default) and is the single
    source of truth — adding a second copy here is what caused issue #144.
    """
    from router import settings  # noqa: PLC0415 — deferred to avoid import cycle

    agent_map = get_agent_map()
    cfg = {
        "slack_credentials": load_slack_credentials(agent_map),
        "session_timeout": resolve_session_timeout(),
        "log_level": settings.get("LOG_LEVEL"),
        "agent_map": agent_map,
    }

    logger.debug(
        "Loaded config: agents_with_creds=%d, session_timeout=%d",
        len(cfg["slack_credentials"]),
        cfg["session_timeout"],
    )
    return cfg
